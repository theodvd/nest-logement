"""Doublons : un même logement vu sur deux sites, ou republié sous un nouvel identifiant.

Mesuré le 03/11/2026 sur la base de prod (3 753 annonces) : les paires qui partagent
type, surface, loyer et position ont des descriptions soit quasi identiques
(ressemblance ≥ 0,9 pour 378 paires), soit sans rapport (< 0,3). Le seuil de 0,6
sépare les deux ; en dessous, ce sont surtout des lots d'agence (plusieurs logements
d'un même immeuble), qu'il ne faut pas fusionner.

Vérifié juste avant le jugement : pas de second appel à Claude, pas de seconde notif.

Exemple :
    >>> original = trouver(annonce, base)
    >>> original["cle"], original["statut"], original["vue_le"]
"""

import difflib
import json
import re
from datetime import datetime, timedelta

from nest.filtres import distance_km

# Annonces auxquelles on compare : celles qui ont atteint (ou vont atteindre) le jugement.
STATUTS_COMPARES = ("jugee", "notifiee", "en_attente")
FENETRE_JOURS = 60
TOLERANCE_LOYER = 0.03          # 3 % du loyer charges comprises
TOLERANCE_SURFACE_M2 = 1.5
SEUIL_DESCRIPTION = 0.6         # meilleur des deux scores : caractères (copie) ou mots (réécriture)
# Deux positions précises.
MEME_POINT_KM = 0.03            # même adresse : un peu de texte commun suffit (réécriture)
SEUIL_MEME_POINT = 0.3          # Oberkampf réécrit : 0,36 ; « 2 pièces » / « studio » : 0,16
DISTANCE_MAX_KM = 0.2
# Deux positions approximatives (Leboncoin : centre du quartier) : un autre point est un
# autre quartier, donc un autre logement, même avec le même texte type d'agence.
MEME_POINT_FLOU_KM = 0.05
TOLERANCE_LOYER_FLOU = 0.01
TOLERANCE_SURFACE_FLOU_M2 = 0.5
# Une précise et une approximative (deux sites) : quartier contre adresse réelle.
DISTANCE_MAX_MIXTE_KM = 3
SEUIL_TITRE = 0.85              # repli quand une description manque
LONGUEUR_MIN_DESCRIPTION = 80
NOMS_SITES = {"leboncoin": "Leboncoin", "gensdeconfiance": "Gens de Confiance"}


def _normaliser(texte: str | None) -> str:
    """Minuscules, ponctuation et espaces multiples retirés."""
    texte = re.sub(r"[^a-z0-9àâçéèêëîïôûùüÿœ ]", " ", (texte or "").lower())
    return re.sub(r"\s+", " ", texte).strip()


def _ressemblance(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a, b).ratio()


def _mots_communs(a: str, b: str) -> float:
    """Part de mots (4 lettres et plus) partagés : résiste à une description réécrite."""
    ma, mb = ({m for m in t.split() if len(m) >= 4} for t in (a, b))
    return len(ma & mb) / len(ma | mb) if ma and mb else 0.0


def meme_logement(a: dict, b: dict) -> bool:
    """True si deux annonces (dicts d'Annonce) décrivent le même logement.

    Loyer (3 %) et surface (1,5 m²) sont déjà filtrés par la requête de trouver().
    Règles réglées sur la base de prod le 03/11 (un faux doublon fait manquer un
    logement : on préfère une notif en trop) :
    - deux positions précises : même point et un peu de texte commun, ou 200 m et
      texte ressemblant ;
    - deux approximatives (republication Leboncoin) : même point, même surface,
      même loyer et texte ressemblant ;
    - une de chaque (deux sites) : 3 km et texte ressemblant.
    """
    if None in (a.get("lat"), a.get("lng"), b.get("lat"), b.get("lng")):
        return False
    distance = distance_km(a["lat"], a["lng"], b["lat"], b["lng"])
    da, db = _normaliser(a.get("description")), _normaliser(b.get("description"))
    texte = (max(_ressemblance(da[:600], db[:600]), _mots_communs(da, db))
             if min(len(da), len(db)) >= LONGUEUR_MIN_DESCRIPTION else None)
    nb_precises = bool(a.get("position_precise")) + bool(b.get("position_precise"))

    if nb_precises == 2:
        if distance <= MEME_POINT_KM and (texte is None or texte >= SEUIL_MEME_POINT):
            return True
        if distance > DISTANCE_MAX_KM:
            return False
        if texte is not None:
            return texte >= SEUIL_DESCRIPTION
        return _ressemblance(_normaliser(a.get("titre")), _normaliser(b.get("titre"))) >= SEUIL_TITRE
    if texte is None or texte < SEUIL_DESCRIPTION:
        return False
    if nb_precises == 0:
        return (distance <= MEME_POINT_FLOU_KM
                and abs(a["surface_m2"] - b["surface_m2"]) <= TOLERANCE_SURFACE_FLOU_M2
                and abs(a["loyer_cc"] - b["loyer_cc"]) <= TOLERANCE_LOYER_FLOU * a["loyer_cc"])
    return distance <= DISTANCE_MAX_MIXTE_KM


def trouver(annonce, base) -> dict | None:
    """Annonce déjà connue qui décrit le même logement, ou None.

    Renvoie {"cle", "statut", "vue_le", "annonce"} (annonce = dict de l'original).
    """
    loyer, surface = annonce.loyer_cc, annonce.surface_m2
    if not loyer or not surface:
        return None
    depuis = (datetime.now() - timedelta(days=FENETRE_JOURS)).isoformat(timespec="seconds")
    lignes = base.cx.execute(
        f"""SELECT cle, statut, vue_le, annonce FROM annonces
            WHERE statut IN ({",".join("?" * len(STATUTS_COMPARES))})
              AND cle != ? AND vue_le >= ?
              AND json_extract(annonce, '$.type_bien') = ?
              AND abs(json_extract(annonce, '$.surface_m2') - ?) <= ?
              AND abs(json_extract(annonce, '$.loyer_cc') - ?) <= ?
            ORDER BY vue_le""",
        (*STATUTS_COMPARES, annonce.cle, depuis, annonce.type_bien,
         surface, TOLERANCE_SURFACE_M2, loyer, TOLERANCE_LOYER * loyer)).fetchall()
    nouvelle = annonce.to_dict()
    for cle, statut, vue_le, brut in lignes:
        original = json.loads(brut)
        if meme_logement(nouvelle, original):
            return {"cle": cle, "statut": statut, "vue_le": vue_le, "annonce": original}
    return None


def nom_site(source: str) -> str:
    return NOMS_SITES.get(source, source)
