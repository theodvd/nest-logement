"""Stockage SQLite : annonces déjà vues (dédoublonnage) et cache des trajets.

Une base locale suffit : un seul processus écrit, toutes les 10 minutes.
"""

import json
import sqlite3
from datetime import datetime
from pathlib import Path

CHEMIN_DEFAUT = Path(__file__).resolve().parent.parent / "data" / "nest.sqlite"

SCHEMA = """
CREATE TABLE IF NOT EXISTS annonces (
    cle         TEXT PRIMARY KEY,   -- "source:id"
    vue_le      TEXT NOT NULL,
    statut      TEXT NOT NULL,      -- ecartee / en_attente / jugee / notifiee / amorcage / doublon
    raison      TEXT,               -- pourquoi écartée
    score       INTEGER,
    trajet_min  INTEGER,
    annonce     TEXT NOT NULL,      -- JSON de l'Annonce
    verdict     TEXT                -- JSON du jugement Claude
);
CREATE TABLE IF NOT EXISTS trajets (
    cle     TEXT PRIMARY KEY,       -- coordonnées arrondies
    calcule_le TEXT NOT NULL,
    trajet  TEXT NOT NULL           -- JSON du Trajet
);
-- Phase 2 : mails d'agence et suivi des candidatures (voir nest/suivi.py).
CREATE TABLE IF NOT EXISTS mails (
    id          TEXT PRIMARY KEY,   -- id Gmail du message
    fil_id      TEXT NOT NULL,      -- id Gmail du fil (thread)
    recu_le     TEXT NOT NULL,
    expediteur  TEXT,
    sujet       TEXT,
    categorie   TEXT NOT NULL,      -- hors_sujet / demande_dossier / proposition_visite / ...
    risque      TEXT,               -- faible / moyen / eleve
    candidature TEXT,               -- cle de la candidature rattachée
    classement  TEXT                -- JSON du classement Claude
);
CREATE TABLE IF NOT EXISTS candidatures (
    cle         TEXT PRIMARY KEY,   -- cle de l'annonce nest, sinon "mail:<fil_id>"
    cree_le     TEXT NOT NULL,
    maj_le      TEXT NOT NULL,
    statut      TEXT NOT NULL,      -- contacte / reponse / visite / dossier / accepte / refuse / arnaque
    bien        TEXT,               -- ex. "Studio 14 m², Paris 75011, 660 € CC"
    agence      TEXT,
    contact     TEXT,               -- adresse mail de l'interlocuteur
    fil_id      TEXT,               -- dernier fil Gmail
    url_annonce TEXT,
    risque      TEXT,
    prochaine_action TEXT,
    notion_id   TEXT                -- page miroir dans Notion
);
CREATE TABLE IF NOT EXISTS evenements (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    candidature TEXT NOT NULL,
    le          TEXT NOT NULL,
    type        TEXT NOT NULL,      -- mail_recu / statut / ...
    detail      TEXT
);
"""


class Base:
    """Accès minimal à la base, sans ORM."""

    def __init__(self, chemin: Path = CHEMIN_DEFAUT):
        chemin.parent.mkdir(parents=True, exist_ok=True)
        self.cx = sqlite3.connect(chemin)
        self.cx.executescript(SCHEMA)

    def deja_vue(self, cle: str) -> bool:
        """True si l'annonce a déjà été traitée lors d'un passage précédent."""
        ligne = self.cx.execute("SELECT 1 FROM annonces WHERE cle = ?", (cle,)).fetchone()
        return ligne is not None

    def enregistrer(self, annonce, statut: str, raison: str | None = None,
                    score: int | None = None, trajet_min: int | None = None,
                    verdict: dict | None = None) -> None:
        """Insère ou met à jour une annonce et son issue."""
        self.cx.execute(
            """INSERT OR REPLACE INTO annonces
               (cle, vue_le, statut, raison, score, trajet_min, annonce, verdict)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (annonce.cle, datetime.now().isoformat(timespec="seconds"), statut, raison,
             score, trajet_min, json.dumps(annonce.to_dict(), ensure_ascii=False),
             json.dumps(verdict, ensure_ascii=False) if verdict else None),
        )
        self.cx.commit()

    def trajet_en_cache(self, cle: str) -> dict | None:
        """Renvoie un trajet déjà calculé pour ces coordonnées, s'il existe."""
        ligne = self.cx.execute("SELECT trajet FROM trajets WHERE cle = ?", (cle,)).fetchone()
        return json.loads(ligne[0]) if ligne else None

    def memoriser_trajet(self, cle: str, trajet: dict) -> None:
        """Met un trajet en cache (les centres de commune reviennent très souvent)."""
        self.cx.execute(
            "INSERT OR REPLACE INTO trajets (cle, calcule_le, trajet) VALUES (?, ?, ?)",
            (cle, datetime.now().isoformat(timespec="seconds"),
             json.dumps(trajet, ensure_ascii=False)),
        )
        self.cx.commit()
