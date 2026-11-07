"""Notification push sur Mac et iPhone via ntfy (https://ntfy.sh).

On publie en JSON (et pas en en-têtes HTTP) pour que les accents passent sans
encodage particulier. Le nom du canal (NTFY_TOPIC) fait office de mot de passe :
il doit rester aléatoire et secret.
"""

import base64
import json
import logging
import os
import zlib
from urllib.parse import quote, urlparse

import requests

log = logging.getLogger(__name__)

# Limite imposée par ntfy : au-delà, la notif est rejetée en 400.
MAX_BOUTONS = 3


def _ligne_trajet(trajet, position_precise: bool) -> str:
    """Ex. : '22 min · Parmentier à 4 min à pied · M3 › M11 · 1 changement'."""
    approx = "≈" if (not position_precise or trajet.source != "google") else ""
    if trajet.mode == "marche":
        return f"{approx}{trajet.minutes_marche} min à pied (compte comme {trajet.minutes} min)"
    if trajet.mode == "moto":
        return (f"{approx}{trajet.minutes_moto} min en moto (parking mentionné), "
                f"{trajet.minutes_transports} min en transports")
    morceaux = [f"{approx}{trajet.minutes} min"]
    if trajet.station:
        morceaux.append(f"{trajet.station} à {trajet.marche_station_min} min à pied")
    if trajet.lignes:
        morceaux.append(" › ".join(trajet.lignes))
    if trajet.changements is not None:
        morceaux.append("direct" if trajet.changements == 0
                        else f"{trajet.changements} changement" + ("s" if trajet.changements > 1 else ""))
    return " · ".join(morceaux)


def lien_ecrire(annonce, message: str, page: str, canal_signal: str | None = None) -> str:
    """Lien vers la page pont qui copie le message et ouvre le formulaire Leboncoin.

    Le message voyage compressé dans le fragment (#…) : le navigateur ne l'envoie
    jamais au serveur, rien n'est stocké nulle part. Hors Leboncoin, la charge
    porte l'URL de l'annonce (« u ») : la page pont ouvre la fiche, où l'on
    clique sur Contacter.
    """
    categorie = urlparse(annonce.url).path.split("/")[2] if annonce.url.count("/") >= 5 else "locations"
    donnees = {"i": annonce.id_source, "c": categorie, "t": annonce.titre[:80], "m": message,
               "s": canal_signal}
    if annonce.source != "leboncoin":
        donnees["u"] = annonce.url
    charge = json.dumps(donnees, ensure_ascii=False, separators=(",", ":")).encode()
    compresseur = zlib.compressobj(9, zlib.DEFLATED, -15)          # deflate « brut »
    brut = compresseur.compress(charge) + compresseur.flush()
    return page.rstrip("/") + "/#" + base64.urlsafe_b64encode(brut).decode().rstrip("=")


def composer(annonce, trajet, verdict, score: int, lignes: list,
             page_ecrire: str | None = None, canal_signal: str | None = None) -> dict:
    """Construit le contenu de la notif (titre, corps, boutons, photo).

    « Coche » = critères bien satisfaits, les plus lourds d'abord ;
    « coince » = critères ratés qui pèsent, plus les alertes de Claude.
    """
    surface = f"{annonce.surface_m2:.0f} m² · " if annonce.surface_m2 else ""
    titre = f"{score}/100 · {surface}{annonce.loyer_cc:.0f} € CC · {trajet.minutes} min"

    par_poids = sorted(lignes, key=lambda l: -l.poids)
    coche = [l.libelle for l in par_poids if l.satisfaction >= 0.75][:4]
    coince = [f"{l.libelle} ({l.preuve})" if l.preuve and l.preuve != "inconnu" else l.libelle
              for l in par_poids if l.satisfaction <= 0.25 and l.poids >= 2][:4]

    # Lien en clair dans le texte : le « click » et les boutons de ntfy sont
    # invisibles sur iPhone sans appui long.
    corps = [verdict.resume, _ligne_trajet(trajet, annonce.position_precise),
             f"🔗 {annonce.url}"]
    if not annonce.position_precise:
        corps.append("Position approximative" + (f", station citée : {verdict.station_citee}"
                                                 if verdict.station_citee else ""))
    corps += [f"✅ {c}" for c in coche]
    corps += [f"⚠️ {c}" for c in coince]
    corps += [f"🚩 {a}" for a in verdict.alertes]
    corps += ["", "Message proposé :", verdict.message]
    ecrire = lien_ecrire(annonce, verdict.message, page_ecrire, canal_signal) if page_ecrire else None
    if ecrire:
        corps += ["", f"✉️ Copier le message et écrire au bailleur : {ecrire}"]

    itineraire = ("https://www.google.com/maps/dir/?api=1&travelmode=transit"
                  f"&origin={annonce.lat},{annonce.lng}&destination=")
    actions = ([{"action": "view", "label": "Écrire", "url": ecrire}] if ecrire else []) + [
        {"action": "view", "label": "Annonce", "url": annonce.url},
        {"action": "view", "label": "Itinéraire", "url": itineraire},
    ] + ([{"action": "http", "label": "Arnaque", "method": "POST", "clear": True,
           "url": f"{os.getenv('NTFY_SERVEUR', 'https://ntfy.sh').rstrip('/')}/{canal_signal}",
           "body": f"arnaque {annonce.id_source}"}] if canal_signal else [])
    # ntfy refuse plus de 3 boutons (erreur 400) : on sacrifie l'itinéraire,
    # le trajet est déjà détaillé dans le corps de la notif.
    if len(actions) > MAX_BOUTONS:
        actions = [a for a in actions if a["label"] != "Itinéraire"]
    return {
        "title": titre,
        "message": "\n".join(corps),
        "click": ecrire or annonce.url,   # toucher la notif = écrire au bailleur
        "attach": annonce.photos[0] if annonce.photos else None,
        "tags": ["house"],
        "priority": 4 if score >= 80 else 3,
        "actions": actions,
    }


def composer_doublon(annonce, original: dict, site: str, site_original: str) -> dict:
    """Notif courte : un logement déjà notifié réapparaît sur un autre site.

    `original` vient de doublons.trouver() ; ex. de titre :
    « 🔁 Aussi sur Gens de Confiance · 830 € CC · 19 m² ».
    """
    surface = f" · {annonce.surface_m2:.0f} m²" if annonce.surface_m2 else ""
    titre_original = original["annonce"].get("titre") or "ce logement"
    return {
        "title": f"🔁 Aussi sur {site} · {annonce.loyer_cc:.0f} € CC{surface}",
        "message": (f"« {titre_original[:80]} », notifié le {original['vue_le'][8:10]}/"
                    f"{original['vue_le'][5:7]} sur {site_original}, est aussi publié sur {site}. "
                    f"Une seconde porte pour contacter le bailleur.\n🔗 {annonce.url}"),
        "click": annonce.url,
        "tags": ["repeat"],
        "priority": 2,
        "actions": [{"action": "view", "label": "Annonce", "url": annonce.url},
                    {"action": "view", "label": "Original", "url": original["annonce"].get("url", "")}],
    }


TITRES_MAIL = {
    "demande_dossier": "📁 Dossier demandé",
    "proposition_visite": "📅 Visite proposée",
    "question": "❓ Question d'agence",
    "refus": "❌ Refus",
    "relance_agence": "🔁 Relance d'agence",
    "confirmation": "✔️ Confirmation",
    "autre_logement": "📩 Mail logement",
}


def composer_mail(mail, classement, candidature: dict, signaux: list[str]) -> dict:
    """Notif pour un mail d'agence : ce qu'il dit, ce qu'il faut faire, les risques.

    Une arnaque probable passe avant tout le reste : titre 🚩 et consigne de ne rien envoyer.
    """
    arnaque = candidature["risque"] == "eleve"
    qui = classement.agence or classement.interlocuteur or mail.expediteur
    titre = ("🚩 Arnaque probable" if arnaque else TITRES_MAIL.get(classement.categorie, "📩 Mail logement"))
    corps = [classement.resume]
    if classement.bien:
        corps.append(f"🏠 {classement.bien}")
    if arnaque:
        corps.append("👉 N'envoie aucune pièce et ne clique pas sur leurs liens.")
    elif classement.action_a_faire and classement.action_a_faire.lower() != "rien":
        corps.append(f"👉 {classement.action_a_faire}")
    corps += [f"📎 {p}" for p in classement.pieces_demandees[:5]]
    corps += [f"📅 {c}" for c in classement.creneaux_visite[:4]]
    corps += [f"🚩 {s}" for s in signaux[:5]]
    if candidature["risque"] == "moyen" and not signaux:
        corps.append("⚠️ Prudence : à vérifier avant d'envoyer quoi que ce soit")
    corps += ["", f"📩 {mail.url}"]
    return {
        "title": f"{titre} · {qui}",
        "message": "\n".join(corps),
        "click": mail.url,
        "tags": ["warning"] if arnaque else ["envelope"],
        "priority": 4 if arnaque or classement.categorie in ("demande_dossier", "proposition_visite") else 3,
        "actions": [{"action": "view", "label": "Ouvrir le mail", "url": mail.url}]
                   + ([{"action": "view", "label": "Annonce", "url": candidature["url_annonce"]}]
                      if candidature.get("url_annonce") else []),
    }


def envoyer(contenu: dict, destination: str | None = None) -> None:
    """Publie la notif sur le canal ntfy configuré.

    Exemple :
        >>> envoyer(composer(annonce, trajet, verdict), profil["recherche"]["bureaux"]["adresse"])
    """
    topic = os.environ["NTFY_TOPIC"]
    serveur = os.getenv("NTFY_SERVEUR", "https://ntfy.sh")
    contenu = {k: v for k, v in contenu.items() if v is not None}
    # Garde-fou : un bouton de trop ne doit jamais faire perdre la notif entière.
    if len(contenu.get("actions", [])) > MAX_BOUTONS:
        contenu["actions"] = contenu["actions"][:MAX_BOUTONS]
    # L'adresse des bureaux complète le lien d'itinéraire.
    for action in contenu.get("actions", []):
        if action["label"] == "Itinéraire":
            action["url"] += quote(destination)
    rep = requests.post(serveur, json={"topic": topic, **contenu}, timeout=15)
    if rep.status_code == 400:
        # ntfy refuse le format (boutons, photo, lien trop long…) : on renvoie le texte seul,
        # qui contient déjà les liens en clair. Mieux vaut une notif sobre que pas de notif.
        log.warning("ntfy refuse la notif (%s), renvoi sans boutons ni photo", rep.text[:200])
        sobre = {k: contenu[k] for k in ("title", "message", "tags", "priority") if k in contenu}
        rep = requests.post(serveur, json={"topic": topic, **sobre}, timeout=15)
    rep.raise_for_status()
