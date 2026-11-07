"""Ce qu'il y a autour d'une annonce, d'après OpenStreetMap (gratuit, API Overpass).

Calculé seulement si le profil a un critère qui s'en sert (`auto: parc` ou
`auto: escalade_km`) ; les cartes sont téléchargées autour du centre de la zone
(`recherche.centre` du profil, sinon le lieu de travail).

- Parc ou square à moins de ~5 min à pied : carte de tous les parcs téléchargée une
  fois (≈ 5 Mo pour Paris, rafraîchie tous les 30 jours), indexée par carrés de ~500 m.
- Salle de bloc la plus proche (liste téléchargée une fois, rafraîchie tous les 30 jours).

Exemple :
    >>> environs = analyser(annonce, base, profil)
    >>> environs["parc"], environs["escalade_km"], environs["salle_escalade"]
"""

import json
import logging
import re
import time
from datetime import datetime, timedelta
from pathlib import Path

import requests

from nest.filtres import distance_km

log = logging.getLogger(__name__)

# Serveur principal puis miroir : l'API publique renvoie parfois des 502/504 passagers.
URLS_OVERPASS = ["https://overpass-api.de/api/interpreter",
                 "https://overpass.kumi.systems/api/interpreter"]
ENTETES = {"User-Agent": "nest-veille-logement/0.1 (usage personnel)"}
DOSSIER = Path(__file__).resolve().parent.parent / "data"
CASE = 0.005                       # taille d'une case de l'index (~500 m en latitude)
_INDEX_PARCS: dict | None = None   # chargé une seule fois par processus

# Salles de bloc ouvertes au public : attribut bloc OU grande enseigne connue.
# Exclut gymnases municipaux et murs de clubs (adhésion annuelle, créneaux limités).
ENSEIGNES = re.compile(
    r"arkose|climbing district|block'?out|blocbuster|antrebloc|vertical'?art|climb up|"
    r"hardbloc|murmur|le triangle|bloc session|grimper|bloc",
    re.IGNORECASE,
)


def _overpass(requete: str) -> list[dict]:
    """Exécute une requête Overpass (2 essais par serveur) et renvoie les éléments."""
    derniere_erreur = None
    for url in URLS_OVERPASS:
        for essai in range(2):
            try:
                rep = requests.post(url, data={"data": requete}, headers=ENTETES, timeout=240)
                rep.raise_for_status()
                return rep.json().get("elements", [])
            except requests.RequestException as err:
                derniere_erreur = err
                log.info("Overpass %s, essai %d en échec : %s", url, essai + 1, err)
                time.sleep(10)
    raise derniere_erreur


def centre(profil: dict) -> tuple[float, float]:
    """Centre de la zone de recherche : `recherche.centre`, sinon le lieu de travail."""
    r = profil["recherche"]
    point = r.get("centre") or r["bureaux"]
    return float(point["lat"]), float(point["lng"])


def _fichier(nom: str, point: tuple[float, float]) -> Path:
    """Fichier de cache propre à une zone (changer de ville ne relit pas l'ancienne carte)."""
    return DOSSIER / f"{nom}_{point[0]:.2f}_{point[1]:.2f}.json"


def _salles(point: tuple[float, float]) -> list[dict]:
    """Liste des salles de bloc à 25 km du centre (cache fichier, 30 jours)."""
    fichier = _fichier("salles_escalade", point)
    if _frais(fichier):
        return json.loads(fichier.read_text())

    lat, lng = point
    elements = _overpass(f'[out:json][timeout:60];nwr["sport"~"climbing"]'
                         f'(around:25000,{lat},{lng});out center tags;')
    salles = []
    for e in elements:
        tags = e.get("tags", {})
        nom = tags.get("name")
        if not nom or "gymnase" in nom.lower() or "collège" in nom.lower():
            continue
        if tags.get("climbing:boulder") == "yes" or ENSEIGNES.search(nom):
            centre = e.get("center") or e
            salles.append({"nom": nom, "lat": centre["lat"], "lng": centre["lon"]})

    DOSSIER.mkdir(parents=True, exist_ok=True)
    fichier.write_text(json.dumps(salles, ensure_ascii=False, indent=1))
    log.info("Salles d'escalade mises à jour : %d", len(salles))
    return salles


def _frais(fichier: Path) -> bool:
    """True si le fichier de cache existe et a moins de 30 jours."""
    if not fichier.exists():
        return False
    age = datetime.now() - datetime.fromtimestamp(fichier.stat().st_mtime)
    return age < timedelta(days=30)


def _index_parcs(point: tuple[float, float]) -> dict:
    """Index {case: [(lat, lng, nom), ...]} des contours de tous les parcs proches."""
    global _INDEX_PARCS
    if _INDEX_PARCS is not None:
        return _INDEX_PARCS

    fichier = _fichier("parcs", point)
    if _frais(fichier):
        points = json.loads(fichier.read_text())
    else:
        lat, lng = point
        elements = _overpass(f'[out:json][timeout:120];nwr["leisure"~"^(park|dog_park)$"]'
                             f'(around:20000,{lat},{lng});out geom;')
        points = []
        for e in elements:
            nom = e.get("tags", {}).get("name") or "espace vert sans nom"
            if e["type"] == "node":
                points.append([e["lat"], e["lon"], nom])
            sommets = e.get("geometry", []) + [g for m in e.get("members", []) for g in m.get("geometry", [])]
            points += [[g["lat"], g["lon"], nom] for g in sommets]
        DOSSIER.mkdir(parents=True, exist_ok=True)
        fichier.write_text(json.dumps(points, ensure_ascii=False))
        log.info("Carte des parcs mise à jour : %d sommets", len(points))

    index: dict = {}
    for plat, plng, nom in points:
        index.setdefault((int(plat / CASE), int(plng / CASE)), []).append((plat, plng, nom))
    _INDEX_PARCS = index
    return index


def _parc_proche(lat: float, lng: float, rayon_m: int, point: tuple[float, float]) -> str | None:
    """Nom du parc / square le plus proche dans le rayon (un nommé de préférence), sinon None."""
    index = _index_parcs(point)
    ci, cj = int(lat / CASE), int(lng / CASE)
    dans_rayon = {}
    for di in (-1, 0, 1):
        for dj in (-1, 0, 1):
            for plat, plng, nom in index.get((ci + di, cj + dj), []):
                d = distance_km(lat, lng, plat, plng)
                if d <= rayon_m / 1000 and d < dans_rayon.get(nom, 99):
                    dans_rayon[nom] = d
    if not dans_rayon:
        return None
    nommes = {n: d for n, d in dans_rayon.items() if n != "espace vert sans nom"}
    return min(nommes or dans_rayon, key=(nommes or dans_rayon).get)


def besoins(profil: dict) -> set[str]:
    """Mesures d'environs utilisées par les critères du profil ({"parc", "escalade_km"} au plus)."""
    return {c.get("auto") for c in profil.get("criteres", [])} & {"parc", "escalade_km"}


def analyser(annonce, base, profil: dict, rayon_parc_m: int = 400) -> dict:
    """Parc proche et salle d'escalade la plus proche (en cache par coordonnées).

    Seules les mesures demandées par le profil sont calculées : une clé absente
    du résultat vaut « inconnu » (neutre) dans le score.
    """
    utiles = besoins(profil)
    if not utiles or annonce.lat is None:
        return {}
    cle = f"environs:{'+'.join(sorted(utiles))}:{annonce.lat:.4f},{annonce.lng:.4f}"
    if (en_cache := base.trajet_en_cache(cle)) is not None:
        return en_cache

    point = centre(profil)
    resultat: dict = {}
    try:
        if "parc" in utiles:
            resultat["parc"] = _parc_proche(annonce.lat, annonce.lng, rayon_parc_m, point)
        if "escalade_km" in utiles and (salles := _salles(point)):
            proche = min(salles, key=lambda s: distance_km(annonce.lat, annonce.lng, s["lat"], s["lng"]))
            resultat["salle_escalade"] = proche["nom"]
            resultat["escalade_km"] = round(
                distance_km(annonce.lat, annonce.lng, proche["lat"], proche["lng"]), 2)
    except requests.RequestException as err:
        log.warning("Overpass en échec (%s) : environs inconnus pour cette annonce", err)
        return {}                # vide = inconnu (neutre) ; pas de cache, on retentera

    base.memoriser_trajet(cle, resultat)
    return resultat
