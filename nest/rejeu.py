"""Rejeu : passe le dernier mois d'annonces dans le pipeline pour calibrer le scoring.

On répond à « combien de notifs j'aurais reçues, et lesquelles ? » sans attendre
des semaines. Le rejeu a sa propre table (`rejeu`) : il ne touche pas à la veille.
Il partage en revanche le cache des trajets et des environs avec elle.

Attention : `collecter` lit jusqu'à 40 pages par catégorie d'un coup, bien plus
qu'un passage normal. À lancer rarement (une fois, pour calibrer le seuil), et
`juger` sur tout le mois coûte quelques dollars de Claude (voir --max).

Exemples d'appel (dans l'ordre) :
    python -m nest.rejeu collecter --jours 30   # Leboncoin, toutes les pages
    python -m nest.rejeu preparer               # filtres + trajets + environs (sans Claude)
    python -m nest.rejeu juger --max 5          # échantillon pour mesurer le coût
    python -m nest.rejeu juger                  # toutes les candidates restantes
    python -m nest.rejeu messages --score-min 60  # régénère les messages (consignes changées)
    python -m nest.rejeu rapport                # chiffres + export JSON pour le rapport
"""

import argparse
import json
import logging
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from datetime import datetime, timedelta
from pathlib import Path

import anthropic

from nest import environs as module_environs, jugement, trajet as module_trajet
from nest.__main__ import charger_profil, seuil_trajet
from nest.annonce import Annonce
from nest.base import Base
from nest.filtres import mois_dispo, raison_ecart
from nest.sources import leboncoin
from nest.trajet import Trajet

log = logging.getLogger("nest.rejeu")
RACINE = Path(__file__).resolve().parent.parent
EXPORT = RACINE / "data" / "rejeu.json"
MODELE = RACINE / "rapport" / "modele.html"
PAGE = RACINE / "rapport" / "rejeu.html"

SCHEMA = """
CREATE TABLE IF NOT EXISTS rejeu (
    cle      TEXT PRIMARY KEY,
    publiee  TEXT,
    statut   TEXT NOT NULL,      -- collectee / ecartee / candidate / jugee
    raison   TEXT,
    annonce  TEXT NOT NULL,
    trajet   TEXT,
    environs TEXT,
    score    INTEGER,
    verdict  TEXT,
    lignes   TEXT,
    cout_usd REAL
);
"""


def _base() -> Base:
    """Base principale (pour le cache partagé) + table du rejeu."""
    base = Base()
    base.cx.executescript(SCHEMA)
    return base


def collecter(profil: dict, jours: int) -> None:
    """Récupère toutes les annonces publiées dans les `jours` derniers jours."""
    base = _base()
    depuis = (datetime.now() - timedelta(days=jours)).strftime("%Y-%m-%d %H:%M:%S")
    annonces = leboncoin.chercher(profil, pages=40)
    gardees = {a.cle: a for a in annonces if a.publiee_le >= depuis}
    for a in gardees.values():
        base.cx.execute("INSERT OR IGNORE INTO rejeu (cle, publiee, statut, annonce) VALUES (?, ?, ?, ?)",
                        (a.cle, a.publiee_le, "collectee", json.dumps(a.to_dict(), ensure_ascii=False)))
    base.cx.commit()
    log.info("%d annonces récupérées, %d publiées depuis le %s", len(annonces), len(gardees), depuis[:10])


def preparer(profil: dict) -> None:
    """Filtres durs, trajet et environs, sans Claude. Reprend là où il s'est arrêté."""
    base = _base()
    profil["recherche"]["anciennete_max_h"] = 10 ** 6   # le rejeu porte justement sur le passé
    lignes = base.cx.execute("SELECT cle, annonce FROM rejeu WHERE statut = 'collectee'").fetchall()
    log.info("%d annonces à préparer", len(lignes))
    for i, (cle, brut) in enumerate(lignes, 1):
        a = Annonce(**json.loads(brut))
        if raison := raison_ecart(a, profil):
            base.cx.execute("UPDATE rejeu SET statut='ecartee', raison=? WHERE cle=?", (raison, cle))
        else:
            t = module_trajet.calculer(a, profil, base)
            if t.minutes > seuil_trajet(a, t, profil):
                base.cx.execute("UPDATE rejeu SET statut='ecartee', raison=?, trajet=? WHERE cle=?",
                                (f"trajet {t.minutes} min", json.dumps(t.to_dict()), cle))
            else:
                env = module_environs.analyser(a, base, profil)
                base.cx.execute("UPDATE rejeu SET statut='candidate', trajet=?, environs=? WHERE cle=?",
                                (json.dumps(t.to_dict()), json.dumps(env, ensure_ascii=False), cle))
        base.cx.commit()
        if i % 100 == 0:
            log.info("… %d / %d", i, len(lignes))
    _resume(base)


def _priorite(disponible_le: str | None, au_plus_tot: str | None) -> int:
    """0 = dispo à partir de `dispo_au_plus_tot` du profil, 1 = non précisée, 2 = dispo avant."""
    am = mois_dispo(disponible_le)
    if am is None:
        return 1
    if not au_plus_tot:
        return 0
    return 0 if am >= tuple(map(int, au_plus_tot.split("-"))) else 2


def refiltrer(profil: dict) -> None:
    """Réapplique les filtres gratuits aux candidates (après ajout d'un filtre)."""
    base = _base()
    profil["recherche"]["anciennete_max_h"] = 10 ** 6
    n = 0
    for cle, brut in base.cx.execute("SELECT cle, annonce FROM rejeu WHERE statut='candidate'").fetchall():
        if raison := raison_ecart(Annonce(**json.loads(brut)), profil):
            base.cx.execute("UPDATE rejeu SET statut='ecartee', raison=? WHERE cle=?", (raison, cle))
            n += 1
    base.cx.commit()
    log.info("%d candidates écartées par les nouveaux filtres", n)
    _resume(base)


def juger(profil: dict, maximum: int | None, workers: int) -> None:
    """Fait juger les candidates par Claude (en parallèle), avec suivi du coût."""
    base = _base()
    lignes = base.cx.execute(
        "SELECT cle, annonce, trajet, environs FROM rejeu WHERE statut = 'candidate'").fetchall()
    # Priorité : dispo au bon moment, puis dispo non précisée, puis dispo trop tôt.
    au_plus_tot = profil["recherche"].get("dispo_au_plus_tot")
    lignes.sort(key=lambda l: _priorite(json.loads(l[1]).get("disponible_le"), au_plus_tot))
    lignes = lignes[:maximum] if maximum else lignes
    client = anthropic.Anthropic()
    log.info("%d candidates à juger (%d en parallèle)", len(lignes), workers)

    def tache(ligne):
        cle, brut, t_brut, env_brut = ligne
        a, t, env = Annonce(**json.loads(brut)), Trajet(**json.loads(t_brut)), json.loads(env_brut)
        verdict, score, detail, usage = jugement.juger(a, t, profil, env, client=client)
        return cle, verdict, score, detail, jugement.cout_usd(usage)

    total = 0.0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(tache, l) for l in lignes]
        for i, f in enumerate(as_completed(futures), 1):
            try:
                cle, verdict, score, detail, cout = f.result()
            except anthropic.APIError as err:
                log.warning("Jugement en échec, sera repris : %s", err)
                continue
            total += cout
            base.cx.execute(
                "UPDATE rejeu SET statut='jugee', score=?, verdict=?, lignes=?, cout_usd=? WHERE cle=?",
                (score, verdict.model_dump_json(), json.dumps([asdict(x) for x in detail], ensure_ascii=False),
                 cout, cle))
            base.cx.commit()
            if i % 25 == 0:
                log.info("… %d / %d jugées, %.2f $ jusqu'ici", i, len(lignes), total)
    log.info("Terminé : %.2f $ pour %d annonces (%.4f $ / annonce)",
             total, len(lignes), total / max(len(lignes), 1))
    _resume(base)


def messages(profil: dict, score_min: int, maximum: int | None, workers: int,
             contenant: str | None = None) -> None:
    """Régénère seulement le message des annonces jugées (après changement des consignes).

    `contenant` : ne reprend que les messages contenant ce texte (ex. reprise après échecs).
    """
    base = _base()
    lignes = base.cx.execute(
        "SELECT cle, annonce, verdict FROM rejeu WHERE statut='jugee' AND score >= ? "
        "AND (? IS NULL OR json_extract(verdict, '$.message') LIKE '%' || ? || '%') ORDER BY score DESC",
        (score_min, contenant, contenant)).fetchall()
    lignes = lignes[:maximum] if maximum else lignes
    client = anthropic.Anthropic()
    log.info("%d messages à régénérer", len(lignes))

    def tache(ligne):
        cle, brut, verdict = ligne
        message, usage = jugement.rediger(Annonce(**json.loads(brut)), profil, client=client)
        return cle, verdict, message, jugement.cout_usd(usage)

    total = 0.0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for f in as_completed([pool.submit(tache, l) for l in lignes]):
            try:
                cle, verdict, message, cout = f.result()
            except anthropic.APIError as err:
                log.warning("Message en échec : %s", err)
                continue
            total += cout
            v = json.loads(verdict)
            v["message"] = message
            base.cx.execute("UPDATE rejeu SET verdict=? WHERE cle=?", (json.dumps(v, ensure_ascii=False), cle))
            base.cx.commit()
    log.info("Terminé : %.2f $ pour %d messages", total, len(lignes))


def rescorer(profil: dict, score_min: int) -> None:
    """Ajoute le profil vendeur aux annonces jugées, écarte les comptes bloqués et recalcule
    le score avec les critères actuels, sans rappeler Claude (évaluations déjà stockées)."""
    import time
    from nest import scoring, vendeurs
    from nest.trajet import Trajet
    base = _base()
    client = leboncoin.nouveau_client()
    lignes = base.cx.execute("SELECT cle, annonce, trajet, environs, lignes FROM rejeu "
                             "WHERE statut='jugee' AND score >= ?", (score_min,)).fetchall()
    log.info("%d annonces à rescorer", len(lignes))
    bloquees = 0
    for cle, brut, t_brut, env_brut, l_brut in lignes:
        a = json.loads(brut)
        if not a.get("vendeur_id"):
            try:
                a["vendeur_id"] = client.get_ad(a["id_source"])._user_id
            except Exception:
                base.cx.execute("UPDATE rejeu SET statut='ecartee', raison='annonce retirée' WHERE cle=?", (cle,))
                base.cx.commit()
                continue
            time.sleep(0.7)
        vendeur = vendeurs.profil_vendeur(a["vendeur_id"], base, client)
        if vendeurs.est_bloque(vendeur, base):
            base.cx.execute("UPDATE rejeu SET statut='ecartee', raison=?, annonce=? WHERE cle=?",
                            (f"compte signalé ({vendeur.get('nom')})", json.dumps(a, ensure_ascii=False), cle))
            base.cx.commit()
            bloquees += 1
            continue
        env = {**json.loads(env_brut), "vendeur": vendeur}
        evaluations = {l["id"]: ({1.0: "oui", 0.5: "partiel", 0.0: "non"}.get(l["satisfaction"], "inconnu"), l["preuve"])
                       for l in json.loads(l_brut)}
        score, detail = scoring.calculer(profil["criteres"], evaluations, Annonce(**a),
                                         Trajet(**json.loads(t_brut)), env)
        base.cx.execute("UPDATE rejeu SET score=?, lignes=?, environs=?, annonce=? WHERE cle=?",
                        (score, json.dumps([asdict(x) for x in detail], ensure_ascii=False),
                         json.dumps(env, ensure_ascii=False), json.dumps(a, ensure_ascii=False), cle))
        base.cx.commit()
    log.info("Rescoring terminé, %d annonces de comptes bloqués écartées", bloquees)


def _categorie(raison: str) -> str:
    """Regroupe les raisons d'écart en catégories lisibles."""
    if raison.startswith("trajet"):
        return "Trajet réel trop long (Google)"
    if raison.startswith("trop loin"):
        return "Trop loin à vol d'oiseau"
    if raison.startswith("budget"):
        return "Au-dessus du budget"
    if raison.startswith("courte durée"):
        return "Courte durée (titre)"
    if raison.startswith("marque signalée"):
        return "Marque d'arnaque citée"
    if raison.startswith("compte signalé"):
        return "Compte signalé (arnaque)"
    if raison.startswith("annonce retirée"):
        return "Annonce retirée depuis"
    if raison.startswith("dispo trop tôt"):
        return "Dispo trop tôt"
    if raison.startswith("pas de position"):
        return "Pas de position"
    return raison.split(" (")[0]


def _resume(base: Base) -> None:
    """Affiche le décompte par statut et les principales raisons d'écart."""
    statuts = dict(base.cx.execute("SELECT statut, COUNT(*) FROM rejeu GROUP BY statut").fetchall())
    log.info("Statuts : %s", statuts)
    raisons = Counter(_categorie(r) for (r,) in
                      base.cx.execute("SELECT raison FROM rejeu WHERE statut='ecartee'").fetchall())
    log.info("Écarts : %s", dict(raisons.most_common()))


def rapport(profil: dict) -> None:
    """Exporte les annonces jugées et les statistiques dans data/rejeu.json."""
    base = _base()
    seuil = profil["notif"]["score_min"]
    jugees = base.cx.execute(
        "SELECT annonce, trajet, environs, score, verdict, lignes, publiee, cout_usd FROM rejeu "
        "WHERE statut='jugee' ORDER BY score DESC").fetchall()
    statuts = dict(base.cx.execute("SELECT statut, COUNT(*) FROM rejeu GROUP BY statut").fetchall())
    raisons = Counter(_categorie(r) for (r,) in
                      base.cx.execute("SELECT raison FROM rejeu WHERE statut='ecartee'").fetchall())
    dates = [p for (p,) in base.cx.execute("SELECT publiee FROM rejeu WHERE publiee IS NOT NULL")]
    debut, fin = min(dates), max(dates)
    jours = max((datetime.fromisoformat(fin) - datetime.fromisoformat(debut)).days, 1)

    def allegee(brut: str) -> dict:
        """Annonce sans la description ni les photos (inutiles au rapport, lourdes)."""
        a = json.loads(brut)
        a.pop("description", None)
        a.pop("photos", None)
        return a

    export = {
        "genere_le": datetime.now().isoformat(timespec="minutes"),
        "debut": f"{debut[8:10]}/{debut[5:7]}", "fin": f"{fin[8:10]}/{fin[5:7]}", "jours": jours,
        "budget": profil["recherche"]["budget_max_cc"],
        "lieu": profil["recherche"]["bureaux"]["adresse"],
        "nb_criteres": len(profil.get("criteres", [])),
        "seuil": seuil,
        "statuts": statuts,
        "raisons_ecart": dict(raisons.most_common(12)),
        "cout_total_usd": round(sum(r[7] or 0 for r in jugees), 2),
        "annonces": [
            {"annonce": allegee(a), "trajet": json.loads(t), "environs": json.loads(e),
             "score": s, "verdict": json.loads(v), "lignes": json.loads(l), "publiee": p}
            for a, t, e, s, v, l, p, _ in jugees
        ],
    }
    EXPORT.write_text(json.dumps(export, ensure_ascii=False, indent=1))
    # Page autonome : les données sont injectées dans le modèle HTML.
    donnees = json.dumps(export, ensure_ascii=False).replace("</", "<\\/")
    PAGE.write_text(MODELE.read_text().replace("__DATA__", donnees))
    au_dessus = sum(1 for r in jugees if r[3] >= seuil)
    log.info("%d jugées, %d au-dessus du seuil %d → %s", len(jugees), au_dessus, seuil, PAGE)


def main() -> None:
    parser = argparse.ArgumentParser(description="Rejeu du dernier mois d'annonces")
    parser.add_argument("etape", choices=["collecter", "preparer", "refiltrer", "juger", "messages", "rescorer", "rapport"])
    parser.add_argument("--jours", type=int, default=30)
    parser.add_argument("--max", type=int, help="nombre maximal d'annonces à juger")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--score-min", type=int, default=60, help="étape messages : à partir de ce score")
    parser.add_argument("--contenant", help="étape messages : seulement ceux qui contiennent ce texte")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    for bruyant in ("httpx", "httpx2", "urllib3"):
        logging.getLogger(bruyant).setLevel(logging.WARNING)

    profil = charger_profil()
    if args.etape == "collecter":
        collecter(profil, args.jours)
    elif args.etape == "preparer":
        preparer(profil)
    elif args.etape == "refiltrer":
        refiltrer(profil)
    elif args.etape == "juger":
        juger(profil, args.max, args.workers)
    elif args.etape == "rescorer":
        rescorer(profil, args.score_min)
    elif args.etape == "messages":
        messages(profil, args.score_min, args.max, args.workers, args.contenant)
    else:
        rapport(profil)


if __name__ == "__main__":
    main()
