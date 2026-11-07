"""Jugement d'une annonce par Claude, à partir du profil (`profil.yaml`).

Claude n'attribue PAS le score : il évalue chaque critère de `profil.yaml`
(oui / partiel / non / inconnu, avec une preuve tirée de l'annonce), puis
`scoring.py` calcule le score. Claude rédige aussi le résumé, les alertes
(arnaque, incohérences) et le brouillon de premier message.
"""

import json
from typing import Literal

import anthropic
from pydantic import BaseModel, Field

from nest import scoring, trajet as module_trajet

# Sonnet d'abord : on ne passera à un modèle plus gros que sur écart démontré.
MODELE = "claude-sonnet-5"


class Evaluation(BaseModel):
    """Verdict de Claude sur un critère."""

    id: str = Field(description="id du critère tel que dans la liste")
    verdict: Literal["oui", "partiel", "non", "inconnu"]
    preuve: str = Field(description="Indice tiré de l'annonce, 3 à 8 mots ; vide si inconnu")


class Verdict(BaseModel):
    """Ce que Claude rend pour une annonce."""

    resume: str = Field(description="Une phrase : le bien en clair, ex. 'Studio meublé de 22 m² au 4e avec ascenseur, Paris 11e'")
    est_une_offre: bool = Field(description="False si c'est une demande de logement, une annonce vide ou hors sujet")
    alertes: list[str] = Field(description="Arnaque probable, incohérences, frais suspects ; liste vide sinon")
    evaluations: list[Evaluation] = Field(description="Une entrée par critère évalué par Claude")
    station_citee: str | None = Field(description="Station de métro/RER citée dans la description, sinon null")
    risque_arnaque: Literal["faible", "moyen", "eleve"] = Field(description="Risque que l'annonce soit une arnaque, d'après l'annonce et le profil du vendeur")
    duree_max_mois: float | None = Field(description="Durée maximale de location si l'annonce la limite (ex. sous-location de 2 mois, 3 semaines = 0.75, « jusqu'à fin janvier ») ; null si bail sans limite ou non précisé")
    message: str = Field(description="Premier message au bailleur, prêt à envoyer")


def _regles_message(profil: dict) -> str:
    """Consignes du premier message au bailleur (partagées par juger et rediger)."""
    loc = profil["locataire"]
    taire = ["le nom de l'entreprise ou de l'école"] if not loc.get("nommer_employeur") else []
    taire += loc.get("ne_jamais_mentionner") or []
    regle_taire = (f"- Ne mentionne JAMAIS : {' ; '.join(taire)}.\n" if taire else "")
    modele = (loc.get("modele_message") or "").strip()
    exemple = (f"""
Exemple de ton et de mise en forme (à adapter, pas à recopier mot pour mot) :
{modele}""" if modele else "")
    return f"""# Le message au bailleur
Il doit sonner comme écrit par {loc['prenom']}, pas par une IA.
Ce qu'il dit, en quelques phrases simples :
- {loc['prenom']}, {loc['age']} ans, {loc['situation']} ;
- {loc['emploi']} ; le logement est proche de son lieu de travail ;
- garanties : {loc['garanties']} ; arrivée souhaitée : {loc['arrivee']} ;
- demander si le logement est toujours disponible et proposer une visite.
Règles :
- Ne reprends PAS les détails de l'annonce (nom de rue, équipements, exposition, formules de
  l'annonceur) : c'est ce qui trahit un message généré.
- Personnalise uniquement pour répondre à ce que l'annonce demande ou impose (présenter sa
  situation, des dates, des documents, un profil recherché, une date de disponibilité éloignée
  de l'arrivée souhaitée) ; sinon, reste générique.
{regle_taire}- Mise en forme aérée, avec une ligne vide entre chaque bloc : « Bonjour, », puis un paragraphe
  sur sa situation, un paragraphe sur les garanties (revenus, garant), une ligne pour la
  question et la visite, puis « Bonne journée, » et « {loc['prenom']} » sur la ligne suivante.
- Vouvoiement, phrases courtes. Jamais de tiret cadratin, aucune formule creuse ni flatterie.
{exemple}"""


def _systeme(profil: dict) -> str:
    """Prompt système stable (mis en cache) : le profil et les critères du locataire."""
    r = profil["recherche"]
    prenom = profil["locataire"]["prenom"]
    a_evaluer = [c for c in profil.get("criteres", []) if "auto" not in c]
    liste = "\n".join(
        f"- `{c['id']}` ({c['libelle']}) : {c.get('consigne', '')}"
        + (f" [seulement pour : {', '.join(c['applique_a'])}]" if c.get("applique_a") else "")
        for c in a_evaluer)
    return f"""Tu aides {prenom} à trouver un logement à {r['ville']}. Pour chaque annonce, tu évalues
chacun de ses critères et tu rédiges le premier message à envoyer.

# Contexte
- Lieu de travail : {r['bureaux']['nom']}, {r['bureaux']['adresse']}, arrivée {r['bureaux']['arrivee']}.
- Le trajet a déjà été vérifié (seul critère éliminatoire) ; le calcul est fourni avec l'annonce.
- Budget : {r['budget_max_cc']} € charges comprises. Entrée souhaitée : {r['entree_souhaitee']}.

# Ce que cherche {prenom}
{profil['preferences']}

# Critères à évaluer (un verdict par critère)
{liste}

Verdicts : `oui` (clairement satisfait), `partiel`, `non` (clairement pas satisfait),
`inconnu` (l'annonce ne permet pas de savoir : ne devine pas, inconnu est neutre).

# Arnaques (risque_arnaque + alertes)
Schéma fréquent : une FAUSSE AGENCE (compte pro récent, souvent des dizaines d'annonces
très attractives) fait sortir du site d'annonces (mail, autre site), fait remplir un dossier puis
demande quelques euros pour le « créer » ou le « finaliser », avant de prélever bien plus.
En France, aucun frais n'est exigible avant la signature du bail (loi ALUR) : toute demande de
paiement pour un dossier, une réservation ou une visite est une arnaque.
Le profil du vendeur est fourni (`vendeur`) : type, ancienneté, nombre d'annonces, identité
vérifiée. Risque `eleve` si plusieurs signaux convergent (agence de moins d'un an + loyer très
bas pour le quartier + invitation à contacter hors du site, plateforme ou frais de dossier,
propriétaire à l'étranger, paiement avant visite). `moyen` si un signal isolé. `faible` sinon ;
un particulier à l'identité vérifiée inscrit depuis des années est presque toujours `faible`.
Marques déjà signalées comme arnaques (et leurs variantes d'écriture) : risque `eleve`
si l'annonce y renvoie : {", ".join(profil.get("arnaques", {}).get("marques", [])) or "aucune"}.
Signale aussi les frais d'intermédiation et les incohérences (DPE, surface, photos).
Si la position est approximative (`position_precise` false), cherche une station ou une rue
citée dans la description et renseigne `station_citee`.

{_regles_message(profil)}"""


def _contenu(annonce, trajet, vendeur: dict | None = None) -> str:
    """Message utilisateur : l'annonce, son trajet et le profil du vendeur, en JSON lisible."""
    donnees = annonce.to_dict()
    donnees["vendeur"] = vendeur or "inconnu"
    donnees.pop("photos", None)          # inutile pour le jugement
    donnees["trajet_calcule"] = trajet.to_dict()
    donnees["parking_detecte"] = module_trajet.parking_prive(annonce)
    return "Voici l'annonce à évaluer :\n" + json.dumps(donnees, ensure_ascii=False, indent=2)


def juger(annonce, trajet, profil: dict, environs: dict | None = None,
          client: anthropic.Anthropic | None = None):
    """Fait évaluer une annonce par Claude puis calcule le score.

    Renvoie (verdict, score sur 100, détail par critère, usage des tokens).

    Exemple :
        >>> verdict, score, lignes, usage = juger(annonce, trajet, profil, environs)
    """
    client = client or anthropic.Anthropic()
    reponse = client.messages.parse(
        model=MODELE,
        max_tokens=4000,
        system=[{"type": "text", "text": _systeme(profil),
                 "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": _contenu(annonce, trajet, (environs or {}).get("vendeur"))}],
        output_config={"effort": "medium"},
        output_format=Verdict,
    )
    verdict = reponse.parsed_output
    evaluations = {e.id: (e.verdict, e.preuve) for e in verdict.evaluations}
    score, lignes = scoring.calculer(profil.get("criteres", []), evaluations,
                                     annonce, trajet, environs)
    if not verdict.est_une_offre:
        score = 0
    if verdict.risque_arnaque == "eleve":
        score = 0
        verdict.alertes.insert(0, "Arnaque probable : notif supprimée")
    duree_min = profil["recherche"].get("duree_min_mois", 6)
    if verdict.duree_max_mois is not None and verdict.duree_max_mois < duree_min:
        score = 0
        verdict.alertes.insert(0, f"Courte durée : {verdict.duree_max_mois:g} mois max, éliminée")
    return verdict, score, lignes, reponse.usage


# Tarifs Sonnet 5 en $ par million de tokens (entrée, sortie) ; cache : lecture ×0,1, écriture ×1,25.
TARIF_ENTREE, TARIF_SORTIE = 2.0, 10.0


def cout_usd(usage) -> float:
    """Coût d'un appel à partir de l'objet `usage` renvoyé par l'API."""
    entree = (usage.input_tokens
              + 0.1 * (usage.cache_read_input_tokens or 0)
              + 1.25 * (usage.cache_creation_input_tokens or 0))
    return (entree * TARIF_ENTREE + usage.output_tokens * TARIF_SORTIE) / 1_000_000


class Message(BaseModel):
    """Message seul (pour régénérer sans tout rejuger)."""

    message: str = Field(description="Premier message au bailleur, prêt à envoyer")


def rediger(annonce, profil: dict, client: anthropic.Anthropic | None = None):
    """Rédige uniquement le premier message (moins cher qu'un jugement complet).

    Exemple :
        >>> message, usage = rediger(annonce, profil)
    """
    client = client or anthropic.Anthropic()
    donnees = {k: v for k, v in annonce.to_dict().items() if k in ("titre", "description", "type_bien", "disponible_le")}
    reponse = client.messages.parse(
        model=MODELE,
        max_tokens=2000,
        system=[{"type": "text", "cache_control": {"type": "ephemeral"},
                 "text": f"Tu rédiges le premier message de {profil['locataire']['prenom']} "
                         "à un bailleur.\n\n"
                         + _regles_message(profil)}],
        messages=[{"role": "user", "content": "Annonce :\n" + json.dumps(donnees, ensure_ascii=False, indent=2)}],
        output_config={"effort": "low"},
        output_format=Message,
    )
    return reponse.parsed_output.message, reponse.usage
