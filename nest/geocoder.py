"""Adresse → coordonnées, via Nominatim (OpenStreetMap, gratuit, sans clé).

Sert à l'entretien d'installation : le profil a besoin de la latitude et de la
longitude du lieu de travail. Une requête à la fois, avec un User-Agent explicite
(règle d'usage de Nominatim).

Exemple d'appel :
    python -m nest.geocoder "Place de la République, 75011 Paris"
"""

import json
import sys

import requests

URL = "https://nominatim.openstreetmap.org/search"
ENTETES = {"User-Agent": "nest-veille-logement/0.1 (installation)"}


def geocoder(adresse: str) -> dict | None:
    """Renvoie {"lat", "lng", "libelle"} du meilleur résultat en France, None sinon.

    Exemple :
        >>> geocoder("Place de la République, Paris")["lat"]
        48.867...
    """
    rep = requests.get(URL, params={"q": adresse, "format": "json", "limit": 1, "countrycodes": "fr"},
                       headers=ENTETES, timeout=20)
    rep.raise_for_status()
    resultats = rep.json()
    if not resultats:
        return None
    r = resultats[0]
    return {"lat": round(float(r["lat"]), 5), "lng": round(float(r["lon"]), 5), "libelle": r["display_name"]}


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit('Usage : python -m nest.geocoder "adresse complète"')
    resultat = geocoder(" ".join(sys.argv[1:]))
    if not resultat:
        sys.exit("Adresse introuvable : précise le code postal ou la ville.")
    print(json.dumps(resultat, ensure_ascii=False))
