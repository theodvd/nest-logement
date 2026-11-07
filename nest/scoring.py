"""Score d'une annonce sur 100, critère par critère.

Principe : prioriser sans éliminer. Chaque critère de `profil.yaml` reçoit une
satisfaction entre 0 et 1 (évaluée par Claude, ou mesurée par le code pour les
critères `auto`), puis :

    score = 100 × Σ(poids × satisfaction) / Σ(poids)

Un critère « inconnu » vaut 0,5 : une annonce peu détaillée n'est pas punie.
"""

from dataclasses import dataclass

from nest.filtres import distance_km

VALEURS = {"oui": 1.0, "partiel": 0.5, "inconnu": 0.5, "non": 0.0}


@dataclass
class Ligne:
    """Résultat d'un critère pour une annonce."""

    id: str
    libelle: str
    poids: float
    satisfaction: float
    preuve: str


def _interpoler(valeur: float, paliers: dict) -> float:
    """Interpolation linéaire entre paliers {valeur: satisfaction}, bornée aux extrémités."""
    points = sorted((float(k), float(v)) for k, v in paliers.items())
    if valeur <= points[0][0]:
        return points[0][1]
    if valeur >= points[-1][0]:
        return points[-1][1]
    for (x1, y1), (x2, y2) in zip(points, points[1:]):
        if x1 <= valeur <= x2:
            return y1 + (y2 - y1) * (valeur - x1) / (x2 - x1)
    return 0.5


def mesure_auto(critere: dict, annonce, trajet, environs: dict) -> tuple[float, str]:
    """Satisfaction d'un critère mesuré par le code (surface, trajet, loyer...).

    Types `auto` disponibles :
    - surface_m2, loyer_cc, trajet_min (effectif), changements : paliers ;
    - distance_km : distance à vol d'oiseau jusqu'à `point: {lat, lng}`, paliers ;
    - escalade_km : salle de bloc la plus proche, paliers ;
    - parc : 1 si parc ou square à ~5 min à pied, 0 sinon ;
    - vendeur_particulier : 1 si particulier, 0 si agence ;
    - compte_fiable : 1 si identité de propriétaire vérifiée, sinon ancienneté du compte (paliers, en mois).
    """
    champ = critere["auto"]
    vendeur = environs.get("vendeur") or {}
    if champ == "vendeur_particulier":
        if not vendeur:
            return 0.5, "inconnu"
        return (1.0, vendeur.get("nom") or "particulier") if vendeur["type"] == "particulier" \
            else (0.0, f"agence {vendeur.get('nom') or ''}".strip())
    if champ == "compte_fiable":
        if not vendeur:
            return 0.5, "inconnu"
        if vendeur.get("identite_verifiee"):
            return 1.0, "identité vérifiée"
        if vendeur.get("anciennete_mois") is None:
            return 0.5, "inconnu"
        return (_interpoler(vendeur["anciennete_mois"], critere["paliers"]),
                f"compte de {vendeur['anciennete_mois']:.0f} mois")
    if champ == "parc":
        if "parc" not in environs:          # OpenStreetMap injoignable
            return 0.5, "inconnu"
        parc = environs["parc"]
        return (1.0, parc) if parc else (0.0, "aucun à 5 min")

    sources = {
        "surface_m2": annonce.surface_m2,
        "loyer_cc": annonce.loyer_cc,
        "trajet_min": trajet.minutes,
        "changements": trajet.changements if trajet.mode == "transports" else 0,
        "escalade_km": environs.get("escalade_km"),
    }
    if champ == "distance_km" and annonce.lat is not None:
        p = critere["point"]
        sources["distance_km"] = round(distance_km(annonce.lat, annonce.lng, p["lat"], p["lng"]), 1)
    valeur = sources.get(champ)
    if valeur is None:
        return 0.5, "inconnu"
    preuve = f"{environs.get('salle_escalade')} à {valeur:g} km" if champ == "escalade_km" else f"{valeur:g}"
    return _interpoler(float(valeur), critere["paliers"]), preuve


def s_applique(critere: dict, annonce) -> bool:
    """Un critère `applique_a: [colocation]` n'entre pas dans le score des studios."""
    cibles = critere.get("applique_a")
    return not cibles or annonce.type_bien in cibles


def calculer(criteres: list[dict], evaluations: dict[str, tuple[str, str]],
             annonce, trajet, environs: dict | None = None) -> tuple[int, list[Ligne]]:
    """Calcule le score sur 100 et le détail par critère.

    `evaluations` : {id_critere: (verdict, preuve)} rendu par Claude pour les
    critères non `auto`. Un critère absent des évaluations compte comme inconnu.

    Exemple :
        >>> score, lignes = calculer(profil["criteres"], {"meuble": ("oui", "meublé")}, a, t)
    """
    environs = environs or {}
    lignes: list[Ligne] = []
    for c in criteres:
        if not s_applique(c, annonce):
            continue
        if "auto" in c:
            satisfaction, preuve = mesure_auto(c, annonce, trajet, environs)
        else:
            verdict, preuve = evaluations.get(c["id"], ("inconnu", ""))
            satisfaction = VALEURS.get(verdict, 0.5)
        lignes.append(Ligne(c["id"], c["libelle"], float(c["poids"]), satisfaction, preuve))

    total = sum(l.poids for l in lignes)
    score = round(100 * sum(l.poids * l.satisfaction for l in lignes) / total) if total else 50
    return score, lignes
