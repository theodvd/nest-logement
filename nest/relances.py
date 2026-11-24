"""Relances : proposer de relancer un bailleur qui n'a pas répondu.

Parcours :
1. L'utilisateur touche « Copier et écrire » sur la page pont : elle publie « ecrit <id> » sur le canal
   de signalement ; vendeurs.lire_signalements() appelle noter_contact() et la candidature
   passe en « contacte ». Toucher le lien vaut envoi (pas de bouton de confirmation).
2. Une réponse du bailleur arrive par mail (« Nouveau message pour "<titre>" ») : suivi.rattacher()
   la relie à l'annonce, la candidature passe en « reponse », plus aucune relance.
3. À chaque passage, traiter() repère les candidatures restées en « contacte » depuis
   `delai_h` heures et envoie une notif avec la relance prête à coller ; toucher son lien
   publie « relance <id> <n> ». Au plus `max` relances, seulement entre `heures`.

L'envoi reste manuel : automatiser la messagerie Leboncoin imposerait de contourner DataDome
(une fenêtre de navigateur pilotée est bloquée), et ferait courir un risque de suspension du compte.

Exemple d'appel (voir ce qui serait proposé, sans rien envoyer) :
    python -m nest.relances
"""

import json
import logging
from datetime import datetime, timedelta

import lbc

from nest import notif, notion, suivi
from nest.annonce import Annonce
from nest.sources.leboncoin import nouveau_client

log = logging.getLogger(__name__)

SITES = {"leboncoin": "Leboncoin", "gensdeconfiance": "Gens de Confiance"}
DOUBLE_TOUCHER_MIN = 10       # deux touchers du même lien en moins de 10 min = un seul envoi
MAX_NOTIFS_PAR_PASSAGE = 3


def _maintenant() -> datetime:
    return datetime.now().replace(microsecond=0)


def _annonce(base, id_source: str) -> tuple[str, dict] | None:
    """(cle, annonce) d'après l'identifiant publié par la page pont."""
    trouvee = base.cx.execute(
        "SELECT cle, annonce FROM annonces WHERE cle IN (?, ?) ORDER BY vue_le DESC LIMIT 1",
        (f"leboncoin:{id_source}", f"gensdeconfiance:{id_source}")).fetchone()
    return (trouvee[0], json.loads(trouvee[1])) if trouvee else None


def _bien(a: dict) -> str:
    """Libellé court de la candidature, ex. « Studio 17 m² rénové…, Paris 75011, 820 € CC »."""
    loyer = f", {a['loyer_cc']:.0f} € CC" if a.get("loyer_cc") else ""
    return f"{a.get('titre', '')[:60]}, {a.get('lieu', '')}{loyer}"


def noter_contact(base, texte: str) -> str | None:
    """Enregistre « ecrit <id> » ou « relance <id> <n> » publié par la page pont.

    Renvoie la clé de la candidature, ou None si l'annonce est inconnue de nest.

    Exemple :
        >>> noter_contact(base, "ecrit 3310047296")
        'leboncoin:3310047296'
    """
    morceaux = texte.split()
    type_evt, id_source = morceaux[0], morceaux[1]
    numero = morceaux[2] if type_evt == "relance" and len(morceaux) > 2 else ""
    trouvee = _annonce(base, id_source)
    if not trouvee:
        log.warning("Contact signalé pour une annonce inconnue de nest : %s", id_source)
        return None
    cle, a = trouvee
    cx, maintenant = base.cx, _maintenant()
    recent = (maintenant - timedelta(minutes=DOUBLE_TOUCHER_MIN)).isoformat()
    if cx.execute("SELECT 1 FROM evenements WHERE candidature = ? AND type = ? AND detail = ? AND le >= ?",
                  (cle, type_evt, numero, recent)).fetchone():
        return cle                                   # lien touché deux fois : un seul envoi
    risque = cx.execute("SELECT json_extract(verdict, '$.risque_arnaque') FROM annonces WHERE cle = ?",
                        (cle,)).fetchone()[0]
    action = ("Attendre la réponse : relance proposée sans réponse sous 24 h" if type_evt == "ecrit"
              else f"Relance {numero} envoyée : attendre la réponse")
    # Une candidature déjà plus avancée (réponse, visite…) garde son statut.
    cx.execute(
        """INSERT INTO candidatures (cle, cree_le, maj_le, statut, bien, agence, url_annonce, risque, prochaine_action)
           VALUES (?, ?, ?, 'contacte', ?, ?, ?, ?, ?)
           ON CONFLICT(cle) DO UPDATE SET maj_le = excluded.maj_le,
               prochaine_action = CASE WHEN candidatures.statut = 'contacte'
                                       THEN excluded.prochaine_action ELSE candidatures.prochaine_action END""",
        (cle, maintenant.isoformat(), maintenant.isoformat(), _bien(a),
         SITES.get(a.get("source"), a.get("source")), a.get("url"), risque, action))
    cx.execute("INSERT INTO evenements (candidature, le, type, detail) VALUES (?, ?, ?, ?)",
               (cle, maintenant.isoformat(), type_evt, numero))
    cx.commit()
    log.info("Candidature %s : %s%s", cle, type_evt, f" {numero}" if numero else "")
    if page := notion.publier(suivi.ligne(base, cle)):
        suivi.noter_notion(base, cle, page)
    return cle


def _historique(base, cle: str) -> dict:
    """Dates utiles d'une candidature : premier et dernier contact, relances proposées."""
    lignes = base.cx.execute("SELECT type, le FROM evenements WHERE candidature = ? ORDER BY le",
                             (cle,)).fetchall()
    contacts = [datetime.fromisoformat(le) for t, le in lignes if t in ("ecrit", "relance")]
    proposees = [datetime.fromisoformat(le) for t, le in lignes if t == "relance_proposee"]
    return {"premier": contacts[0] if contacts else None, "dernier": contacts[-1] if contacts else None,
            "proposees": proposees, "retiree": any(t == "annonce_retiree" for t, _ in lignes)}


def annonce_en_ligne(annonce: Annonce, client: lbc.Client | None = None) -> bool | None:
    """True / False si on sait, None si Leboncoin n'a pas répondu (DataDome) : on réessaiera."""
    if annonce.source != "leboncoin":
        return True                     # Gens de Confiance : pas de vérification (cookie, rythme)
    try:
        (client or nouveau_client()).get_ad(annonce.id_source)
        return True
    except lbc.exceptions.NotFoundError:
        return False
    except Exception as err:
        log.info("Relance reportée, annonce %s non vérifiable : %s", annonce.id_source, err)
        return None


def texte_relance(profil: dict, annonce: Annonce, numero: int) -> str:
    """Relance n°`numero` à partir des modèles de `profil.yaml > relances.messages`."""
    titre = annonce.titre.lower()
    bien = ("chambre" if annonce.type_bien == "colocation"
            else "studio" if "studio" in titre
            else "appartement" if annonce.type_bien == "studio_t1" else "logement")
    modeles = profil["relances"]["messages"]
    modele = modeles[min(numero, len(modeles)) - 1]
    loc = profil["locataire"]
    return modele.format(bien=bien, prenom=loc["prenom"],
                         dossierfacile=loc.get("dossierfacile", {}).get("lien", "")).strip()


def a_relancer(base, profil: dict, maintenant: datetime | None = None) -> list[tuple[str, int, float]]:
    """Candidatures à relancer maintenant : (cle, numéro de relance, heures sans réponse)."""
    r = profil["relances"]
    maintenant = maintenant or _maintenant()
    if not r.get("actif", True) or not (r["heures"][0] <= maintenant.hour < r["heures"][1]):
        return []
    retenues = []
    for (cle,) in base.cx.execute("SELECT cle FROM candidatures WHERE statut = 'contacte' "
                                  "AND cle NOT LIKE 'mail:%' ORDER BY maj_le").fetchall():
        h = _historique(base, cle)
        if not h["dernier"] or h["retiree"] or len(h["proposees"]) >= r["max"]:
            continue
        if maintenant - h["premier"] > timedelta(days=r["abandon_jours"]):
            continue
        reference = max([h["dernier"], *h["proposees"]])
        if maintenant - reference >= timedelta(hours=r["delai_h"]):
            retenues.append((cle, len(h["proposees"]) + 1, (maintenant - h["dernier"]).total_seconds() / 3600))
    return retenues


def traiter(profil: dict, base, a_blanc: bool = False) -> int:
    """Propose les relances dues (une notif par relance). Renvoie le nombre de notifs envoyées."""
    if "relances" not in profil:
        return 0
    from nest.vendeurs import canal_signalement
    envoyees, client = 0, None
    for cle, numero, heures in a_relancer(base, profil)[:MAX_NOTIFS_PAR_PASSAGE]:
        annonce = Annonce(**json.loads(base.cx.execute("SELECT annonce FROM annonces WHERE cle = ?",
                                                       (cle,)).fetchone()[0]))
        if annonce.source == "leboncoin":
            client = client or nouveau_client()
        en_ligne = annonce_en_ligne(annonce, client)
        maintenant = _maintenant().isoformat()
        if en_ligne is None:
            continue
        if en_ligne is False:
            if not a_blanc:
                base.cx.execute("INSERT INTO evenements (candidature, le, type, detail) VALUES (?, ?, ?, ?)",
                                (cle, maintenant, "annonce_retiree", ""))
                base.cx.execute("UPDATE candidatures SET maj_le = ?, prochaine_action = ? WHERE cle = ?",
                                (maintenant, "Annonce retirée : plus de relance", cle))
                base.cx.commit()
            log.info("Annonce retirée, pas de relance : %s", cle)
            continue
        message = texte_relance(profil, annonce, numero)
        contenu = notif.composer_relance(annonce, message, numero, profil["relances"]["max"], heures,
                                         profil["notif"].get("page_ecrire"), canal_signalement())
        if a_blanc:
            print("\n" + "=" * 60 + "\n" + contenu["title"] + "\n" + contenu["message"])
            continue
        notif.envoyer(contenu)
        base.cx.execute("INSERT INTO evenements (candidature, le, type, detail) VALUES (?, ?, ?, ?)",
                        (cle, maintenant, "relance_proposee", str(numero)))
        base.cx.commit()
        envoyees += 1
        log.info("Relance %d proposée : %s", numero, cle)
    return envoyees


if __name__ == "__main__":
    from nest.__main__ import charger_profil
    from nest.base import Base

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    p, b = charger_profil(), Base()
    dues = a_relancer(b, p)
    print(f"{len(dues)} relance(s) due(s) maintenant :", dues)
    traiter(p, b, a_blanc=True)
