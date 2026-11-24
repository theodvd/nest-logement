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

from nest import claude, recherches, scoring, trajet as module_trajet

# Sonnet d'abord : on ne passera à un modèle plus gros que sur écart démontré.
MODELE = "claude-sonnet-5"

# Rempli AVANT le message (les champs sont générés dans l'ordre) : Claude liste d'abord ce
# que l'annonce exige, puis y répond. Sans cette étape, il sautait des questions.
DESCRIPTION_EXIGENCES = (
    "Ce que l'annonce demande de dire dans le premier message, ou impose au candidat "
    "(questions, garantie exigée, pièces, contraintes de contact ou de visite), un point par "
    "entrée, dans l'ordre de l'annonce ; liste vide si elle ne demande rien")
DESCRIPTION_A_COMPLETER = (
    "Les exigences de l'annonce auxquelles le message ne peut PAS répondre faute d'information "
    "dans le profil, ou que le locataire ne remplit pas (ex. « Visale exigée », « revenus du "
    "garant »), 2 à 6 mots chacune ; liste vide si le message couvre tout. N'y mets jamais une "
    "exigence que les faits du profil couvrent : chaque fausse alerte fait perdre du temps")


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
    exigences_annonce: list[str] = Field(description=DESCRIPTION_EXIGENCES)
    message: str = Field(description="Premier message au bailleur, prêt à envoyer")
    a_completer: list[str] = Field(description=DESCRIPTION_A_COMPLETER)


class VerdictGroupe(Verdict):
    """Recherche à plusieurs (coloc entre amis, 23/11) : chambres et loyer pour le groupe."""

    chambres_pour_le_groupe: int | None = Field(description=(
        "Nombre de chambres que le groupe d'amis pourrait occuper : toutes les chambres d'un "
        "logement entier (un séjour ou un bureau ne compte pas), ou les chambres LIBRES d'une "
        "colocation ; null si l'annonce ne permet pas de le savoir"))
    loyer_total_groupe_cc: float | None = Field(description=(
        "Loyer total charges comprises pour ces chambres : le loyer du logement entier, ou la "
        "somme des loyers des chambres libres d'une colocation ; null si inconnu"))


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
    # Faits facultatifs, cités seulement si l'annonce les demande. Une recherche à plusieurs
    # a ses propres critères (« logement à partager »), pas ceux de la recherche solo.
    facultatifs = ""
    criteres = ((profil.get("groupe") or {}).get("criteres_recherche")
                or loc.get("criteres_recherche") or "").strip()
    if criteres:
        facultatifs += f"- critères de recherche (seulement si l'annonce les demande) : {criteres} ;\n"
    if historique := (loc.get("historique_location") or "").strip():
        facultatifs += ("- logement actuel et passé (seulement si l'annonce le demande) : "
                        f"{historique} ;\n")
    if groupe := profil.get("groupe"):
        return _regles_message_groupe(profil, groupe, facultatifs, regle_taire)
    return f"""# Le message au bailleur
Il doit sonner comme écrit par {loc['prenom']}, pas par une IA.

## D'abord : ce que l'annonce exige (`exigences_annonce`)
Beaucoup d'annonces disent quoi écrire dans le premier message (« merci de préciser… ») ou
imposent une condition (garantie Visale, pièces, pas d'appel, visites à partir d'une date).
Un message qui saute une de ces questions est écarté sans être lu. Donc :
1. Relève chaque exigence dans `exigences_annonce`, dans l'ordre de l'annonce.
2. Réponds à CHACUNE dans le message, avec les faits ci-dessous, de façon visible (une phrase
   par question, dans l'ordre de l'annonce quand c'est naturel).
3. Respecte les contraintes de contact : si les visites ne commencent qu'à une date, propose
   une visite à partir de cette date.
4. Si un fait manque pour répondre, ou si {loc['prenom']} ne remplit pas une condition : n'invente
   rien, ne prétends pas la remplir, et note-la dans `a_completer`.

## Les faits (la seule matière autorisée)
- {loc['prenom']}, {loc['age']} ans, {loc['situation']} ;
- {loc['emploi']} ; le logement est proche de son lieu de travail ;
- garanties : {loc['garanties']} ; arrivée souhaitée : {loc['arrivee']} ;
{facultatifs}- demander si le logement est toujours disponible et proposer une visite.

## Règles
- N'invente JAMAIS un fait absent de cette liste (revenus, garant, logement actuel, pièces).
- Ne reprends PAS les détails de l'annonce (nom de rue, équipements, exposition, formules de
  l'annonceur) : c'est ce qui trahit un message généré.
- Hors exigences de l'annonce, reste générique (une date de disponibilité éloignée de
  l'arrivée souhaitée mérite tout de même un mot).
{regle_taire}- Mise en forme aérée, avec une ligne vide entre chaque bloc : « Bonjour, », puis un paragraphe
  sur sa situation, un paragraphe sur les garanties (revenus, garant), une ligne pour la
  question et la visite, puis « Bonne journée, » et « {loc['prenom']} » sur la ligne suivante.
- Vouvoiement, phrases courtes. Jamais de tiret cadratin, aucune formule creuse ni flatterie.
{exemple}"""


def _faits_amis(groupe: dict) -> str:
    """Faits sur les amis, seulement ceux renseignés dans le profil (rien n'est inventé)."""
    lignes = []
    for ami in groupe.get("amis") or []:
        prenom = (ami.get("prenom") or "").strip()
        morceaux = [(ami.get(k) or "").strip() for k in ("situation", "emploi")]
        if not prenom or not any(morceaux):
            continue
        garants = (ami.get("garants") or "").strip()
        lignes.append(f"- {prenom} : " + " ; ".join(m for m in morceaux if m)
                      + (f" ; garants : {garants} ;" if garants else
                         f" ; garants inconnus : n'en dis rien et mets « garants de {prenom} » dans "
                         "`a_completer` ;"))
    if not lignes:
        return ("- la situation de ses amis n'est pas encore renseignée : n'en dis rien au-delà de la "
                "présentation du groupe, et mets « situation et dossier de mes amis » dans "
                "`a_completer` ;\n")
    return "\n".join(lignes) + "\n"


def _regles_message_groupe(profil: dict, groupe: dict, facultatifs: str, regle_taire: str) -> str:
    """Consignes du premier message pour une recherche à plusieurs (coloc entre amis).

    Même exigence que le message solo (complet dès le premier envoi), mais au nom du groupe :
    présentation commune, puis la situation de l'auteur, puis celle des amis quand elle est connue.
    """
    loc = profil["locataire"]
    p = loc["prenom"]
    return f"""# Le message au bailleur (recherche à plusieurs)
{p} écrit au nom d'un petit groupe d'amis qui cherche à louer ensemble. Le message doit sonner
comme écrit par {p}, pas par une IA. Le bailleur trie des centaines de demandes sans poser de
questions : le message est complet, mais reste concis.

## D'abord : ce que l'annonce exige (`exigences_annonce`)
Relève chaque exigence de l'annonce (questions, garantie exigée, pièces, contraintes de contact
ou de visite) dans `exigences_annonce`, puis réponds à CHACUNE dans le message avec les faits
ci-dessous. Ce que les faits ne couvrent pas (situation des amis, Visale) va dans `a_completer` :
n'invente rien, ne prétends pas remplir une condition.

## Les faits (la seule matière autorisée)
- le groupe : {groupe.get('presentation', '').strip()} ;
- taille du groupe selon le logement : autant de personnes que de chambres pour le groupe,
  {p} compris ; les amis sont pris dans l'ordre de la liste ci-dessous (2 chambres = {p} et le
  premier, 3 = {p} et les deux premiers). Ne cite que les amis du groupe retenu, par leur
  prénom. S'il manque des amis dans la liste pour remplir les chambres, écris « un autre ami »
  et mets « situation du dernier ami » dans `a_completer` ;
- arrivée souhaitée : {loc['arrivee']} ;
- {p}, {loc['age']} ans, {loc['situation']} ; {loc['emploi']} ;
- garanties de {p} : {loc['garanties']} ;
{facultatifs}{_faits_amis(groupe)}- demander si le logement est toujours disponible et proposer une visite.

## Règles
- N'invente JAMAIS un fait absent de cette liste (revenus, garants ou dossier des amis, Visale).
- Ne reprends PAS les détails de l'annonce (nom de rue, équipements, formules de l'annonceur).
{regle_taire}- Mise en forme aérée, une ligne vide entre chaque bloc : « Bonjour, », le groupe, la situation
  de {p} (revenus, garanties), celle des amis si elle est connue, la question et la visite,
  puis « Bonne journée, » et « {p} » sur la ligne suivante.
- Vouvoiement, phrases courtes, « nous » pour le groupe et « je » pour {p}. Jamais de tiret
  cadratin, aucune formule creuse ni flatterie.

Exemple de structure (à adapter, pas à recopier mot pour mot) :
Bonjour,

Nous sommes deux amis, [prénom de l'ami] et moi, à la recherche d'un logement à partager.
[Présentation du groupe.]

Pour ma part, [situation et emploi de {p}, revenus].

[Garanties de {p}.]

[Une phrase par ami du groupe : sa situation et ses revenus.]

Le logement est-il toujours disponible ? Nous serions ravis de le visiter.

Bonne journée,
{p}"""


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
- {_ligne_budget(profil)} Entrée souhaitée : {r['entree_souhaitee']}.

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


def _ligne_budget(profil: dict) -> str:
    """Budget lisible pour Claude : plafond simple, ou selon le nombre de chambres (groupe)."""
    groupe = profil.get("groupe")
    if not groupe:
        return f"Budget : {profil['recherche']['budget_max_cc']} € charges comprises."
    paliers = ", ".join(f"{k} chambres : {float(v):.0f} €"
                        for k, v in sorted(groupe["budget_par_chambres"].items(), key=lambda kv: int(kv[0])))
    return (f"Recherche à plusieurs : le budget est le loyer TOTAL charges comprises pour le groupe, "
            f"pas le prix par chambre ({paliers} ; au-delà, le plus haut palier). Il faut au moins "
            f"{groupe.get('chambres_min', 2)} chambres pour le groupe : renseigne "
            f"`chambres_pour_le_groupe` et `loyer_total_groupe_cc`, le code applique la règle.")


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
    groupe = profil.get("groupe")
    verdict, usage = claude.parse(
        modele=MODELE, systeme=_systeme(profil),
        contenu=_contenu(annonce, trajet, (environs or {}).get("vendeur")),
        schema=VerdictGroupe if groupe else Verdict, effort="medium", max_tokens=4000, client=client)
    evaluations = {e.id: (e.verdict, e.preuve) for e in verdict.evaluations}
    if groupe:
        environs = {**(environs or {}), "chambres_groupe": verdict.chambres_pour_le_groupe,
                    "loyer_groupe_cc": verdict.loyer_total_groupe_cc}
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
    if groupe and (raison := ecart_groupe(verdict, groupe)):
        score = 0
        verdict.alertes.insert(0, raison)
    return verdict, score, lignes, usage


def ecart_groupe(verdict, groupe: dict) -> str | None:
    """Règles de la coloc entre amis : assez de chambres, loyer total sous le plafond.

    Un nombre de chambres ou un loyer inconnu laisse passer (dans le doute, on notifie).

    Exemple :
        >>> ecart_groupe(v, {"chambres_min": 2, "budget_par_chambres": {2: 2000, 3: 3000}})
        'Loyer pour le groupe : 2 300 € pour 2 chambres (plafond 2 000 €), éliminée'
    """
    chambres, total = verdict.chambres_pour_le_groupe, verdict.loyer_total_groupe_cc
    if chambres is None:
        return None
    if chambres < groupe.get("chambres_min", 2):
        return f"Seulement {chambres} chambre(s) pour le groupe, éliminée"
    plafond = recherches.plafond_groupe(chambres, groupe)
    if total and plafond and total > plafond:
        euros = lambda x: f"{x:,.0f}".replace(",", " ")
        return f"Loyer pour le groupe : {euros(total)} € pour {chambres} chambres (plafond {euros(plafond)} €), éliminée"
    return None


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

    exigences_annonce: list[str] = Field(description=DESCRIPTION_EXIGENCES)
    message: str = Field(description="Premier message au bailleur, prêt à envoyer")
    a_completer: list[str] = Field(description=DESCRIPTION_A_COMPLETER)


def rediger(annonce, profil: dict, client: anthropic.Anthropic | None = None):
    """Rédige uniquement le premier message (moins cher qu'un jugement complet).

    Exemple :
        >>> message, usage = rediger(annonce, profil)
    """
    donnees = {k: v for k, v in annonce.to_dict().items() if k in ("titre", "description", "type_bien", "disponible_le")}
    resultat, usage = claude.parse(
        modele=MODELE,
        systeme=f"Tu rédiges le premier message de {profil['locataire']['prenom']} à un bailleur.\n\n"
                + _regles_message(profil),
        contenu="Annonce :\n" + json.dumps(donnees, ensure_ascii=False, indent=2),
        schema=Message, effort="low", max_tokens=2000, client=client)
    return resultat.message, usage
