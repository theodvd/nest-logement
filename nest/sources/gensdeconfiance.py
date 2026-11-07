"""Connecteur Gens de Confiance (petites annonces réservées aux membres parrainés).

Le site est fait en Next.js : la page de recherche porte les annonces dans son
JSON `__NEXT_DATA__`, la fiche d'une annonce dans le flux React (`self.__next_f`).
Les coordonnées GPS ne sont données qu'aux membres connectés : on envoie le
cookie de session de l'utilisateur (variable GDC_COOKIE du .env). Sans cookie,
la source est simplement ignorée.
Cloudflare refuse les clients qui ne ressemblent pas à un navigateur : curl_cffi
imite l'empreinte réseau de Chrome, comme `lbc` le fait pour Leboncoin.

Accès non officiel, avec le compte de l'utilisateur : rythme bas (page 1 de deux
recherches par passage, au plus toutes les 30 min, voir `__main__`) et une fiche
ouverte seulement pour les annonces qui ont passé filtres et trajet.

Limite connue : le site ignore la position envoyée sans identifiant de lieu Google
et centre la recherche sur Paris. Hors Île-de-France, cette source ne sert à rien.

Exemples d'appel :
    python -m nest.sources.gensdeconfiance            # recherche du profil, résumé
    python -m nest.sources.gensdeconfiance <uuid>     # lecture d'une fiche
"""

import html as module_html
import json
import logging
import os
import re
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from curl_cffi import requests as cffi

from nest.annonce import Annonce

log = logging.getLogger(__name__)

SOURCE = "gensdeconfiance"
SITE = "https://gensdeconfiance.com"
URL_RECHERCHE = f"{SITE}/fr/ui/results"
URL_FICHE = SITE + "/fr/ui/post/realestate__rent/{uuid}"
PARIS = ZoneInfo("Europe/Paris")

# Filtres du site par type de bien du profil (valeurs multiples séparées par des virgules).
# Studio/T1 : logement entier d'une pièce ; colocation : colocation ou chambre.
FILTRES = {
    "studio_t1": {"propertyTypes": "apartment,home", "rentalTypes": "home", "nbPieces": "1"},
    "colocation": {"rentalTypes": "flatshare,room"},
}

MOIS = {"janvier": 1, "février": 2, "mars": 3, "avril": 4, "mai": 5, "juin": 6, "juillet": 7,
        "août": 8, "septembre": 9, "octobre": 10, "novembre": 11, "décembre": 12}

RE_NEXT_DATA = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)
RE_FLUX = re.compile(r'self\.__next_f\.push\(\[1,"((?:[^"\\]|\\.)*)"\]\)')
RE_CRITERE = re.compile(r'"\$","div","([a-z_]+)",\{"className":"[^"]*_criterion"')
RE_TEXTE = re.compile(r'"children":"((?:[^"\\]|\\.)*)"')
# Critères dont la valeur n'est portée que par une classe CSS (lettre en surbrillance).
CRITERES_ILLISIBLES = {"dpe", "ges"}


class SessionExpiree(Exception):
    """GDC_COOKIE absent ou expiré : le site ne donne plus les coordonnées GPS."""


class Bloque(Exception):
    """Cloudflare a refusé la requête (défi anti-bot)."""


def client() -> cffi.Session:
    """Session HTTP qui imite Chrome et porte le cookie de session de l'utilisateur."""
    cookie = os.getenv("GDC_COOKIE")
    if not cookie:
        raise SessionExpiree("GDC_COOKIE absent de l'environnement")
    session = cffi.Session(impersonate="chrome")
    session.headers.update({"Cookie": cookie, "Accept-Language": "fr-FR,fr;q=0.9"})
    return session


def _lire(session: cffi.Session, url: str, params: dict | None = None) -> str:
    """GET d'une page du site ; lève Bloque si Cloudflare oppose son défi."""
    rep = session.get(url, params=params, timeout=30)
    if rep.status_code == 403 or rep.headers.get("cf-mitigated"):
        raise Bloque(f"Cloudflare a refusé {url} (HTTP {rep.status_code})")
    rep.raise_for_status()
    return rep.text


def _page_props(page: str) -> dict:
    """Extrait `props.pageProps` du JSON Next.js d'une page de recherche."""
    m = RE_NEXT_DATA.search(page)
    if not m:
        raise ValueError("page de recherche sans __NEXT_DATA__ : la structure du site a changé ?")
    return json.loads(m.group(1))["props"]["pageProps"]


def _date_paris(ms: int | None) -> str:
    """Horodatage en millisecondes → "AAAA-MM-JJ HH:MM:SS", heure de Paris."""
    if not ms:
        return ""
    return datetime.fromtimestamp(ms / 1000, PARIS).strftime("%Y-%m-%d %H:%M:%S")


def _montant(prix: dict | None) -> float | None:
    """{"value": 1010, "currency": "EUR"} → 1010.0, None si absent."""
    valeur = (prix or {}).get("value")
    return float(valeur) if valeur is not None else None


def _convertir(item: dict, type_bien: str) -> Annonce:
    """Transforme une annonce de la page de recherche en Annonce commune.

    Sur Gens de Confiance, `price` est le loyer hors charges et `rentalCharge`
    les charges (vérifié le 03/11/2026 : 1 010 + 40 = 1 050 € CC sur la fiche).
    """
    adresse = item.get("address") or {}
    lat, lng = adresse.get("latitude"), adresse.get("longitude")
    hc, charges = _montant(item.get("price")), _montant(item.get("rentalCharge"))
    proprietaire = item.get("collectionOwner") or {}
    pro = bool(item.get("isPro") or proprietaire.get("isProfessionalProfile")
               or item.get("handledBy") == "pro")
    etage, nb_etages = item.get("propertyFloor"), item.get("nbFloorsBuilding")
    ascenseur = item.get("elevator")
    return Annonce(
        source=SOURCE,
        id_source=item["uuid"],
        url=URL_FICHE.format(uuid=item["uuid"]),
        titre=item.get("title") or "",
        description="",                 # absente de la recherche : voir completer()
        type_bien=type_bien,
        loyer_cc=hc + (charges or 0) if hc is not None else None,
        loyer_hc=hc,
        charges=charges,
        surface_m2=item.get("carrezSurface"),
        pieces=item.get("numberOfRooms"),
        meuble={"furnished": True, "unfurnished": False}.get(
            (item.get("realEstate") or {}).get("rentalFurnishings")),
        lat=lat,
        lng=lng,
        position_precise=lat is not None and lng is not None,
        lieu=" ".join(filter(None, [adresse.get("city"), adresse.get("zip")])),
        publiee_le=_date_paris(item.get("publishedAt")),
        disponible_le=None,             # sur la fiche seulement
        photos=[item["mainImageUrl"].split("?")[0]] if item.get("mainImageUrl") else [],
        details={
            "etage": f"{etage}/{nb_etages}" if etage is not None and nb_etages else etage,
            "ascenseur": {True: "Oui", False: "Non"}.get(ascenseur),
            "caracteristiques": item.get("realEstateEquipments") or None,
            "exposition": item.get("mainExposures") or None,
            "exclusivite_gdc": item.get("advantage") == "exclusivity",
            "annonceur": {"nom": proprietaire.get("fullName"), "pro": pro},
        },
        vendeur_id=f"gdc:{proprietaire['vanity']}" if proprietaire.get("vanity") else None,
    )


def chercher(profil: dict, depuis: str | None = None, pages_max: int = 8,
             pause_s: float = 2.0, session: cffi.Session | None = None) -> list[Annonce]:
    """Récupère les annonces récentes du profil (page 1 de chaque recherche).

    Le site trie par date d'affichage (une annonce remontée revient en tête).
    `depuis` ("AAAA-MM-JJ HH:MM:SS") sert au rattrapage après un trou : chaque
    recherche est paginée jusqu'à ce que 90 % d'une page soit plus ancien, au
    plus `pages_max` pages.

    Lève SessionExpiree si le cookie ne connecte plus : sans connexion, les
    annonces arrivent sans coordonnées et seraient toutes écartées.

    Exemple :
        >>> annonces = chercher(charger_profil())
        >>> annonces[0].lieu, annonces[0].loyer_cc
    """
    recherche = profil["recherche"]
    bureaux = recherche["bureaux"]
    # Le site ignore lat/lon sans identifiant de lieu Google et centre sur Paris
    # (sans effet pour un lieu de travail parisien). Les deux restent obligatoires
    # (sans eux, la recherche porte sur toute la France).
    commun = {"types": "offering", "category": "realestate__rent",
              "maxPrice": int(recherche["budget_max_cc"]),      # filtre le loyer HC : large
              "radius": int(recherche["rayon_recherche_km"]),
              "lat": bureaux["lat"], "lon": bureaux["lng"]}
    limite_ms = (datetime.strptime(depuis, "%Y-%m-%d %H:%M:%S").replace(tzinfo=PARIS).timestamp() * 1000
                 if depuis else None)
    session = session or client()
    annonces: dict[str, Annonce] = {}

    for type_bien in (t for t in recherche["types"] if t in FILTRES):
        for page in range(1, (pages_max if depuis else 1) + 1):
            if annonces:
                time.sleep(pause_s)
            params = {**commun, **FILTRES[type_bien], **({"page": page} if page > 1 else {})}
            props = _page_props(_lire(session, URL_RECHERCHE, params))
            if not props.get("user"):
                raise SessionExpiree("le site ne reconnaît plus la session (cookie expiré ?)")
            resultat = props["initialSearchClassifieds"]
            items = resultat.get("items") or []
            for item in items:
                annonces.setdefault(item["uuid"], _convertir(item, type_bien))
            if not limite_ms or page >= (resultat.get("lastPage") or 1) or not items:
                break
            anciennes = sum((i.get("displayDate") or 0) < limite_ms for i in items)
            if anciennes >= 0.9 * len(items):
                log.info("Rattrapage Gens de Confiance %s : %d page(s) lues", type_bien, page)
                break

    sans_position = sum(not a.position_precise for a in annonces.values())
    if sans_position:
        log.warning("Gens de Confiance : %d annonce(s) sans coordonnées", sans_position)
    log.info("Gens de Confiance : %d annonces récupérées", len(annonces))
    return list(annonces.values())


def _flux(page: str) -> str:
    """Recolle le flux React de la fiche (morceaux `self.__next_f.push`)."""
    return "".join(json.loads(f'"{m}"') for m in RE_FLUX.findall(page))


def _textes(fragment: str) -> list[str]:
    """Tous les textes `"children":"…"` d'un fragment du flux, décodés."""
    return [json.loads(f'"{t}"') for t in RE_TEXTE.findall(fragment)]


def criteres(flux: str) -> dict[str, str]:
    """Critères de la fiche : {"availability_date": "27 novembre 2026", ...}.

    Chaque critère est un bloc `["$","div","<clé>",{"className":"…_criterion"}]`
    qui contient son libellé, et sa valeur soit en clair juste après, soit dans
    une ligne à part du flux (`cb:[…]`) désignée par une référence `$Lcb`.
    """
    lignes = dict(re.findall(r"(?:^|\n)([0-9a-f]+):(.*)", flux))
    blocs = list(RE_CRITERE.finditer(flux))
    valeurs = {}
    for n, m in enumerate(blocs):
        if m.group(1) in CRITERES_ILLISIBLES:
            continue
        fin = blocs[n + 1].start() if n + 1 < len(blocs) else m.start() + 4000
        bloc = flux[m.start():fin]
        ref = re.search(r'"\$L([0-9a-f]+)"', bloc)
        textes_ref = _textes(lignes.get(ref.group(1), "")) if ref else []
        textes_bloc = _textes(bloc)
        if textes_ref:
            valeurs[m.group(1)] = " ".join(textes_ref)
        elif len(textes_bloc) >= 2:
            valeurs[m.group(1)] = textes_bloc[1]
    return valeurs


def _date_fr(texte: str | None) -> str | None:
    """« 27 novembre 2026 » → « 27/11/2026 » (format lu par filtres.mois_dispo)."""
    m = re.match(r"(\d{1,2}) (\S+) (\d{4})", (texte or "").strip())
    if not m or m.group(2).lower() not in MOIS:
        return texte
    return f"{int(m.group(1)):02d}/{MOIS[m.group(2).lower()]:02d}/{m.group(3)}"


def lire_fiche(page: str) -> dict:
    """Extrait de la fiche ce que la recherche ne donne pas.

    Exemple de sortie :
        {"description": "Chers Gens de Confiance, …", "disponible_le": "27/11/2026",
         "criteres": {"contract_type": "Bail meublé", "floor": "1 sur 6", …},
         "frais_service": "321 €"}
    """
    flux = _flux(page)
    meta = re.search(r'<meta name="description" content="([^"]*)"', page)
    frais = re.search(r"frais de service de <strong>([^<]+)</strong>", flux, re.I)
    crit = criteres(flux)
    return {
        "description": module_html.unescape(meta.group(1)).strip() if meta else "",
        "disponible_le": _date_fr(crit.get("availability_date")),
        "criteres": crit,
        "frais_service": frais.group(1).replace(" ", " ").replace("\xa0", " ") if frais else None,
    }


def completer(annonce: Annonce, session: cffi.Session | None = None) -> Annonce:
    """Ouvre la fiche et complète l'annonce (description, disponibilité, bail, frais).

    Appelé juste avant le jugement, pour les seules annonces candidates.
    """
    fiche = lire_fiche(_lire(session or client(), annonce.url))
    crit = fiche["criteres"]
    annonce.description = fiche["description"]
    annonce.disponible_le = fiche["disponible_le"]
    annonce.details.update({k: v for k, v in {
        "type_contrat": crit.get("contract_type"),
        "etage": crit.get("floor") or annonce.details.get("etage"),
        "depot_garantie": crit.get("security_deposit"),
        "equipements": crit.get("equipments"),
        # Frais facturés au locataire par Gens de Confiance, en plus du loyer. Libellé
        # explicite : sans lui, Claude y voit un paiement suspect (passage du 03/11).
        "frais_service_gdc": (f"{fiche['frais_service']} : frais officiels de Gens de Confiance, "
                              "facturés au locataire, normaux sur ce site (coût en plus, pas une arnaque)"
                              if fiche["frais_service"] else None),
    }.items() if v})
    return annonce


if __name__ == "__main__":
    import yaml
    from pathlib import Path

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if len(sys.argv) > 1:
        uuid = sys.argv[1].rstrip("/").split("/")[-1].split("?")[0]
        fiche = lire_fiche(_lire(client(), URL_FICHE.format(uuid=uuid)))
        print(json.dumps({**fiche, "description": fiche["description"][:300]}, ensure_ascii=False, indent=2))
    else:
        with open(Path(__file__).resolve().parents[2] / "profil.yaml", encoding="utf-8") as f:
            for a in chercher(yaml.safe_load(f)):
                print(f"{a.type_bien:11} {a.loyer_cc or 0:6.0f} € CC  {a.surface_m2 or 0:4.0f} m²  "
                      f"{'GPS' if a.position_precise else 'sans GPS'}  {a.publiee_le}  {a.lieu:15}  {a.titre[:50]}")
