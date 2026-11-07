"""Tri des mails : préfiltre gratuit, classement par Claude, signaux d'arnaque.

Trois étages, du moins cher au plus cher :
1. `concerne_logement` : mots-clés et expéditeurs connus. Les newsletters,
   alertes bancaires, etc. s'arrêtent ici sans appel à Claude.
2. `classer` : Claude lit le mail et rend une sortie structurée (catégorie,
   bien concerné, pièces demandées, créneaux de visite, signaux d'arnaque).
3. `signaux_code` : règles fixes, indépendantes de Claude. Le risque final est
   le plus élevé des deux : le code peut relever le risque, jamais l'abaisser.

Règle de sécurité de nest : il ne dépose JAMAIS de pièces sur une plateforme
tierce. Une demande de dossier hors DossierFacile est toujours signalée.
"""

import base64
import json
import re
from typing import Literal
from urllib.parse import parse_qs, urlparse

import anthropic
from pydantic import BaseModel, Field

from nest.jugement import MODELE

Categorie = Literal["demande_dossier", "proposition_visite", "question", "refus",
                    "relance_agence", "confirmation", "autre_logement", "hors_sujet"]
Risque = Literal["faible", "moyen", "eleve"]
ORDRE_RISQUE = {"faible": 0, "moyen": 1, "eleve": 2}

# Vocabulaire de la recherche de logement (préfiltre, en minuscules, sans accents).
MOTS_LOGEMENT = re.compile(
    r"\b(logement|appartement|appart|studio|t1|f1|t2|colocation|coloc|chambre|location|louer|"
    r"loyer|bail|locataire|bailleur|proprietaire|garant|garantie|dossier|visite|visiter|"
    r"agence immobiliere|immobilier|annonce|m2|charges comprises|cc|caution|depot de garantie|"
    r"dossierfacile|visale|etat des lieux)\b")

# Expéditeurs toujours examinés (le préfiltre ne doit pas les manquer).
DOMAINES_LOGEMENT = ("leboncoin.fr", "dossierfacile.logement.gouv.fr", "seloger.com", "bienici.com",
                     "pap.fr", "gensdeconfiance.com", "logic-immo.com", "lacartedescolocs.fr")

# Notifications de sécurité ou de compte : jamais une réponse de bailleur.
SUJETS_IGNORES = re.compile(r"nouvel appareil|mot de passe|code de (connexion|v[ée]rification)|"
                            r"confirmez votre (adresse|compte)", re.IGNORECASE)

# Plateformes de dossier reconnues comme sûres (on peut y envoyer un lien, pas des pièces).
PLATEFORMES_SURES = ("dossierfacile.logement.gouv.fr", "dossierfacile.fr", "visale.fr")

# Moyens de paiement et scénarios typiques d'arnaque à la location.
MOTS_ARNAQUE = {
    r"mandat cash|western union|moneygram|transcash|\bpcs\b|neosurf|coupon": "paiement intraçable demandé",
    r"(a|à) l.?etranger|expatri|mut[ée] (a|à)|en mission": "propriétaire à l'étranger",
    r"airbnb|booking\.com|cl[ée]s? (par|envoy[ée]es? par) (la )?poste|remise des cl[ée]s (a|à) distance":
        "remise des clés à distance",
    r"(virement|paiement|acompte|r[ée]servation).{0,60}avant (la |toute )?visite": "paiement avant visite",
}

PIECES_SENSIBLES = re.compile(r"pi[eè]ce d.identit[ée]|passeport|carte d.identit[ée]|avis d.imposition|"
                              r"bulletins? de (salaire|paie)|rib\b", re.IGNORECASE)


def _sans_accents(texte: str) -> str:
    """Minuscules sans accents, pour des mots-clés simples et robustes."""
    table = str.maketrans("àâäéèêëîïôöùûüç", "aaaeeeeiioouuuc")
    return texte.lower().translate(table)


def concerne_logement(mail, domaines_connus: set[str] = frozenset()) -> bool:
    """Préfiltre gratuit : True si le mail mérite d'être lu par Claude.

    `domaines_connus` = domaines des interlocuteurs déjà suivis (une agence qui
    répond « ok pour jeudi » sans aucun mot-clé doit quand même passer).
    """
    if SUJETS_IGNORES.search(mail.sujet):
        return False
    if mail.domaine in domaines_connus or mail.domaine.endswith(DOMAINES_LOGEMENT):
        return True
    texte = _sans_accents(mail.sujet + " " + mail.corps[:3000])
    return len(set(MOTS_LOGEMENT.findall(texte))) >= 2   # deux mots distincts, pas un seul « annonce »


class Classement(BaseModel):
    """Ce que Claude rend pour un mail."""

    concerne_logement: bool = Field(description="False si le mail n'a rien à voir avec la recherche de logement du locataire")
    categorie: Categorie = Field(description=(
        "demande_dossier : on demande au locataire son dossier ou des pièces ; "
        "proposition_visite : un ou plusieurs créneaux de visite proposés ; "
        "question : on attend une réponse du locataire sur autre chose ; "
        "refus : le bien est loué ou la candidature n'est pas retenue ; "
        "relance_agence : l'agence relance sans rien de nouveau ; "
        "confirmation : accusé de réception, visite ou dossier confirmés ; "
        "autre_logement : lié au logement mais rien de tout cela ; "
        "hors_sujet : sans rapport"))
    resume: str = Field(description="Une phrase, factuelle : ce que dit le mail")
    interlocuteur: str | None = Field(description="Nom de la personne qui écrit, si donné")
    agence: str | None = Field(description="Nom de l'agence ou « particulier »")
    bien: str | None = Field(description="Le bien en clair, ex. 'Studio 14 m², Paris 75011, 660 € CC' ; null si non précisé")
    surface_m2: float | None
    loyer_cc: float | None = Field(description="Loyer charges comprises en €, si donné")
    code_postal: str | None
    pieces_demandees: list[str] = Field(description="Pièces ou informations demandées au locataire ; vide sinon")
    plateforme_dossier: str | None = Field(description="URL où l'on demande de déposer le dossier, si c'est le cas")
    creneaux_visite: list[str] = Field(description="Créneaux proposés, tels qu'écrits ; vide sinon")
    signaux_arnaque: list[str] = Field(description="Indices concrets d'arnaque ou de collecte de dossiers ; vide sinon")
    risque_arnaque: Risque
    action_a_faire: str = Field(description="Ce que le locataire doit faire, en une phrase courte ; « rien » si rien")


TYPES_LISIBLES = {"studio_t1": "un studio ou un T1", "colocation": "une colocation"}


def systeme(profil: dict) -> str:
    """Prompt système du tri, construit à partir du profil (stable, donc mis en cache)."""
    r, loc = profil["recherche"], profil["locataire"]
    prenom = loc["prenom"]
    types = " ou ".join(TYPES_LISIBLES.get(t, t) for t in r["types"])
    return f"""Tu tries les mails que reçoit {prenom} pendant sa recherche de logement à {r['ville']}.
{prenom} écrit aux bailleurs sur des sites d'annonces ; certains répondent par mail (agences surtout).
Pour chaque mail, rends un classement structuré.

# Contexte
- {prenom}, {loc['age']} ans, {loc['situation']}, cherche {types} pour {r['entree_souhaitee']},
  budget {r['budget_max_cc']} € CC.
- Son dossier sera sur DossierFacile (plateforme de l'État) : c'est le seul endroit sûr.

# Arnaques à repérer (sois concret, cite ce que tu vois)
- Collecte de dossiers : on presse {prenom} de déposer pièce d'identité, avis d'imposition,
  bulletins de salaire sur une plateforme inconnue, sans visite, avec un mail type envoyé
  en masse, parfois pour plusieurs biens différents en même temps.
- Paiement avant visite, mandat cash, coupons PCS/Transcash, Western Union.
- Propriétaire à l'étranger, clés envoyées par la poste, passage par « Airbnb » ou un
  faux tiers de confiance.
- Urgence artificielle (« les premiers dossiers complets gagnent »), loyer très bas pour
  le quartier, interlocuteur qui refuse la visite.
risque_arnaque : `eleve` si plusieurs signaux nets, `moyen` si un signal, `faible` sinon.
Une agence réelle qui demande DossierFacile ou propose une visite est `faible`."""


def _contenu(mail) -> str:
    """Message utilisateur : le mail à classer, en JSON lisible."""
    return "Mail à classer :\n" + json.dumps({
        "de": f"{mail.nom_expediteur} <{mail.expediteur}>",
        "date": mail.date,
        "sujet": mail.sujet,
        "envoi_en_masse": mail.envoi_en_masse,
        "pieces_jointes": mail.pieces_jointes,
        "corps": mail.corps,
    }, ensure_ascii=False, indent=2)


def classer(mail, profil: dict, client: anthropic.Anthropic | None = None):
    """Fait classer un mail par Claude.

    Renvoie (classement, usage des tokens).

    Exemple :
        >>> classement, usage = classer(mail, profil)
        >>> classement.categorie, classement.risque_arnaque
    """
    client = client or anthropic.Anthropic()
    reponse = client.messages.parse(
        model=MODELE,
        max_tokens=2000,
        system=[{"type": "text", "text": systeme(profil), "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": _contenu(mail)}],
        output_config={"effort": "low"},
        output_format=Classement,
    )
    return reponse.parsed_output, reponse.usage


def _parametres_decodes(lien: str) -> list[str]:
    """Valeurs de paramètres d'URL qui sont du base64 lisible (traceurs de campagne)."""
    lisibles = []
    for valeurs in parse_qs(urlparse(lien).query).values():
        for v in valeurs:
            try:
                texte = base64.b64decode(v + "=" * (-len(v) % 4), validate=True).decode("ascii")
            except ValueError:            # binascii.Error et UnicodeDecodeError en héritent
                continue
            if re.fullmatch(r"[a-z0-9_-]{6,}", texte):
                lisibles.append(texte)
    return lisibles


def signaux_code(mail, classement: Classement) -> list[str]:
    """Signaux d'arnaque détectés par des règles fixes (sans Claude)."""
    signaux = []
    texte = _sans_accents(mail.sujet + " " + mail.corps)
    for motif, libelle in MOTS_ARNAQUE.items():
        if re.search(motif, texte):
            signaux.append(libelle)

    demande_pieces = classement.categorie == "demande_dossier" or PIECES_SENSIBLES.search(mail.corps)
    domaines_liens = {urlparse(l).netloc.lower().removeprefix("www.") for l in mail.liens}
    plateforme = urlparse(classement.plateforme_dossier or "").netloc.lower().removeprefix("www.")
    if plateforme and not plateforme.endswith(PLATEFORMES_SURES) and plateforme != mail.domaine:
        signaux.append(f"dossier à déposer sur une plateforme tierce ({plateforme}), pas DossierFacile")
    if demande_pieces and mail.envoi_en_masse:
        signaux.append("demande de dossier envoyée par un outil d'emailing en masse")
    if demande_pieces:
        for lien in mail.liens:
            signaux += [f"lien traceur de campagne (« {e} »)" for e in _parametres_decodes(lien)]
    if demande_pieces and not domaines_liens & set(PLATEFORMES_SURES) and PIECES_SENSIBLES.search(mail.corps):
        signaux.append("pièces sensibles demandées hors DossierFacile")
    return list(dict.fromkeys(signaux))


# Thèmes couverts par les règles fixes : un signal de Claude sur le même thème est un doublon.
THEMES = ("plateforme", "masse", "traceur", "paiement", "etranger", "cles", "visite")


def fusionner_signaux(code: list[str], claude: list[str]) -> list[str]:
    """Signaux du code d'abord (les plus précis), puis ceux de Claude sans doublon de thème.

    Exemple :
        >>> fusionner_signaux(["dossier à déposer sur une plateforme tierce (x.com)"],
        ...                   ["plateforme inconnue", "urgence artificielle"])
        ['dossier à déposer sur une plateforme tierce (x.com)', 'urgence artificielle']
    """
    couverts = {t for t in THEMES if any(t in _sans_accents(s) for s in code)}
    autres = [s for s in claude if not any(t in _sans_accents(s) for t in couverts)]
    return list(dict.fromkeys(code + autres))


def risque_final(classement: Classement, signaux: list[str]) -> Risque:
    """Risque retenu : le plus élevé entre Claude et les règles fixes."""
    par_code: Risque = "eleve" if len(signaux) >= 2 else "moyen" if signaux else "faible"
    return max(classement.risque_arnaque, par_code, key=ORDRE_RISQUE.get)
