"""Point d'entrée : un passage complet collecte → filtres → trajet → jugement → notif.

Les clés sont lues dans `.env` (à la racine du dépôt) ; une variable déjà définie
dans l'environnement passe devant (ex. `op run --env-file=… -- python -m nest`).

Exemples d'appel :
    python -m nest                 # passage normal (c'est ce que lance la tâche planifiée)
    python -m nest --verifier      # vérifie le profil et chaque clé, sans rien envoyer
    python -m nest --sec --max 3   # à blanc : affiche les notifs sans rien envoyer ni enregistrer
    python -m nest --amorcer       # marque l'existant comme vu, sans notifier
    python -m nest --test-notif    # envoie une notif de test pour vérifier le téléphone
    python -m nest --signaler <lien de l'annonce>   # bloque le compte (arnaque)
    python -m nest --mails-seulement --sec   # tri des mails à blanc (si Gmail est autorisé)
    python -m nest --sans-mails    # annonces seules
    python -m nest --profil profil.example.yaml --sec   # avec un autre profil
"""

import argparse
import json
import logging
import os
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

import requests
import yaml

from nest import doublons, environs as module_environs, jugement, notif, notion, trajet as module_trajet, vendeurs
from nest.annonce import Annonce
from nest.base import Base
from nest.filtres import raison_ecart
from nest.mails import gmail, passage as passage_mails
from nest.sources import gensdeconfiance, leboncoin

RACINE = Path(__file__).resolve().parent.parent
log = logging.getLogger("nest")
DERNIER_PASSAGE = RACINE / "data" / "dernier_passage.txt"
TROU_MAX_MIN = 40   # au-delà, le passage suivant fait un rattrapage
# Gens de Confiance est interrogé avec le compte de l'utilisateur : rythme plus bas que Leboncoin.
DERNIER_PASSAGE_GDC = RACINE / "data" / "dernier_passage_gdc.txt"
GDC_INTERVALLE_MIN = 30
ALERTE_SESSION_GDC = RACINE / "data" / "alerte_session_gdc.txt"


class ConfigIncomplete(Exception):
    """Profil ou clé manquants : le message dit quoi faire."""


def charger_env(chemin: Path = RACINE / ".env") -> None:
    """Charge `.env` (CLE=valeur) sans écraser les variables déjà définies."""
    if not chemin.exists():
        return
    for ligne in chemin.read_text(encoding="utf-8").splitlines():
        ligne = ligne.strip()
        if not ligne or ligne.startswith("#") or "=" not in ligne:
            continue
        cle, valeur = ligne.split("=", 1)
        valeur = valeur.strip()
        if len(valeur) >= 2 and valeur[0] == valeur[-1] and valeur[0] in "\"'":
            valeur = valeur[1:-1]
        else:
            valeur = re.split(r"(?:^|\s)#", valeur, maxsplit=1)[0].strip()   # commentaire en fin de ligne
        if valeur:
            os.environ.setdefault(cle.strip(), valeur)


def charger_profil(chemin: Path = RACINE / "profil.yaml") -> dict:
    """Lit le profil de recherche (seule source des critères)."""
    if not chemin.exists():
        raise ConfigIncomplete(
            f"{chemin.name} introuvable. Lance l'entretien dans Claude Code (/entretien-recherche) "
            "pour le créer, ou copie profil.example.yaml en profil.yaml et adapte-le.")
    with open(chemin, encoding="utf-8") as f:
        profil = yaml.safe_load(f)
    manquants = [c for c in ("recherche", "preferences", "locataire", "criteres", "notif") if c not in (profil or {})]
    if manquants:
        raise ConfigIncomplete(f"{chemin.name} incomplet, sections absentes : {', '.join(manquants)}")
    return profil


# Clé → (indispensable pour quoi, où l'obtenir).
CLES = {
    "ANTHROPIC_API_KEY": ("juger les annonces", "https://console.anthropic.com/settings/keys"),
    "NTFY_TOPIC": ("envoyer les notifs", "un nom de canal aléatoire, ex. nest-7f3a9c2e41d8"),
    "GOOGLE_MAPS_API_KEY": ("calculer les vrais trajets", "Google Cloud, API « Routes » activée"),
}


def verifier_cles(a_blanc: bool = False, notif_seule: bool = False) -> None:
    """Lève ConfigIncomplete si une clé indispensable manque ; prévient pour les autres."""
    requises = ["NTFY_TOPIC"] if notif_seule else ["ANTHROPIC_API_KEY"] + ([] if a_blanc else ["NTFY_TOPIC"])
    erreurs = []
    for cle in requises:
        valeur = os.getenv(cle, "")
        if not valeur:
            usage, source = CLES[cle]
            erreurs.append(f"{cle} absente (sert à {usage}) : {source}. À mettre dans .env.")
        elif valeur.startswith("op://"):
            erreurs.append(f"{cle} est une référence 1Password non résolue : lance la commande avec "
                           "op run --env-file=.env -- …")
    if erreurs:
        raise ConfigIncomplete("\n".join(erreurs))
    if not notif_seule and not os.getenv("GOOGLE_MAPS_API_KEY"):
        log.warning("GOOGLE_MAPS_API_KEY absente : trajets estimés à vol d'oiseau, bien moins fiables.")


def verifier(profil: dict) -> bool:
    """Teste pour de vrai chaque clé présente (aucune notif envoyée). True si tout va bien."""
    import anthropic

    ok = True
    def ligne(etat: str, texte: str) -> None:
        print(f"  {etat}  {texte}")

    print(f"Profil : {profil['locataire']['prenom']}, {profil['recherche']['ville']}, "
          f"{len(profil.get('criteres', []))} critères, seuil {profil['notif']['score_min']}")
    if not os.getenv("ANTHROPIC_API_KEY"):
        ligne("✗", "ANTHROPIC_API_KEY absente")
        ok = False
    else:
        try:
            anthropic.Anthropic().models.retrieve(jugement.MODELE)
            ligne("✓", f"Anthropic : clé valide, modèle {jugement.MODELE} accessible")
        except anthropic.AuthenticationError:
            ligne("✗", "Anthropic : clé refusée (vérifie-la sur console.anthropic.com)")
            ok = False
        except anthropic.APIError as err:
            ligne("✗", f"Anthropic : {err}")
            ok = False
    if not os.getenv("GOOGLE_MAPS_API_KEY"):
        ligne("!", "GOOGLE_MAPS_API_KEY absente : trajets à vol d'oiseau")
    else:
        b = profil["recherche"]["bureaux"]
        try:
            module_trajet._appeler({**module_trajet._points(b["lat"] + 0.01, b["lng"], b), "travelMode": "WALK"},
                                   "routes.duration", os.environ["GOOGLE_MAPS_API_KEY"])
            ligne("✓", "Google Routes : clé valide")
        except requests.HTTPError as err:
            try:
                detail = err.response.json()["error"]["message"]
            except (AttributeError, ValueError, KeyError, TypeError):
                detail = err
            ligne("✗", f"Google Routes refuse la clé (API « Routes » activée ? facturation liée ?) : {detail}")
            ok = False
    ligne("✓" if os.getenv("NTFY_TOPIC") else "✗",
          "ntfy : canal configuré (python -m nest --test-notif pour essayer)" if os.getenv("NTFY_TOPIC")
          else "NTFY_TOPIC absent")
    ok = ok and bool(os.getenv("NTFY_TOPIC"))
    ligne("✓" if os.getenv("GDC_COOKIE") else "·",
          "Gens de Confiance : cookie présent" if os.getenv("GDC_COOKIE") else "Gens de Confiance : désactivé (pas de cookie)")
    ligne("✓" if gmail.actif() else "·", "Gmail : autorisé" if gmail.actif() else "Gmail : désactivé")
    ligne("✓" if notion.actif() else "·", "Notion : configuré" if notion.actif() else "Notion : désactivé")
    ligne("✓" if profil["notif"].get("page_ecrire") else "·",
          "Page « Écrire » : " + (profil["notif"].get("page_ecrire") or "désactivée (message dans la notif)"))
    return ok


def seuil_trajet(annonce, trajet, profil: dict) -> int:
    """Trajet max toléré : marge en plus si la position ou le calcul est approximatif."""
    r = profil["recherche"]
    flou = not annonce.position_precise or trajet.source != "google"
    return r["trajet_max_min"] + (r["marge_position_floue_min"] if flou else 0)


def trou_depuis_dernier_passage(fichier: Path = DERNIER_PASSAGE) -> timedelta | None:
    """Temps écoulé depuis la dernière lecture réussie d'une source (None si inconnue)."""
    try:
        dernier = datetime.fromisoformat(fichier.read_text().strip())
    except (OSError, ValueError):
        return None
    return datetime.now() - dernier


def alerter_session_gdc(erreur: Exception, a_blanc: bool) -> None:
    """Prévient que le cookie Gens de Confiance est à renouveler (une fois par jour)."""
    log.warning("Gens de Confiance ignoré : %s", erreur)
    if a_blanc or ((t := trou_depuis_dernier_passage(ALERTE_SESSION_GDC)) and t < timedelta(days=1)):
        return
    try:
        notif.envoyer({"title": "Gens de Confiance : cookie à renouveler",
                       "message": "nest ne voit plus les annonces Gens de Confiance. Reconnecte-toi "
                                  "sur le site et recopie le cookie dans .env (GDC_COOKIE). "
                                  "Leboncoin continue normalement.",
                       "tags": ["warning"]})
        ALERTE_SESSION_GDC.write_text(datetime.now().isoformat(timespec="seconds"))
    except requests.RequestException:
        log.exception("Alerte cookie Gens de Confiance non envoyée")


def annonces_gdc(profil: dict, a_blanc: bool) -> list[Annonce]:
    """Annonces Gens de Confiance, au plus toutes les 30 min, sans jamais bloquer Leboncoin.

    Même logique de rattrapage que Leboncoin, sur son propre horodatage.
    Sans GDC_COOKIE, la source est désactivée (aucune alerte).
    """
    if not os.getenv("GDC_COOKIE"):
        return []
    trou = trou_depuis_dernier_passage(DERNIER_PASSAGE_GDC)
    if trou is not None and trou < timedelta(minutes=GDC_INTERVALLE_MIN) and not a_blanc:
        return []
    depuis = None
    if trou and trou > timedelta(minutes=GDC_INTERVALLE_MIN + TROU_MAX_MIN):
        depuis = (datetime.now() - trou - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
    try:
        annonces = gensdeconfiance.chercher(profil, depuis=depuis)
    except gensdeconfiance.SessionExpiree as err:
        alerter_session_gdc(err, a_blanc)
        return []
    except Exception:
        log.exception("Gens de Confiance en échec, Leboncoin continue")
        return []
    if not a_blanc:
        DERNIER_PASSAGE_GDC.write_text(datetime.now().isoformat(timespec="seconds"))
    return annonces


def annonces_en_attente(base: Base) -> list[Annonce]:
    """Annonces mises de côté au-delà du plafond de jugements, à juger en priorité."""
    lignes = base.cx.execute("SELECT annonce FROM annonces WHERE statut = 'en_attente' "
                             "ORDER BY vue_le").fetchall()
    return [Annonce(**json.loads(brut)) for (brut,) in lignes]


def passage(profil: dict, base: Base, a_blanc: bool = False) -> None:
    """Exécute un passage complet sur toutes les sources."""
    if signales := vendeurs.lire_signalements(base):
        log.info("Comptes signalés et bloqués : %s", ", ".join(signales))
    # Rattrapage : après un trou (Mac en veille, coupure réseau), on lit Leboncoin
    # jusqu'à retrouver le dernier passage réussi (marge d'une heure), et le filtre
    # d'ancienneté s'allonge d'autant : une annonce jamais vue n'est jamais jetée.
    trou = trou_depuis_dernier_passage()
    depuis, anciennete_h = None, profil["recherche"]["anciennete_max_h"]
    if trou and trou > timedelta(minutes=TROU_MAX_MIN):
        depuis = (datetime.now() - trou - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
        anciennete_h += trou.total_seconds() / 3600
        log.info("Rattrapage : dernier passage il y a %.1f h, lecture jusqu'au %s",
                 trou.total_seconds() / 3600, depuis)
    # Chaque source est isolée : un blocage DataDome de Leboncoin (presque un passage
    # sur deux début novembre) ne doit pas priver nest de Gens de Confiance.
    try:
        annonces = leboncoin.chercher(profil, depuis=depuis)
        if not a_blanc:
            # Noté seulement après une lecture réussie : une coupure réseau déclenche le rattrapage.
            DERNIER_PASSAGE.write_text(datetime.now().isoformat(timespec="seconds"))
    except Exception:
        log.exception("Leboncoin en échec, les autres sources continuent")
        annonces = []
    annonces += annonces_gdc(profil, a_blanc)
    nouvelles = [a for a in annonces if not base.deja_vue(a.cle)]
    attente = annonces_en_attente(base)
    log.info("%d nouvelles annonces sur %d, %d en attente de jugement",
             len(nouvelles), len(annonces), len(attente))

    # 1. Filtres durs + trajet : tout ce qui ne coûte (presque) rien.
    #    Les annonces en attente ont déjà passé les filtres : on ne les refiltre pas.
    candidates = []
    cles_attente = {a.cle for a in attente}
    for annonce in attente + nouvelles:
        if annonce.cle not in cles_attente and (raison := raison_ecart(annonce, profil, anciennete_max_h=anciennete_h)):
            base.enregistrer(annonce, "ecartee", raison=raison)
            continue
        t = module_trajet.calculer(annonce, profil, base)
        if t.minutes > seuil_trajet(annonce, t, profil):
            base.enregistrer(annonce, "ecartee", raison=f"trajet {t.minutes} min", trajet_min=t.minutes)
            continue
        vendeur = vendeurs.profil_vendeur(annonce.vendeur_id, base, annonce=annonce)
        if vendeurs.est_bloque(vendeur, base):
            base.enregistrer(annonce, "ecartee", raison=f"compte signalé ({vendeur.get('nom')})")
            continue
        candidates.append((annonce, t, vendeur))

    # 2. Jugement Claude, les plus proches d'abord, plafonné par passage.
    #    Au-delà du plafond, les annonces sont mises « en attente » dans la base
    #    (contenu complet) et jugées en priorité aux passages suivants.
    candidates.sort(key=lambda c: c[1].minutes)
    plafond = profil["notif"]["max_jugements_par_passage"]
    log.info("%d candidates après filtres, %d jugées ce passage",
             len(candidates), min(len(candidates), plafond))
    if not a_blanc:
        for annonce, t, _ in candidates[plafond:]:
            base.enregistrer(annonce, "en_attente", trajet_min=t.minutes)

    destination = profil["recherche"]["bureaux"]["adresse"]
    for annonce, t, vendeur in candidates[:plafond]:
        # Gens de Confiance : la description et la disponibilité ne sont que sur la fiche,
        # ouverte maintenant seulement (une requête par candidate), puis on refiltre.
        if annonce.source == gensdeconfiance.SOURCE and not annonce.description:
            try:
                gensdeconfiance.completer(annonce)
            except Exception as err:
                log.warning("Fiche Gens de Confiance illisible, jugée sans description : %s (%s)",
                            annonce.url, err)
            if raison := raison_ecart(annonce, profil, anciennete_max_h=anciennete_h):
                base.enregistrer(annonce, "ecartee", raison=raison, trajet_min=t.minutes)
                continue
        # Même logement déjà jugé (autre site ou republication) : ni second jugement ni
        # seconde notif. Seule exception, un logement notifié qui réapparaît sur un autre
        # site : notif courte « aussi sur ».
        if original := doublons.trouver(annonce, base):
            site, site_original = (doublons.nom_site(annonce.source),
                                   doublons.nom_site(original["annonce"]["source"]))
            base.enregistrer(annonce, "doublon", trajet_min=t.minutes,
                             raison=f"doublon de {original['cle']} ({original['statut']})")
            if original["statut"] == "notifiee" and site != site_original:
                contenu = notif.composer_doublon(annonce, original, site, site_original)
                if a_blanc:
                    print("\n" + "=" * 60 + "\nDOUBLON  " + contenu["title"] + "\n" + contenu["message"])
                else:
                    try:
                        notif.envoyer(contenu, destination)
                    except requests.RequestException:
                        log.exception("Notif « aussi sur » non envoyée : %s", annonce.url)
            continue
        environs = {**module_environs.analyser(annonce, base, profil), "vendeur": vendeur}
        verdict, score, lignes, _ = jugement.juger(annonce, t, profil, environs)
        contenu = notif.composer(annonce, t, verdict, score, lignes,
                                 page_ecrire=profil["notif"].get("page_ecrire"),
                                 canal_signal=vendeurs.canal_signalement())
        notifier = score >= profil["notif"]["score_min"]

        if a_blanc:
            print("\n" + "=" * 60)
            print(("NOTIF  " if notifier else "SOUS LE SEUIL  ") + contenu["title"])
            print(contenu["message"])
            print(annonce.url)
            continue

        if notifier:
            try:
                notif.envoyer(contenu, destination)
            except requests.RequestException:
                # En attente : l'annonce sera rejugée et renotifiée au passage suivant.
                log.exception("Notif non envoyée, reprise au prochain passage : %s", annonce.url)
                base.enregistrer(annonce, "en_attente", trajet_min=t.minutes)
                continue
        base.enregistrer(annonce, "notifiee" if notifier else "jugee",
                         score=score, trajet_min=t.minutes,
                         verdict=verdict.model_dump())


def main() -> None:
    parser = argparse.ArgumentParser(description="nest : veille logement")
    parser.add_argument("--sec", action="store_true", help="à blanc, rien n'est envoyé ni enregistré")
    parser.add_argument("--verifier", action="store_true", help="vérifie le profil et les clés, sans rien envoyer")
    parser.add_argument("--amorcer", action="store_true", help="marque l'existant comme vu")
    parser.add_argument("--test-notif", action="store_true", help="envoie une notif de test")
    parser.add_argument("--max", type=int, help="plafond de jugements Claude pour ce passage")
    parser.add_argument("--signaler", metavar="LIEN", help="bloque le compte qui a publié cette annonce")
    parser.add_argument("--mails-seulement", action="store_true", help="passage mails seul, sans annonces")
    parser.add_argument("--sans-mails", action="store_true", help="annonces seules, sans les mails")
    parser.add_argument("--profil", type=Path, default=RACINE / "profil.yaml", help="fichier de profil")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    charger_env()
    try:
        profil = charger_profil(args.profil.resolve())
        if args.verifier:
            sys.exit(0 if verifier(profil) else 1)
        if args.test_notif:
            verifier_cles(notif_seule=True)
        elif not (args.signaler or args.amorcer):
            verifier_cles(a_blanc=args.sec)
    except ConfigIncomplete as err:
        print(f"nest ne peut pas démarrer :\n{err}", file=sys.stderr)
        sys.exit(2)
    if args.max is not None:
        profil["notif"]["max_jugements_par_passage"] = args.max

    if args.test_notif:
        notif.envoyer({"title": "nest est branché", "message": "Les notifs logement arriveront ici.",
                       "tags": ["house"]}, profil["recherche"]["bureaux"]["adresse"])
        return

    base = Base(Path(":memory:")) if args.sec else Base()

    if args.signaler:
        print("Compte bloqué :", vendeurs.bloquer(args.signaler, base))
        return

    if args.amorcer:
        annonces = leboncoin.chercher(profil)
        if os.getenv("GDC_COOKIE"):
            try:
                annonces += gensdeconfiance.chercher(profil)
            except Exception as err:
                log.warning("Amorçage sans Gens de Confiance : %s", err)
        for a in annonces:
            base.enregistrer(a, "amorcage")
        log.info("Amorçage : %d annonces marquées comme vues", len(annonces))
        return

    # Annonces et mails sont indépendants : une panne de l'un ne bloque pas l'autre.
    if not args.mails_seulement:
        try:
            passage(profil, base, a_blanc=args.sec)
        except Exception:
            log.exception("Passage annonces en échec")
    # Mails : seulement si Gmail a été autorisé (module facultatif).
    if args.mails_seulement and not gmail.actif():
        print("Gmail n'est pas autorisé : voir python -m nest.mails.gmail --autoriser", file=sys.stderr)
    elif not args.sans_mails and gmail.actif():
        try:
            passage_mails.traiter(profil, base, a_blanc=args.sec)
        except gmail.JetonAbsent as e:
            log.info("Mails ignorés : %s", e)
        except Exception:
            log.exception("Passage mails en échec")


if __name__ == "__main__":
    main()
