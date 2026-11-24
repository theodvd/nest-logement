"""Appels à Claude, par l'API ou par l'abonnement Claude.

Deux modes, choisis par la variable NEST_CLAUDE du .env :

- `api` (par défaut) : SDK Anthropic et clé ANTHROPIC_API_KEY, facturée à l'usage.
- `abonnement` : Claude Code en mode script (`claude -p`), avec l'abonnement Claude
  (Pro, Max) de l'utilisateur. Rien à payer en plus, mais chaque jugement entame le
  quota de l'abonnement, partagé avec l'usage normal de Claude. Pour une tâche
  planifiée, mettre dans .env le jeton longue durée donné par `claude setup-token`
  (CLAUDE_CODE_OAUTH_TOKEN) : la connexion normale expire et demande un navigateur.

Dans les deux modes, la réponse respecte le schéma Pydantic demandé.

Exemple d'appel :
    >>> objet, usage = parse(modele="claude-sonnet-5", systeme="Tu juges…",
    ...                      contenu="Annonce : …", schema=Verdict, effort="medium")
"""

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from types import SimpleNamespace

from pydantic import BaseModel

DELAI_S = 300                 # un jugement prend 5 à 30 s ; au-delà, quelque chose bloque
EMPLACEMENTS = ("~/.local/bin/claude", "~/.claude/local/claude",
                "/opt/homebrew/bin/claude", "/usr/local/bin/claude")


class Essai(BaseModel):
    """Schéma minimal des tests de connexion (--verifier, python -m nest.claude)."""

    reponse: str


class ErreurClaude(RuntimeError):
    """Claude Code a échoué ou n'a pas rendu de réponse structurée."""


def mode() -> str:
    """« api » ou « abonnement », d'après NEST_CLAUDE."""
    valeur = os.getenv("NEST_CLAUDE", "api").strip().lower()
    return "abonnement" if valeur == "abonnement" else "api"


def binaire() -> str | None:
    """Chemin de Claude Code. launchd et cron ont un PATH minimal : on cherche aussi
    aux emplacements d'installation habituels. NEST_CLAUDE_BIN force un chemin."""
    if force := os.getenv("NEST_CLAUDE_BIN"):
        return force
    if trouve := shutil.which("claude"):
        return trouve
    for chemin in EMPLACEMENTS:
        if Path(chemin).expanduser().is_file():
            return str(Path(chemin).expanduser())
    return None


def client():
    """Client API à partager entre plusieurs appels ; None en mode abonnement."""
    if mode() == "abonnement":
        return None
    import anthropic

    return anthropic.Anthropic()


def parse(*, modele: str, systeme: str, contenu: str, schema: type[BaseModel],
          effort: str = "medium", max_tokens: int = 4000, client=None):
    """Envoie une requête à Claude et renvoie (objet validé par `schema`, usage).

    `usage` porte input_tokens, output_tokens, cache_read_input_tokens et
    cache_creation_input_tokens, dans les deux modes (voir jugement.cout_usd). En mode
    abonnement, le coût ainsi calculé est un équivalent API : rien n'est facturé en plus.
    """
    if mode() == "abonnement":
        return _parse_abonnement(modele, systeme, contenu, schema, effort)
    import anthropic

    client = client or anthropic.Anthropic()
    reponse = client.messages.parse(
        model=modele,
        max_tokens=max_tokens,
        system=[{"type": "text", "text": systeme, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": contenu}],
        output_config={"effort": effort},
        output_format=schema,
    )
    return reponse.parsed_output, reponse.usage


def _parse_abonnement(modele: str, systeme: str, contenu: str, schema: type[BaseModel], effort: str):
    """Même requête via `claude -p`, isolée de la configuration de l'utilisateur."""
    chemin = binaire()
    if not chemin:
        raise ErreurClaude("Claude Code introuvable : installe-le, ou mets son chemin dans NEST_CLAUDE_BIN.")
    # Une clé API présente passerait avant l'abonnement : on la retire pour ce processus.
    env = {k: v for k, v in os.environ.items() if k not in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")}
    commande = [
        chemin, "-p",
        "--output-format", "json",
        "--model", modele,
        "--effort", effort,
        "--system-prompt", systeme,          # remplace les consignes de Claude Code
        "--json-schema", json.dumps(schema.model_json_schema(), ensure_ascii=False),
        "--tools", "",                       # aucun outil : il lit et répond, c'est tout
        "--strict-mcp-config",               # aucun serveur MCP de l'utilisateur
        "--setting-sources", "",             # ni réglages, ni CLAUDE.md, ni mémoire de l'utilisateur
        "--no-session-persistence",          # pas d'historique de session sur le disque
    ]
    try:
        # Dossier temporaire neutre : rien du dépôt n'est chargé dans le contexte.
        sortie = subprocess.run(commande, input=contenu, capture_output=True, text=True,
                                env=env, timeout=DELAI_S, cwd=tempfile.gettempdir())
    except subprocess.TimeoutExpired as err:
        raise ErreurClaude(f"Claude Code n'a pas répondu en {DELAI_S} s") from err
    try:
        donnees = json.loads(sortie.stdout)
    except json.JSONDecodeError as err:
        raise ErreurClaude(f"Réponse illisible de Claude Code (code {sortie.returncode}) : "
                           f"{(sortie.stderr or sortie.stdout)[:300]}") from err
    if donnees.get("is_error") or donnees.get("structured_output") is None:
        # Quota de l'abonnement atteint, connexion expirée, refus… : le texte dit pourquoi.
        raise ErreurClaude(f"Claude Code : {donnees.get('subtype')} : {str(donnees.get('result'))[:300]}")
    u = donnees.get("usage") or {}
    usage = SimpleNamespace(
        input_tokens=u.get("input_tokens", 0),
        output_tokens=u.get("output_tokens", 0),
        cache_read_input_tokens=u.get("cache_read_input_tokens", 0),
        cache_creation_input_tokens=u.get("cache_creation_input_tokens", 0),
    )
    return schema.model_validate(donnees["structured_output"]), usage


if __name__ == "__main__":
    # Test rapide du mode courant : python -m nest.claude
    from nest.__main__ import charger_env

    charger_env()
    objet, usage = parse(modele="claude-sonnet-5", systeme="Réponds au format demandé.",
                         contenu="Dis bonjour en un mot.", schema=Essai, effort="low")
    print(f"Mode {mode()} : {objet.reponse} ({usage.input_tokens} + {usage.output_tokens} tokens)")
