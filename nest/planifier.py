"""Installe (ou retire) la tâche planifiée qui lance un passage de veille.

- macOS : agent launchd dans ~/Library/LaunchAgents (actif tant que la session est ouverte ;
  le Mac en veille laisse des trous, rattrapés au réveil par `__main__`).
- Linux : ligne crontab repérée par le marqueur « # nest-veille ».
- Windows : non pris en charge.

Exemples d'appel :
    python -m nest.planifier --afficher        # montre ce qui serait installé, sans rien faire
    python -m nest.planifier                   # installe, un passage toutes les 10 min
    python -m nest.planifier --intervalle 15   # un passage toutes les 15 min
    python -m nest.planifier --retirer         # désinstalle
"""

import argparse
import os
import platform
import subprocess
import sys
from pathlib import Path

RACINE = Path(__file__).resolve().parent.parent
LANCEUR = RACINE / "lancer.sh"
LABEL = "com.nest.veille"
PLIST = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
MARQUEUR = "# nest-veille"
INTERVALLE_MIN = 5     # en dessous, le risque de blocage par les sites grimpe


def plist(intervalle_min: int) -> str:
    """Contenu de l'agent launchd."""
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{LABEL}</string>
  <key>ProgramArguments</key><array><string>{LANCEUR}</string></array>
  <key>StartInterval</key><integer>{intervalle_min * 60}</integer>
  <key>RunAtLoad</key><false/>
  <key>StandardErrorPath</key><string>{RACINE / "data" / "launchd.err"}</string>
</dict>
</plist>
"""


def ligne_cron(intervalle_min: int) -> str:
    """Ligne crontab (les minutes doivent diviser 60 pour un rythme régulier)."""
    return f"*/{intervalle_min} * * * * {LANCEUR} {MARQUEUR}"


def _crontab_actuelle() -> list[str]:
    rep = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    return rep.stdout.splitlines() if rep.returncode == 0 else []


def _ecrire_crontab(lignes: list[str]) -> None:
    subprocess.run(["crontab", "-"], input="\n".join(lignes) + "\n", text=True, check=True)


def installer(intervalle_min: int) -> str:
    """Installe la tâche et renvoie une phrase de compte rendu."""
    systeme = platform.system()
    (RACINE / "data").mkdir(exist_ok=True)
    if not (RACINE / ".venv" / "bin" / "python").exists():
        raise SystemExit("Environnement .venv absent : python3 -m venv .venv && .venv/bin/pip install -r requirements.txt")
    if systeme == "Darwin":
        PLIST.parent.mkdir(parents=True, exist_ok=True)
        domaine = f"gui/{os.getuid()}"
        subprocess.run(["launchctl", "bootout", f"{domaine}/{LABEL}"], capture_output=True)
        PLIST.write_text(plist(intervalle_min))
        subprocess.run(["launchctl", "bootstrap", domaine, str(PLIST)], check=True)
        return f"launchd : {LABEL} installé, un passage toutes les {intervalle_min} min ({PLIST})"
    if systeme == "Linux":
        lignes = [l for l in _crontab_actuelle() if MARQUEUR not in l] + [ligne_cron(intervalle_min)]
        _ecrire_crontab(lignes)
        return f"cron : un passage toutes les {intervalle_min} min (crontab -l pour voir)"
    raise SystemExit(f"{systeme} n'est pas pris en charge : nest tourne sur macOS ou Linux.")


def retirer() -> str:
    """Désinstalle la tâche, quel que soit le système."""
    if platform.system() == "Darwin":
        subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}"], capture_output=True)
        PLIST.unlink(missing_ok=True)
        return f"launchd : {LABEL} retiré"
    if platform.system() == "Linux":
        _ecrire_crontab([l for l in _crontab_actuelle() if MARQUEUR not in l])
        return "cron : ligne nest retirée"
    raise SystemExit("Système non pris en charge.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Tâche planifiée de nest")
    parser.add_argument("--intervalle", type=int, default=10, help="minutes entre deux passages (10 par défaut)")
    parser.add_argument("--afficher", action="store_true", help="montre ce qui serait installé")
    parser.add_argument("--retirer", action="store_true", help="désinstalle la tâche")
    args = parser.parse_args()
    if args.intervalle < INTERVALLE_MIN or 60 % args.intervalle:
        sys.exit(f"Intervalle refusé : au moins {INTERVALLE_MIN} min, et un diviseur de 60 (5, 10, 15, 20, 30, 60).")
    if args.retirer:
        print(retirer())
    elif args.afficher:
        systeme = platform.system()
        if systeme == "Darwin":
            print(f"# {PLIST}\n{plist(args.intervalle)}")
        elif systeme == "Linux":
            print(ligne_cron(args.intervalle))
        else:
            print(f"{systeme} n'est pas pris en charge : nest tourne sur macOS ou Linux.")
    else:
        print(installer(args.intervalle))


if __name__ == "__main__":
    main()
