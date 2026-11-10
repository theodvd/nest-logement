"""Comptes annonceurs : profil (particulier ou agence, ancienneté, badges) et liste noire.

- Le profil sert au score (particulier, compte fiable) et à la détection d'arnaque.
- Un compte signalé est bloqué pour toujours : toutes ses annonces sont écartées
  avant jugement. Signalement par la commande `python -m nest --signaler <lien>`, par le
  bouton « Arnaque » de la notif ou de la page pont (via un canal ntfy dédié).

Exemple :
    >>> v = profil_vendeur(annonce.vendeur_id, base)
    >>> v["type"], v["anciennete_mois"], v["identite_verifiee"]
"""

import json
import logging
import os
import re
from datetime import datetime, timedelta

import lbc
import requests

from nest.sources.leboncoin import nouveau_client

log = logging.getLogger(__name__)

# Identifiant d'une annonce Gens de Confiance (Leboncoin : un nombre).
RE_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")

SCHEMA = """
CREATE TABLE IF NOT EXISTS vendeurs (
    id        TEXT PRIMARY KEY,
    maj_le    TEXT NOT NULL,
    profil    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS bloques (
    id        TEXT PRIMARY KEY,     -- id du compte Leboncoin, ou "gdc:<vanity>"
    nom       TEXT,
    raison    TEXT,
    bloque_le TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS etat (
    cle       TEXT PRIMARY KEY,
    valeur    TEXT
);
"""


def _schema(base) -> None:
    base.cx.executescript(SCHEMA)


def _nom_normalise(nom: str | None) -> str:
    """« Immo Exemple » et « immoexemple » donnent la même clé."""
    return re.sub(r"[^a-z0-9]", "", (nom or "").lower())


def _profil_gdc(vendeur_id: str, annonce) -> dict:
    """Profil d'un annonceur Gens de Confiance, tiré de la page de recherche (aucun appel).

    Le site ne donne ni ancienneté ni badge : « compte fiable » reste inconnu (None,
    pas False : sinon Claude signale une « identité non vérifiée »).
    """
    annonceur = (getattr(annonce, "details", None) or {}).get("annonceur") or {}
    return {
        "id": vendeur_id,
        "nom": annonceur.get("nom"),
        "type": "agence" if annonceur.get("pro") else "particulier",
        "anciennete_mois": None,
        "identite_verifiee": None,
        "badges": [],
        "site": "Gens de Confiance (membres parrainés)",
    }


def profil_vendeur(vendeur_id: str | None, base, client: lbc.Client | None = None,
                   annonce=None) -> dict:
    """Profil du compte annonceur, en cache 7 jours. Dict vide si inconnu."""
    if not vendeur_id:
        return {}
    if vendeur_id.startswith("gdc:"):
        return _profil_gdc(vendeur_id, annonce)
    _schema(base)
    ligne = base.cx.execute("SELECT maj_le, profil FROM vendeurs WHERE id = ?", (vendeur_id,)).fetchone()
    if ligne and datetime.fromisoformat(ligne[0]) > datetime.now() - timedelta(days=7):
        return json.loads(ligne[1])
    try:
        u = (client or nouveau_client()).get_user(user_id=vendeur_id)
    except Exception as err:            # compte supprimé, réseau, anti-bot : profil inconnu
        log.warning("Profil vendeur %s indisponible : %s", vendeur_id, err)
        return {}
    pro = getattr(u, "pro", None)
    inscrit = (u.registered_at or "")[:10]
    try:
        anciennete = (datetime.now() - datetime.fromisoformat(inscrit)).days / 30.4
    except ValueError:
        anciennete = None
    badges = [b.name for b in (u.badges or [])]
    profil = {
        "id": vendeur_id,
        "nom": (getattr(pro, "online_store_name", None) if pro else None) or u.name,
        "type": "particulier" if u.account_type == "private" else "agence",
        "inscrit_le": inscrit,
        "anciennete_mois": round(anciennete, 1) if anciennete is not None else None,
        "nb_annonces": u.total_ads,
        "identite_verifiee": "VerifiedLandlordID" in badges,
        "badges": badges,
    }
    base.cx.execute("INSERT OR REPLACE INTO vendeurs (id, maj_le, profil) VALUES (?, ?, ?)",
                    (vendeur_id, datetime.now().isoformat(timespec="seconds"),
                     json.dumps(profil, ensure_ascii=False)))
    base.cx.commit()
    return profil


def est_bloque(vendeur: dict, base) -> bool:
    """True si le compte (ou une agence du même nom) a été signalé."""
    if not vendeur:
        return False
    _schema(base)
    ids = {r[0] for r in base.cx.execute("SELECT id FROM bloques")}
    noms = {_nom_normalise(r[0]) for r in base.cx.execute("SELECT nom FROM bloques") if r[0]}
    return vendeur.get("id") in ids or _nom_normalise(vendeur.get("nom")) in noms


def bloquer(id_annonce: str, base, raison: str = "arnaque signalée") -> str:
    """Bloque le compte qui a publié cette annonce. Renvoie le nom du compte.

    Exemple :
        >>> bloquer("https://www.leboncoin.fr/ad/locations/1234567890", base)
        'Immo Exemple'
    """
    _schema(base)
    if uuid := RE_UUID.search(id_annonce):
        return _bloquer_gdc(uuid.group(0), base, raison)
    id_annonce = re.sub(r"\D", "", id_annonce.rstrip("/").split("/")[-1])
    client = nouveau_client()
    ad = client.get_ad(id_annonce)
    vendeur = profil_vendeur(ad._user_id, base, client) or {"id": ad._user_id, "nom": None}
    base.cx.execute("INSERT OR REPLACE INTO bloques (id, nom, raison, bloque_le) VALUES (?, ?, ?, ?)",
                    (vendeur["id"], vendeur.get("nom"), f"{raison} (annonce {id_annonce})",
                     datetime.now().isoformat(timespec="seconds")))
    base.cx.commit()
    log.info("Compte bloqué : %s (%s)", vendeur.get("nom"), vendeur["id"])
    return vendeur.get("nom") or vendeur["id"]


def _bloquer_gdc(uuid: str, base, raison: str) -> str:
    """Bloque l'annonceur Gens de Confiance d'une annonce déjà vue par nest."""
    ligne = base.cx.execute("SELECT annonce FROM annonces WHERE cle = ?",
                            (f"gensdeconfiance:{uuid}",)).fetchone()
    if not ligne:
        raise ValueError(f"annonce Gens de Confiance {uuid} inconnue de nest")
    annonce = json.loads(ligne[0])
    vendeur_id = annonce.get("vendeur_id")
    if not vendeur_id:
        raise ValueError(f"annonce Gens de Confiance {uuid} sans annonceur identifié")
    nom = (annonce.get("details", {}).get("annonceur") or {}).get("nom")
    base.cx.execute("INSERT OR REPLACE INTO bloques (id, nom, raison, bloque_le) VALUES (?, ?, ?, ?)",
                    (vendeur_id, nom, f"{raison} (annonce {uuid})",
                     datetime.now().isoformat(timespec="seconds")))
    base.cx.commit()
    log.info("Compte Gens de Confiance bloqué : %s (%s)", nom, vendeur_id)
    return nom or vendeur_id


def canal_signalement() -> str | None:
    """Canal ntfy où arrivent les signalements (dérivé du canal des notifs)."""
    topic = os.getenv("NTFY_TOPIC")
    return f"{topic}-signal" if topic else None


def lire_signalements(base) -> list[str]:
    """Relève les signalements envoyés depuis la notif ou la page pont, et bloque les comptes."""
    canal = canal_signalement()
    if not canal:
        return []
    _schema(base)
    serveur = os.getenv("NTFY_SERVEUR", "https://ntfy.sh").rstrip("/")
    depuis = (base.cx.execute("SELECT valeur FROM etat WHERE cle='signal_depuis'").fetchone() or ["all"])[0]
    try:
        rep = requests.get(f"{serveur}/{canal}/json", params={"poll": "1", "since": depuis}, timeout=15)
        rep.raise_for_status()
    except requests.RequestException as err:
        log.warning("Signalements illisibles pour ce passage : %s", err)
        return []
    bloques, dernier = [], depuis
    for ligne in rep.text.splitlines():
        msg = json.loads(ligne)
        if msg.get("event") != "message":
            continue
        dernier = msg["id"]
        # UUID d'abord : celui d'une annonce Gens de Confiance peut contenir 8 chiffres d'affilée.
        if m := RE_UUID.search(msg.get("message", "")) or re.search(r"(\d{8,})", msg.get("message", "")):
            try:
                bloques.append(bloquer(m.group(0), base))
            except Exception as err:
                log.warning("Signalement %s non traité : %s", m.group(0), err)
    base.cx.execute("INSERT OR REPLACE INTO etat (cle, valeur) VALUES ('signal_depuis', ?)", (dernier,))
    base.cx.commit()
    return bloques
