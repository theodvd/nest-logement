"""Suivi des candidatures : un bien = une ligne, chaque mail fait avancer son statut.

Parcours : contacte → reponse → visite → dossier → accepte, avec deux issues
terminales, refuse et arnaque. Un statut n'avance que vers l'avant (un mail
« merci pour votre dossier » ne fait pas revenir une candidature en « réponse ») ;
chaque mail est quand même noté dans `evenements`.

SQLite est la source de vérité ; Notion n'en est qu'un miroir (voir notion.py).

Exemple d'appel (voir l'état du suivi dans le terminal) :
    python -m nest.suivi
"""

import json
from datetime import datetime

STATUTS = ["contacte", "reponse", "visite", "dossier", "accepte"]
TERMINAUX = ["refuse", "arnaque"]

# Catégorie du mail → statut visé par la candidature.
STATUT_VISE = {
    "demande_dossier": "dossier",
    "proposition_visite": "visite",
    "refus": "refuse",
    "question": "reponse",
    "relance_agence": "reponse",
    "confirmation": "reponse",
    "autre_logement": "reponse",
}


def _maintenant() -> str:
    return datetime.now().isoformat(timespec="seconds")


def statut_suivant(actuel: str | None, vise: str) -> str:
    """Nouveau statut : on n'avance que vers l'avant, sauf issue terminale."""
    if actuel in TERMINAUX:
        return actuel
    if vise in TERMINAUX or actuel is None:
        return vise
    return vise if STATUTS.index(vise) > STATUTS.index(actuel) else actuel


def _annonce_correspondante(base, classement) -> tuple[str, str] | None:
    """Annonce nest qui correspond au bien du mail (surface, loyer, code postal).

    Renvoie (cle, url) ou None. Tolérances : ±1 m², ±15 € (arrondis des agences).
    Seules les annonces jugées par nest comptent : ce sont celles auxquelles on écrit.
    """
    if classement.surface_m2 is None or classement.loyer_cc is None:
        return None
    lignes = base.cx.execute(
        """SELECT a.cle, json_extract(a.annonce, '$.url')
           FROM annonces AS a
           WHERE a.statut IN ('notifiee', 'jugee')          -- annonces vues via nest
             AND abs(json_extract(a.annonce, '$.surface_m2') - ?) <= 1
             AND abs(json_extract(a.annonce, '$.loyer_cc') - ?) <= 15
             AND (? IS NULL OR json_extract(a.annonce, '$.lieu') LIKE '%' || ? || '%')
           ORDER BY a.vue_le DESC
           LIMIT 2""",
        (classement.surface_m2, classement.loyer_cc, classement.code_postal, classement.code_postal),
    ).fetchall()
    return tuple(lignes[0]) if len(lignes) == 1 else None    # ambigu = pas de rattachement


def rattacher(base, mail, classement) -> tuple[str, str | None]:
    """Trouve (ou crée) la candidature concernée par ce mail.

    Ordre : 1) annonce nest reconnue ; 2) même interlocuteur et même bien ;
    3) bien non précisé : même fil Gmail, sinon dernière candidature de cet
    interlocuteur ; 4) sinon nouvelle candidature.
    Renvoie (cle, url de l'annonce ou None).
    """
    if trouvee := _annonce_correspondante(base, classement):
        return trouvee
    cx = base.cx
    if classement.bien:
        trouve = cx.execute("SELECT cle, url_annonce FROM candidatures WHERE contact = ? AND bien = ?",
                           (mail.expediteur, classement.bien)).fetchone()
    else:
        trouve = (cx.execute("SELECT candidature, NULL FROM mails WHERE fil_id = ? AND candidature IS NOT NULL "
                            "ORDER BY recu_le DESC LIMIT 1", (mail.fil_id,)).fetchone()
                 or cx.execute("SELECT cle, url_annonce FROM candidatures WHERE contact = ? "
                               "ORDER BY maj_le DESC LIMIT 1", (mail.expediteur,)).fetchone())
    if trouve:
        return trouve[0], trouve[1]
    return f"mail:{mail.id}", None


def contact_signale(base, adresse: str) -> bool:
    """True si cet interlocuteur a déjà une candidature classée « arnaque »."""
    return base.cx.execute("SELECT 1 FROM candidatures WHERE contact = ? AND statut = 'arnaque'",
                           (adresse,)).fetchone() is not None


def domaines_suivis(base) -> set[str]:
    """Domaines des interlocuteurs suivis (pour que le préfiltre ne les rate pas)."""
    lignes = base.cx.execute("SELECT DISTINCT contact FROM candidatures WHERE contact IS NOT NULL").fetchall()
    return {c.rsplit("@", 1)[-1] for (c,) in lignes} - {"gmail.com", "hotmail.fr", "hotmail.com",
                                                         "outlook.fr", "outlook.com", "yahoo.fr", "orange.fr"}


def deja_lu(base, mail_id: str) -> bool:
    """True si ce mail a déjà été trié lors d'un passage précédent."""
    return base.cx.execute("SELECT 1 FROM mails WHERE id = ?", (mail_id,)).fetchone() is not None


def noter_mail(base, mail, categorie: str, risque: str | None = None,
               candidature: str | None = None, classement=None) -> None:
    """Mémorise un mail trié (même hors sujet, pour ne pas le relire)."""
    base.cx.execute(
        """INSERT OR REPLACE INTO mails
           (id, fil_id, recu_le, expediteur, sujet, categorie, risque, candidature, classement)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (mail.id, mail.fil_id, mail.date, mail.expediteur, mail.sujet, categorie, risque, candidature,
         classement.model_dump_json() if classement else None),
    )
    base.cx.commit()


def mettre_a_jour(base, cle: str, mail, classement, risque: str, url_annonce: str | None) -> dict:
    """Applique un mail à sa candidature et renvoie la ligne à jour (avec l'ancien statut).

    Exemple :
        >>> ligne = mettre_a_jour(base, "mail:1a15…", mail, classement, "eleve", None)
        >>> ligne["ancien_statut"], ligne["statut"]
        (None, 'arnaque')
    """
    cx = base.cx
    existante = cx.execute("SELECT statut FROM candidatures WHERE cle = ?", (cle,)).fetchone()
    ancien = existante[0] if existante else None
    vise = "arnaque" if risque == "eleve" else STATUT_VISE.get(classement.categorie, "reponse")
    nouveau = statut_suivant(ancien, vise)
    maintenant = _maintenant()

    cx.execute(
        """INSERT INTO candidatures
               (cle, cree_le, maj_le, statut, bien, agence, contact, fil_id, url_annonce, risque, prochaine_action)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(cle) DO UPDATE SET
               maj_le = excluded.maj_le, statut = excluded.statut,
               bien = coalesce(excluded.bien, candidatures.bien),
               agence = coalesce(excluded.agence, candidatures.agence),
               contact = excluded.contact, fil_id = excluded.fil_id,
               url_annonce = coalesce(candidatures.url_annonce, excluded.url_annonce),
               risque = excluded.risque, prochaine_action = excluded.prochaine_action""",
        (cle, maintenant, maintenant, nouveau, classement.bien, classement.agence, mail.expediteur,
         mail.fil_id, url_annonce, risque, classement.action_a_faire),
    )
    cx.execute("INSERT INTO evenements (candidature, le, type, detail) VALUES (?, ?, ?, ?)",
               (cle, maintenant, "mail_recu",
                json.dumps({"mail": mail.id, "categorie": classement.categorie, "resume": classement.resume},
                           ensure_ascii=False)))
    if nouveau != ancien:
        cx.execute("INSERT INTO evenements (candidature, le, type, detail) VALUES (?, ?, ?, ?)",
                   (cle, maintenant, "statut", f"{ancien or '∅'} → {nouveau}"))
    cx.commit()
    return {**ligne(base, cle), "ancien_statut": ancien}


def ligne(base, cle: str) -> dict:
    """Une candidature sous forme de dictionnaire."""
    cur = base.cx.execute("SELECT * FROM candidatures WHERE cle = ?", (cle,))
    colonnes = [c[0] for c in cur.description]
    return dict(zip(colonnes, cur.fetchone()))


def noter_notion(base, cle: str, notion_id: str) -> None:
    """Retient l'id de la page Notion miroir de la candidature."""
    base.cx.execute("UPDATE candidatures SET notion_id = ? WHERE cle = ?", (notion_id, cle))
    base.cx.commit()


if __name__ == "__main__":
    from nest.base import Base

    b = Base()
    print(f"{'statut':10} {'risque':7} {'maj':16}  bien · agence → action")
    for cle in [c for (c,) in b.cx.execute("SELECT cle FROM candidatures ORDER BY maj_le DESC LIMIT 50")]:
        c = ligne(b, cle)
        print(f"{c['statut']:10} {c['risque'] or '':7} {c['maj_le'][:16]}  "
              f"{c['bien'] or '?'} · {c['agence'] or c['contact']} → {c['prochaine_action']}")
