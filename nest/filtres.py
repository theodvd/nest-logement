"""Filtres durs, sans IA : éliminent gratuitement l'essentiel des annonces.

Chaque filtre renvoie None si l'annonce passe, sinon la raison de l'écart
(stockée en base pour pouvoir auditer ce qui a été jeté).
"""

import math
import re
from datetime import datetime, timedelta

# Titres sans ambiguïté de location courte (le reste est relevé par Claude).
# « 6 mois » et plus ne sont pas visés : c'est la durée du stage.
COURTE_DUREE = re.compile(
    r"courte durée|ponctuel|par semaine|\b\d+\s*(?:à\s*\d+\s*)?(?:semaines?|jours?|nuits?)\b"
    r"|\b(?:[1-5]|un|deux|trois|quatre|cinq)\s*(?:[-àa]\s*[1-5]\s*)?mois\b"
    r"|\bdu\s+\d{1,2}(?:/\d{1,2})?\s+au\s+\d{1,2}",
    re.IGNORECASE,
)


def mois_dispo(disponible_le: str | None) -> tuple[int, int] | None:
    """Convertit "11/2026" ou "01/11/2026" en (année, mois), None si absent ou illisible."""
    if not disponible_le:
        return None
    morceaux = disponible_le.strip().split("/")
    try:
        mois, annee = int(morceaux[-2]), int(morceaux[-1])
        return annee, mois
    except (ValueError, IndexError):
        return None


def distance_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Distance à vol d'oiseau (formule de haversine), en kilomètres."""
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _compact(texte: str) -> str:
    """« Immo Exemple », « immo-exemple » et « IMMOEXEMPLE » donnent la même chaîne."""
    return re.sub(r"[^a-z0-9]", "", (texte or "").lower())


def marque_signalee(annonce, profil: dict) -> str | None:
    """Renvoie la marque d'arnaque citée dans l'annonce, s'il y en a une."""
    texte = _compact(annonce.titre + " " + annonce.description)
    for marque in profil.get("arnaques", {}).get("marques", []):
        if _compact(marque) and _compact(marque) in texte:
            return marque
    return None


def raison_ecart(annonce, profil: dict, maintenant: datetime | None = None,
                 anciennete_max_h: float | None = None) -> str | None:
    """Applique les filtres durs dans l'ordre du moins cher au plus cher.

    `anciennete_max_h` remplace la valeur du profil : après un trou de veille, on
    l'allonge de la durée du trou pour ne pas jeter une annonce jamais vue.
    """
    recherche = profil["recherche"]
    maintenant = maintenant or datetime.now()
    anciennete_max_h = anciennete_max_h or recherche["anciennete_max_h"]

    # 1. Ancienneté : le tri "plus récentes" remonte aussi des annonces sponsorisées.
    try:
        publiee = datetime.strptime(annonce.publiee_le, "%Y-%m-%d %H:%M:%S")
        if maintenant - publiee > timedelta(hours=anciennete_max_h):
            return f"trop ancienne ({annonce.publiee_le})"
    except ValueError:
        pass  # date illisible : on laisse passer plutôt que de rater une annonce

    # 1 bis. Marque d'arnaque signalée citée dans l'annonce.
    if marque := marque_signalee(annonce, profil):
        return f"marque signalée ({marque})"

    # 2. Location courte durée évidente dans le titre.
    if m := COURTE_DUREE.search(annonce.titre):
        return f"courte durée ({m.group(0)})"

    # 3. Dispo explicite trop tôt : il paierait des mois à vide (dates absentes = on garde).
    if (au_plus_tot := recherche.get("dispo_au_plus_tot")) and (am := mois_dispo(annonce.disponible_le)):
        annee, mois = map(int, au_plus_tot.split("-"))
        if am < (annee, mois):
            return f"dispo trop tôt ({annonce.disponible_le})"

    # 4. Budget (charges comprises).
    if annonce.loyer_cc and annonce.loyer_cc > recherche["budget_max_cc"]:
        return f"budget ({annonce.loyer_cc:.0f} € CC)"

    # 5. Distance à vol d'oiseau : évite de payer un calcul d'itinéraire inutile.
    if annonce.lat is None or annonce.lng is None:
        return "pas de position"
    bureaux = recherche["bureaux"]
    km = distance_km(annonce.lat, annonce.lng, bureaux["lat"], bureaux["lng"])
    if km > recherche["distance_max_vol_oiseau_km"]:
        return f"trop loin à vol d'oiseau ({km:.1f} km)"

    return None
