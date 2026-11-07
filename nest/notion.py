"""Miroir du suivi des candidatures dans une base Notion (lisible sur iPhone).

SQLite reste la référence : si Notion est en panne ou mal configuré, la veille
continue et la page sera rattrapée au prochain changement de la candidature.

Configuration (variables d'environnement) :
    NOTION_TOKEN       jeton d'une intégration interne Notion (notion.so/profile/integrations)
    NOTION_BASE_SUIVI  id de la base « Candidatures logement » (partagée avec l'intégration)

Exemple :
    >>> from nest import notion
    >>> page_id = notion.publier(candidature)     # crée ou met à jour la page
"""

import logging
import os

import requests

log = logging.getLogger(__name__)
API = "https://api.notion.com/v1"
VERSION = "2022-06-28"

LIBELLES_STATUT = {
    "contacte": "Contacté", "reponse": "Réponse reçue", "visite": "Visite",
    "dossier": "Dossier demandé", "accepte": "Accepté", "refuse": "Refusé", "arnaque": "Arnaque probable",
}
LIBELLES_RISQUE = {"faible": "Faible", "moyen": "Moyen", "eleve": "Élevé"}


def actif() -> bool:
    """True si la configuration Notion est présente."""
    return bool(os.getenv("NOTION_TOKEN") and os.getenv("NOTION_BASE_SUIVI"))


def _texte(valeur: str | None) -> list:
    return [{"type": "text", "text": {"content": (valeur or "")[:1900]}}]


def _proprietes(c: dict) -> dict:
    """Ligne SQLite → propriétés de la page Notion (noms = colonnes de la base)."""
    return {
        "Bien": {"title": _texte(c.get("bien") or c.get("agence") or c.get("contact"))},
        "Statut": {"select": {"name": LIBELLES_STATUT[c["statut"]]}},
        "Risque": {"select": {"name": LIBELLES_RISQUE.get(c.get("risque") or "faible")}},
        "Agence": {"rich_text": _texte(c.get("agence"))},
        "Contact": {"email": c.get("contact")},
        "Prochaine action": {"rich_text": _texte(c.get("prochaine_action"))},
        "Dernier mail": {"url": f"https://mail.google.com/mail/u/0/#all/{c['fil_id']}" if c.get("fil_id") else None},
        "Annonce": {"url": c.get("url_annonce")},
        "Mis à jour": {"date": {"start": c["maj_le"], "time_zone": "Europe/Paris"}},
    }


def publier(c: dict) -> str | None:
    """Crée ou met à jour la page de la candidature ; renvoie son id (None si échec)."""
    if not actif():
        return None
    entetes = {"Authorization": f"Bearer {os.environ['NOTION_TOKEN']}",
               "Notion-Version": VERSION, "Content-Type": "application/json"}
    try:
        if c.get("notion_id"):
            rep = requests.patch(f"{API}/pages/{c['notion_id']}", headers=entetes,
                                 json={"properties": _proprietes(c)}, timeout=15)
        else:
            rep = requests.post(f"{API}/pages", headers=entetes, timeout=15, json={
                "parent": {"database_id": os.environ["NOTION_BASE_SUIVI"]},
                "properties": _proprietes(c)})
        rep.raise_for_status()
        return rep.json()["id"]
    except requests.RequestException as e:
        detail = getattr(e.response, "text", "")[:300] if getattr(e, "response", None) is not None else ""
        log.warning("Notion : échec pour %s (%s) %s", c["cle"], e, detail)
        return None
