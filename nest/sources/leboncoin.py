"""Connecteur Leboncoin, via la librairie open source `lbc`.

`lbc` interroge l'API interne de Leboncoin (celle qu'utilise le site) en imitant
un vrai navigateur, ce qui passe la protection anti-bot. Accès non officiel :
on reste sur un rythme modéré (un passage toutes les 10 minutes au plus).
"""

import logging
import time

import lbc

from nest.annonce import Annonce

log = logging.getLogger(__name__)

# Codes Leboncoin du champ "real_estate_type" : 1 = maison, 2 = appartement.
# Exclut parkings, caves, terrains qui polluent la catégorie "Locations".
TYPES_LOGEMENT = ["1", "2"]


def _attr(ad, cle: str, libelle: bool = False):
    """Lit un attribut d'annonce (valeur brute ou libellé), None s'il est absent."""
    attribut = ad.attributes.get(cle) if ad.attributes else None
    if attribut is None:
        return None
    return attribut.value_label if libelle else attribut.value


def _nombre(valeur) -> float | None:
    """Convertit une valeur texte en nombre, None si vide ou invalide."""
    try:
        return float(valeur) if valeur not in (None, "") else None
    except ValueError:
        return None


def _convertir(ad, type_bien: str) -> Annonce:
    """Transforme une annonce `lbc` en Annonce commune."""
    meuble = _attr(ad, "furnished")
    pieces = _nombre(_attr(ad, "rooms"))
    loc = ad.location
    return Annonce(
        source="leboncoin",
        id_source=str(ad.id),
        url=ad.url,
        titre=ad.subject or "",
        description=ad.body or "",
        type_bien=type_bien,
        loyer_cc=ad.price,
        loyer_hc=_nombre(_attr(ad, "rent_excluding_charges")),
        charges=_nombre(_attr(ad, "monthly_charges")),
        surface_m2=_nombre(_attr(ad, "square")),
        pieces=int(pieces) if pieces else None,
        meuble={"1": True, "2": False}.get(meuble),
        lat=loc.lat if loc else None,
        lng=loc.lng if loc else None,
        # "address" = adresse saisie par l'annonceur ; sinon centre de la commune.
        position_precise=bool(loc and loc.source == "address"),
        lieu=(loc.city_label if loc else "") or "",
        publiee_le=ad.first_publication_date or "",
        disponible_le=_attr(ad, "available_date"),
        photos=list(ad.images or []),
        details={
            "etage": _attr(ad, "floor_display"),
            "ascenseur": _attr(ad, "elevator", libelle=True),
            "dpe": _attr(ad, "energy_rate", libelle=True),
            "ges": _attr(ad, "ges", libelle=True),
            "chauffage": _attr(ad, "heating_mode", libelle=True),
            "caracteristiques": _attr(ad, "specificities", libelle=True),
            "encadrement_loyers": _attr(ad, "is_public_rent_control", libelle=True),
            "charges_incluses": _attr(ad, "charges_included", libelle=True),
        },
        vendeur_id=getattr(ad, "_user_id", None),
    )


def chercher(profil: dict, pages: int = 1, pause_s: float = 1.5,
             depuis: str | None = None, pages_max: int = 15) -> list[Annonce]:
    """Récupère les annonces les plus récentes correspondant au profil.

    `pages` > 1 sert au rejeu : on pagine (100 annonces par page) jusqu'à ce
    que Leboncoin ne renvoie plus rien, avec une pause entre deux pages.

    `depuis` ("AAAA-MM-JJ HH:MM:SS") sert au rattrapage après un trou : chaque
    catégorie est paginée jusqu'à ce qu'au moins 90 % d'une page soit plus ancien
    que cette date (au plus `pages_max` pages, garde-fou anti-bot). Pas de test sur
    une seule annonce : Leboncoin mêle des annonces anciennes remontées un peu
    partout dans les pages (constaté le 27/10).

    Exemple :
        >>> annonces = chercher(charger_profil())
        >>> annonces[0].titre
    """
    recherche = profil["recherche"]
    bureaux = recherche["bureaux"]
    budget = int(recherche["budget_max_cc"])
    zone = lbc.City(
        lat=bureaux["lat"],
        lng=bureaux["lng"],
        radius=int(recherche["rayon_recherche_km"] * 1000),
        city=recherche["ville"],   # simple libellé : la zone, c'est le rayon autour du lieu de travail
    )
    client = lbc.Client()
    annonces: list[Annonce] = []

    if depuis:
        pages = pages_max
    limite = 50 if pages == 1 else 100
    requetes = []
    if "studio_t1" in recherche["types"]:
        requetes.append(("studio_t1", dict(category=lbc.Category.IMMOBILIER_LOCATIONS,
                                           rooms=[1, 1], real_estate_type=TYPES_LOGEMENT)))
    if "colocation" in recherche["types"]:
        requetes.append(("colocation", dict(category=lbc.Category.IMMOBILIER_COLOCATIONS)))

    for type_bien, filtres in requetes:
        for page in range(1, pages + 1):
            res = client.search(sort=lbc.Sort.NEWEST, locations=[zone], limit=limite,
                                page=page, price=[0, budget], **filtres)
            lues = [_convertir(ad, type_bien) for ad in res.ads]
            annonces += lues
            if len(res.ads) < limite:
                break
            anciennes = sum(a.publiee_le < depuis for a in lues) if depuis else 0
            if depuis and anciennes >= 0.9 * len(lues):
                log.info("Rattrapage %s : %d page(s) lues, %d/%d annonces de la dernière avant le %s",
                         type_bien, page, anciennes, len(lues), depuis)
                break
            if depuis and page == pages:
                log.warning("Rattrapage %s : plafond de %d pages atteint avant le %s, "
                            "des annonces plus anciennes peuvent manquer", type_bien, pages, depuis)
            if pages > 1:
                time.sleep(pause_s)

    log.info("Leboncoin : %d annonces récupérées", len(annonces))
    return annonces
