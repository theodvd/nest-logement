"""Un passage sur la boîte mail : lire → préfiltrer → classer → suivre → notifier.

Appelé par `python -m nest` après les annonces, si Gmail a été autorisé (sinon ignoré).
Rien n'est envoyé à qui que ce soit : lecture seule côté Gmail, notifs à l'utilisateur seul.

Exemples d'appel :
    python -m nest --mails-seulement          # passage mails seul
    python -m nest --mails-seulement --sec    # à blanc : affiche, n'enregistre ni ne notifie
"""

import logging

from nest import notif, notion, suivi
from nest.mails import classement as tri
from nest.mails import gmail

log = logging.getLogger("nest.mails")

# Catégories qui méritent une notif (les autres vont seulement au suivi).
A_NOTIFIER = {"demande_dossier", "proposition_visite", "question", "refus"}


def doit_notifier(categorie: str, candidature: dict, deja_signale: bool) -> bool:
    """Une notif par action à faire ; une seule alerte par interlocuteur suspect."""
    if candidature["risque"] == "eleve":
        return not deja_signale
    return categorie in A_NOTIFIER or candidature["risque"] == "moyen"


def traiter(profil: dict, base, a_blanc: bool = False, client_gmail=None) -> None:
    """Exécute un passage complet sur les mails récents."""
    reglages = profil.get("mails", {})
    client_gmail = client_gmail or gmail.service()
    mails = [m for m in gmail.recents(client_gmail, jours=reglages.get("fenetre_jours", 3))
             if not suivi.deja_lu(base, m.id)]
    mails.sort(key=lambda m: m.date)                    # du plus ancien au plus récent
    connus = suivi.domaines_suivis(base)

    a_classer = []
    for mail in mails:
        if tri.concerne_logement(mail, connus):
            a_classer.append(mail)
        elif not a_blanc:
            suivi.noter_mail(base, mail, "hors_sujet")
    plafond = reglages.get("max_classements_par_passage", 10)
    log.info("Mails : %d nouveaux, %d liés au logement, %d classés ce passage",
             len(mails), len(a_classer), min(len(a_classer), plafond))

    # Au-delà du plafond, les mails ne sont pas notés : ils seront repris au passage suivant.
    for mail in a_classer[:plafond]:
        classement, _ = tri.classer(mail, profil)
        if not classement.concerne_logement or classement.categorie == "hors_sujet":
            if not a_blanc:
                suivi.noter_mail(base, mail, "hors_sujet", classement=classement)
            continue

        signaux = tri.fusionner_signaux(tri.signaux_code(mail, classement), classement.signaux_arnaque)
        deja_signale = suivi.contact_signale(base, mail.expediteur)
        if deja_signale:
            signaux.insert(0, "interlocuteur déjà signalé comme suspect")
        risque = "eleve" if deja_signale else tri.risque_final(classement, signaux)

        if a_blanc:
            faux = {"risque": risque, "url_annonce": None}
            contenu = notif.composer_mail(mail, classement, faux, signaux)
            print("\n" + "=" * 60)
            print(f"[{classement.categorie} · risque {risque}]  {contenu['title']}")
            print(contenu["message"])
            continue

        cle, url_annonce = suivi.rattacher(base, mail, classement)
        candidature = suivi.mettre_a_jour(base, cle, mail, classement, risque, url_annonce)
        if page := notion.publier(candidature):
            suivi.noter_notion(base, cle, page)
        if doit_notifier(classement.categorie, candidature, deja_signale):
            # Si l'envoi échoue, l'exception remonte avant noter_mail : le mail sera repris.
            notif.envoyer(notif.composer_mail(mail, classement, candidature, signaux))
        suivi.noter_mail(base, mail, classement.categorie, risque, cle, classement)
        log.info("Mail %s → %s (%s, risque %s)", mail.id, cle, candidature["statut"], risque)
