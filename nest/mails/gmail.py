"""Accès à une boîte Gmail, en LECTURE SEULE (module facultatif).

Le droit demandé à Google est `gmail.readonly` : nest ne peut ni envoyer, ni
supprimer, ni modifier un mail. Sans autorisation, le tri des mails est
simplement ignoré.

Mise en place (une seule fois) :
    1. Console Google Cloud : activer l'API Gmail, créer un ID client OAuth
       de type « Application de bureau », passer l'écran de consentement en
       « Production » (sinon le jeton expire au bout de 7 jours).
    2. Mettre GMAIL_CLIENT_ID et GMAIL_CLIENT_SECRET dans .env.
    3. .venv/bin/python -m nest.mails.gmail --autoriser
       → le navigateur s'ouvre, on accepte, le jeton est rangé dans
       data/gmail_jeton.json (chmod 600, ignoré par git).

Exemple d'appel :
    >>> from nest.mails import gmail
    >>> for mail in gmail.recents(gmail.service(), jours=3):
    ...     print(mail.date, mail.expediteur, mail.sujet)
"""

import argparse
import base64
import os
import re
from dataclasses import dataclass, field
from email.utils import parseaddr, parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

RACINE = Path(__file__).resolve().parent.parent.parent
JETON = RACINE / "data" / "gmail_jeton.json"
DROITS = ["https://www.googleapis.com/auth/gmail.readonly"]

# Corps tronqué : les mails d'agence tiennent largement dedans, et ça borne le coût Claude.
CORPS_MAX = 6000


class JetonAbsent(RuntimeError):
    """L'autorisation Gmail n'a pas encore été faite (voir --autoriser)."""


@dataclass
class Mail:
    """Un mail reçu, réduit à ce dont le tri a besoin."""

    id: str
    fil_id: str
    date: str                   # ISO, heure de Paris
    expediteur: str             # adresse seule, ex. "contact@agence-exemple.fr"
    nom_expediteur: str
    sujet: str
    corps: str                  # texte brut, tronqué
    liens: list[str] = field(default_factory=list)
    pieces_jointes: list[str] = field(default_factory=list)
    envoi_en_masse: bool = False  # en-tête List-Unsubscribe présent
    libelles: list[str] = field(default_factory=list)

    @property
    def domaine(self) -> str:
        """Domaine de l'expéditeur, ex. 'agence-exemple.fr'."""
        return self.expediteur.rsplit("@", 1)[-1].lower()

    @property
    def url(self) -> str:
        """Lien qui ouvre le fil dans Gmail (navigateur ou app)."""
        return f"https://mail.google.com/mail/u/0/#all/{self.fil_id}"


def _client_oauth() -> dict:
    """Configuration du client OAuth, lue dans les variables d'environnement."""
    return {"installed": {
        "client_id": os.environ["GMAIL_CLIENT_ID"],
        "client_secret": os.environ["GMAIL_CLIENT_SECRET"],
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "redirect_uris": ["http://localhost"],
    }}


def _ranger_jeton(identifiants: Credentials) -> None:
    """Écrit le jeton sur disque, lisible par l'utilisateur seul."""
    JETON.parent.mkdir(parents=True, exist_ok=True)
    JETON.write_text(identifiants.to_json(), encoding="utf-8")
    JETON.chmod(0o600)


def autoriser() -> None:
    """Ouvre le navigateur pour autoriser la lecture de la boîte (une fois)."""
    flux = InstalledAppFlow.from_client_config(_client_oauth(), DROITS)
    identifiants = flux.run_local_server(port=0, prompt="consent", access_type="offline")
    _ranger_jeton(identifiants)
    print(f"Autorisation enregistrée dans {JETON}")


def actif() -> bool:
    """True si l'autorisation Gmail a été faite (sinon le tri des mails est ignoré)."""
    return JETON.exists()


def service():
    """Client Gmail prêt à l'emploi ; rafraîchit le jeton si besoin."""
    if not JETON.exists():
        raise JetonAbsent("Lancer d'abord : python -m nest.mails.gmail --autoriser")
    identifiants = Credentials.from_authorized_user_file(str(JETON), DROITS)
    if not identifiants.valid:
        identifiants.refresh(Request())
        _ranger_jeton(identifiants)
    return build("gmail", "v1", credentials=identifiants, cache_discovery=False)


class _VersTexte(HTMLParser):
    """Convertit un corps HTML en texte, en gardant les liens."""

    def __init__(self):
        super().__init__()
        self.morceaux: list[str] = []
        self.liens: list[str] = []
        self._ignore = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("style", "script", "head"):
            self._ignore += 1
        if tag == "a" and (href := dict(attrs).get("href")):
            self.liens.append(href)
        if tag in ("br", "p", "div", "tr", "li"):
            self.morceaux.append("\n")

    def handle_endtag(self, tag):
        if tag in ("style", "script", "head") and self._ignore:
            self._ignore -= 1

    def handle_data(self, data):
        if not self._ignore:
            self.morceaux.append(data)


def _decoder(donnees: str) -> str:
    """Décode une partie de mail (base64 « url-safe » de l'API Gmail)."""
    return base64.urlsafe_b64decode(donnees + "=" * (-len(donnees) % 4)).decode("utf-8", "replace")


def _parcourir(partie: dict, textes: list, htmls: list, pj: list) -> None:
    """Descend dans l'arbre MIME et range texte, HTML et noms de pièces jointes."""
    if partie.get("filename"):
        pj.append(partie["filename"])
    type_mime = partie.get("mimeType", "")
    donnees = partie.get("body", {}).get("data")
    if donnees and type_mime == "text/plain":
        textes.append(_decoder(donnees))
    elif donnees and type_mime == "text/html":
        htmls.append(_decoder(donnees))
    for sous_partie in partie.get("parts", []) or []:
        _parcourir(sous_partie, textes, htmls, pj)


def _vers_mail(brut: dict) -> Mail:
    """Transforme la réponse brute de l'API en Mail."""
    entetes = {e["name"].lower(): e["value"] for e in brut["payload"].get("headers", [])}
    nom, adresse = parseaddr(entetes.get("from", ""))
    textes, htmls, pj = [], [], []
    _parcourir(brut["payload"], textes, htmls, pj)

    convertisseur = _VersTexte()
    for h in htmls:
        convertisseur.feed(h)
    corps = "\n".join(textes) if textes else "".join(convertisseur.morceaux)
    corps = re.sub(r"\n\s*\n+", "\n\n", corps).strip()
    liens = convertisseur.liens + re.findall(r"https?://[^\s)>\"']+", corps)

    try:
        date = parsedate_to_datetime(entetes["date"]).isoformat(timespec="seconds")
    except (KeyError, TypeError, ValueError):
        date = ""
    return Mail(
        id=brut["id"], fil_id=brut["threadId"], date=date,
        expediteur=adresse.lower(), nom_expediteur=nom,
        sujet=entetes.get("subject", "").replace("​", ""),   # espaces invisibles anti-filtre
        corps=corps[:CORPS_MAX],
        liens=list(dict.fromkeys(liens)),                          # sans doublons, ordre gardé
        pieces_jointes=pj,
        envoi_en_masse="list-unsubscribe" in entetes,
        libelles=brut.get("labelIds", []),
    )


def recents(client, jours: int = 3, requete: str = "") -> list[Mail]:
    """Mails reçus ces derniers jours, hors envoyés, promotions et réseaux sociaux.

    Exemple :
        >>> recents(service(), jours=2)
    """
    q = f"newer_than:{jours}d -in:sent -in:draft -category:promotions -category:social {requete}"
    ids, jeton_page = [], None
    while True:
        rep = client.users().messages().list(userId="me", q=q.strip(), pageToken=jeton_page,
                                             maxResults=100).execute()
        ids += [m["id"] for m in rep.get("messages", [])]
        jeton_page = rep.get("nextPageToken")
        if not jeton_page:
            break
    return [_vers_mail(client.users().messages().get(userId="me", id=i, format="full").execute())
            for i in ids]


def lire(client, ids: list[str]) -> list[Mail]:
    """Relit des mails précis (utile pour rejouer un tri)."""
    return [_vers_mail(client.users().messages().get(userId="me", id=i, format="full").execute())
            for i in ids]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Accès Gmail de nest")
    parser.add_argument("--autoriser", action="store_true", help="autorisation OAuth (une fois)")
    parser.add_argument("--lister", type=int, metavar="JOURS", help="affiche les mails récents")
    args = parser.parse_args()
    if args.autoriser:
        autoriser()
    elif args.lister:
        for m in recents(service(), jours=args.lister):
            print(f"{m.date[:16]}  {m.expediteur:40.40}  {m.sujet[:70]}")
    else:
        parser.print_help()
