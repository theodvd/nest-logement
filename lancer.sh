#!/bin/bash
# Lance un passage de veille nest (appelé par launchd ou cron, voir nest/planifier.py).
# Les clés sont lues dans .env par nest lui-même.
cd "$(dirname "$0")" || exit 1
mkdir -p data
export TZ=Europe/Paris
# Verrou : si le passage précédent tourne encore, on n'en lance pas un second.
if command -v flock >/dev/null 2>&1; then
  exec flock -n data/nest.lock ./.venv/bin/python -m nest >> data/nest.log 2>&1
else
  exec /usr/bin/lockf -t 0 data/nest.lock ./.venv/bin/python -m nest >> data/nest.log 2>&1
fi
