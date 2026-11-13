"""Trajet effectif jusqu'au lieu de travail : le meilleur mode entre transports, marche et moto.

- Transports : Google Routes (TRANSIT), arrivée à l'heure visée un mardi.
- Marche : Google Routes (WALK), seulement si l'annonce est assez proche. Une
  minute à pied peut compter moins qu'une minute en transports pour qui aime
  marcher (`poids_minute_marche` du profil : 0.67 = 30 min à pied ≈ 20 min de
  métro ; 1 = neutre).
- Moto (option `moto: true` du profil) : seulement si l'annonce mentionne un
  parking privé. Google ne calcule pas les deux-roues en France : on prend le
  temps en voiture à l'heure de pointe (TRAFFIC_AWARE), estimation prudente.

Sans clé Google, repli sur une estimation à vol d'oiseau (transports seulement).
"""

import logging
import os
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests

from nest.filtres import distance_km

log = logging.getLogger(__name__)

URL_ROUTES = "https://routes.googleapis.com/directions/v2:computeRoutes"
# Le field mask limite la réponse (et le coût) aux champs utiles.
CHAMPS_TRANSIT = ",".join([
    "routes.duration",
    "routes.legs.steps.travelMode",
    "routes.legs.steps.staticDuration",
    "routes.legs.steps.transitDetails.stopDetails.departureStop.name",
    "routes.legs.steps.transitDetails.transitLine.nameShort",
    "routes.legs.steps.transitDetails.transitLine.name",
    "routes.legs.steps.transitDetails.transitLine.vehicle.type",
])
PARIS = ZoneInfo("Europe/Paris")

# Indices d'un stationnement privé dans l'annonce (attributs ou description).
MOTIF_PARKING = re.compile(
    r"\b(parking|box|garage|place de stationnement|stationnement privé|local (?:moto|vélo|deux[- ]roues))\b",
    re.IGNORECASE,
)


@dataclass
class Trajet:
    """Résumé du trajet domicile → lieu de travail."""

    minutes: int                        # trajet EFFECTIF (meilleur mode, marche pondérée)
    mode: str                           # "transports", "marche" ou "moto"
    source: str                         # "google" ou "vol_oiseau"
    minutes_transports: int | None = None
    minutes_marche: int | None = None
    minutes_moto: int | None = None
    station: str | None = None          # première station empruntée
    marche_station_min: int | None = None
    lignes: list[str] = field(default_factory=list)   # ex. ["M3", "RER A"]
    changements: int | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def parking_prive(annonce) -> bool:
    """True si l'annonce évoque un parking, un box ou un garage."""
    texte = " ".join([annonce.titre, annonce.description,
                      str(annonce.details.get("caracteristiques") or "")])
    return bool(MOTIF_PARKING.search(texte))


def _secondes(duree: str | None) -> int:
    """Convertit une durée Google ("754s") en secondes."""
    return int(duree.rstrip("s")) if duree else 0


def _prochain_mardi(heure: str) -> str:
    """Horodatage RFC 3339 du prochain mardi à l'heure donnée (heure de Paris)."""
    h, m = map(int, heure.split(":"))
    now = datetime.now(PARIS)
    jours = (1 - now.weekday()) % 7 or 7          # 1 = mardi
    cible = (now + timedelta(days=jours)).replace(hour=h, minute=m, second=0, microsecond=0)
    return cible.isoformat()


def _nom_ligne(ligne: dict) -> str:
    """Nom court lisible : "M3", "RER A", "Bus 20"..."""
    court = ligne.get("nameShort") or ligne.get("name") or "?"
    vehicule = (ligne.get("vehicle") or {}).get("type", "")
    if vehicule == "SUBWAY" and not court.upper().startswith("M"):
        return f"M{court}"
    if vehicule == "BUS":
        return f"Bus {court}"
    if vehicule in ("HEAVY_RAIL", "COMMUTER_TRAIN") and len(court) == 1:
        return f"RER {court}"
    return court


def _appeler(corps: dict, champs: str, cle_api: str) -> list[dict]:
    """Appel brut à Google Routes, renvoie la liste des routes."""
    rep = requests.post(URL_ROUTES, json=corps, timeout=20,
                        headers={"X-Goog-Api-Key": cle_api, "X-Goog-FieldMask": champs})
    rep.raise_for_status()
    return rep.json().get("routes") or []


def _points(lat: float, lng: float, bureaux: dict) -> dict:
    """Origine et destination au format Google."""
    return {
        "origin": {"location": {"latLng": {"latitude": lat, "longitude": lng}}},
        "destination": {"location": {"latLng": {
            "latitude": bureaux["lat"], "longitude": bureaux["lng"]}}},
    }


def _transports(lat: float, lng: float, bureaux: dict, cle_api: str) -> dict | None:
    """Itinéraire en transports en commun, résumé (durée, station, lignes)."""
    routes = _appeler({**_points(lat, lng, bureaux), "travelMode": "TRANSIT",
                       "arrivalTime": _prochain_mardi(bureaux.get("arrivee", "09:00")),
                       "languageCode": "fr"}, CHAMPS_TRANSIT, cle_api)
    if not routes:
        return None
    route = routes[0]
    etapes = [e for leg in route.get("legs", []) for e in leg.get("steps", [])]
    transports = [e for e in etapes if e.get("travelMode") == "TRANSIT"]

    # Marche avant la première étape en transport = distance réelle à la station.
    marche = 0
    for etape in etapes:
        if etape.get("travelMode") == "TRANSIT":
            break
        marche += _secondes(etape.get("staticDuration"))

    premiere = transports[0]["transitDetails"] if transports else {}
    return {
        "minutes_transports": round(_secondes(route.get("duration")) / 60),
        "station": ((premiere.get("stopDetails") or {}).get("departureStop") or {}).get("name"),
        "marche_station_min": round(marche / 60) if transports else None,
        "lignes": [_nom_ligne(e["transitDetails"].get("transitLine") or {}) for e in transports],
        "changements": max(len(transports) - 1, 0) if transports else None,
    }


def _duree_simple(lat: float, lng: float, bureaux: dict, cle_api: str, mode: str) -> int | None:
    """Durée en minutes à pied (WALK) ou en voiture à l'heure de pointe (DRIVE)."""
    corps = {**_points(lat, lng, bureaux), "travelMode": mode}
    if mode == "DRIVE":
        corps["routingPreference"] = "TRAFFIC_AWARE"
        corps["departureTime"] = _prochain_mardi(bureaux.get("depart", "08:30"))
    routes = _appeler(corps, "routes.duration", cle_api)
    return round(_secondes(routes[0].get("duration")) / 60) if routes else None


def _effectif(infos: dict, poids_marche: float) -> tuple[int, str]:
    """Choisit le meilleur mode (marche pondérée) : (minutes effectives, mode)."""
    options = []
    if infos.get("minutes_transports") is not None:
        options.append((infos["minutes_transports"], "transports"))
    if infos.get("minutes_marche") is not None:
        options.append((round(infos["minutes_marche"] * poids_marche), "marche"))
    if infos.get("minutes_moto") is not None:
        options.append((infos["minutes_moto"], "moto"))
    return min(options) if options else (999, "transports")


def calculer(annonce, profil: dict, base) -> Trajet:
    """Trajet effectif pour une annonce, avec cache par coordonnées (~10 m).

    Exemple :
        >>> t = calculer(annonce, profil, base)
        >>> t.minutes, t.mode, t.station, t.lignes
    """
    r = profil["recherche"]
    bureaux = r["bureaux"]
    cle_api = os.getenv("GOOGLE_MAPS_API_KEY")
    km = distance_km(annonce.lat, annonce.lng, bureaux["lat"], bureaux["lng"])

    if not cle_api:
        estimation = round(10 + km / 16 * 60)   # ~16 km/h effectifs + 10 min d'accès
        return Trajet(minutes=estimation, mode="transports", source="vol_oiseau",
                      minutes_transports=estimation)

    # La position des bureaux fait partie de la clé : corriger l'adresse recalcule les trajets.
    coords = f"{annonce.lat:.4f},{annonce.lng:.4f}>{bureaux['lat']:.4f},{bureaux['lng']:.4f}"
    infos: dict = {}
    try:
        # Transports (toujours) : en cache par coordonnées.
        cle = f"transit:{coords}"
        transit = base.trajet_en_cache(cle)
        if transit is None:
            transit = _transports(annonce.lat, annonce.lng, bureaux, cle_api) or {}
            base.memoriser_trajet(cle, transit)
        infos.update(transit)

        # Marche : inutile au-delà de la distance où elle ne peut pas gagner.
        if km <= r.get("marche_max_km", 3.6):
            cle = f"walk:{coords}"
            marche = base.trajet_en_cache(cle)
            if marche is None:
                marche = {"minutes_marche": _duree_simple(annonce.lat, annonce.lng, bureaux, cle_api, "WALK")}
                base.memoriser_trajet(cle, marche)
            infos.update(marche)

        # Moto (si le profil l'active) : seulement si l'annonce évoque un stationnement privé.
        if r.get("moto") and parking_prive(annonce):
            cle = f"drive:{coords}"
            moto = base.trajet_en_cache(cle)
            if moto is None:
                moto = {"minutes_moto": _duree_simple(annonce.lat, annonce.lng, bureaux, cle_api, "DRIVE")}
                base.memoriser_trajet(cle, moto)
            infos.update(moto)
    except requests.RequestException as err:
        log.warning("Google Routes en échec (%s), repli à vol d'oiseau", err)
        estimation = round(10 + km / 16 * 60)
        return Trajet(minutes=estimation, mode="transports", source="vol_oiseau",
                      minutes_transports=estimation)

    minutes, mode = _effectif(infos, r.get("poids_minute_marche", 0.67))
    return Trajet(minutes=minutes, mode=mode, source="google", **infos)
