# nest : consignes pour Claude Code

nest est une veille logement pour étudiants : `python -m nest` lit les annonces récentes,
écarte gratuitement ce qui ne colle pas, calcule le vrai trajet jusqu'au lieu de stage,
fait juger le reste par Claude d'après `profil.yaml`, et envoie une notif ntfy avec un
brouillon de message. L'utilisateur choisit et envoie lui-même : nest n'écrit jamais à
personne à sa place.

## Premier contact

- Pas de `profil.yaml` ou pas de `.env` : proposer de lancer l'installation guidée,
  `/entretien-recherche` (skill dans `.claude/skills/entretien-recherche`).
- Windows n'est pas pris en charge (macOS et Linux seulement).

## Pipeline

`nest/sources/` (Leboncoin via `lbc`, Gens de Confiance via `curl_cffi`) → `filtres.py`
(gratuit) → `trajet.py` (Google Routes, cache SQLite) → `doublons.py` → `environs.py`
(OpenStreetMap, seulement si un critère s'en sert) → `jugement.py` (Claude, sortie
structurée, critère par critère) → `scoring.py` (score sur 100) → `notif.py` (ntfy).
Facultatif : `mails/` (tri des mails d'agence, Gmail en lecture seule), `notion.py`
(miroir du suivi), page pont `ecrire/index.html` (à héberger soi-même).

## Règles

- Le profil est la seule source des critères et du contenu des messages : rien de
  personnel en dur dans le code.
- Secrets dans `.env` (chmod 600, ignoré par git) : ne jamais l'afficher, ne jamais
  réécrire une clé dans la conversation.
- Fréquence basse : 10 min par défaut, jamais moins de 5. Les sites ne sont pas
  officiellement ouverts à ce type d'accès. `python -m nest.rejeu collecter` lit 40 pages
  d'un coup : ne le lancer que si l'utilisateur le demande, en le prévenant.
- Pour tester, toujours à blanc d'abord : `.venv/bin/python -m nest --sec --max 3`.
- Commentaires et docstrings en français, PEP8, jamais de tiret cadratin.

## Commandes utiles

```bash
.venv/bin/python -m nest --verifier          # profil et clés, sans rien envoyer
.venv/bin/python -m nest --sec --max 3       # passage à blanc
.venv/bin/python -m nest --test-notif        # notif de test
.venv/bin/python -m nest --signaler <lien>   # bloque le compte d'une annonce (arnaque)
.venv/bin/python -m nest.planifier --retirer # arrête la tâche planifiée
tail -f data/nest.log                        # journal des passages planifiés
```
