#!/usr/bin/env bash
# Repo Archaeologist launcher: sets up, indexes a repo, and answers "why" questions about it.
#
#   ./start.sh                              # prompts for a repo, then for questions
#   ./start.sh <repo>                       # index <repo>, then ask questions interactively
#   ./start.sh <repo> "why ...?" [flags]    # answer one question (flags go to `arch ask`)
#
# <repo> is a local path, a GitHub "owner/name", or a GitHub URL. GitHub repos are cloned
# (full history) into $ARCH_HOME/clones. Useful `arch ask` flags: --offline --no-hops
# --no-verify --json. Keys live in .env (copied from .env.example on first run).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CACHE="${ARCH_HOME:-$HOME/.cache/repo-archaeologist}"

bold() { printf '\033[1m%s\033[0m\n' "$*"; }
info() { printf '\033[2m%s\033[0m\n' "$*"; }
die() { printf '\033[31merror:\033[0m %s\n' "$*" >&2; exit 1; }
arch() { uv run --quiet --project "$ROOT" arch "$@"; }

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'
  exit 0
fi

# -- prerequisites ----------------------------------------------------------------------
command -v git >/dev/null || die "git is not installed."
command -v uv >/dev/null || die "uv is not installed. Install it with:
  curl -LsSf https://astral.sh/uv/install.sh | sh"

info "Checking dependencies..."
uv sync --quiet --project "$ROOT" --extra embed

if [[ ! -f "$ROOT/.env" ]]; then
  cp "$ROOT/.env.example" "$ROOT/.env"
  chmod 600 "$ROOT/.env"
  bold "Created $ROOT/.env"
  echo "Add an API key to it, then run ./start.sh again:"
  echo "  GEMINI_API_KEY=...     free tier: https://aistudio.google.com/apikey"
  echo "  ANTHROPIC_API_KEY=...  paid:      https://console.anthropic.com"
  exit 1
fi

# Which backend will run, without ever printing a key.
provider="$(uv run --quiet --project "$ROOT" python -c '
import os
from archaeologist.config import gemini_api_key, llm_provider, load_env_file
load_env_file()
p = llm_provider()
ok = bool(os.getenv("ANTHROPIC_API_KEY")) if p == "anthropic" else bool(gemini_api_key())
print(p if ok else "")
')" || die "could not read settings (check ARCH_PROVIDER in .env)"
[[ -n "$provider" ]] || die "no LLM API key found. Add one to $ROOT/.env:
  GEMINI_API_KEY=...     free tier: https://aistudio.google.com/apikey
  ANTHROPIC_API_KEY=...  paid:      https://console.anthropic.com"
info "LLM backend: $provider"

# -- repo -------------------------------------------------------------------------------
repo="${1:-}"
if [[ -z "$repo" ]]; then
  read -r -e -p "Repo (local path, owner/name or GitHub URL) [.]: " repo || exit 0
  repo="${repo:-.}"
fi

slug=""
if [[ "$repo" =~ ^(https?://)?(www\.)?github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+) ]]; then
  slug="${BASH_REMATCH[3]}/${BASH_REMATCH[4]%.git}"
elif [[ "$repo" =~ ^git@github\.com:([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)$ ]]; then
  slug="${BASH_REMATCH[1]}/${BASH_REMATCH[2]%.git}"
elif [[ ! -e "$repo" && "$repo" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]]; then
  slug="$repo"
fi

if [[ -n "$slug" ]]; then
  dest="$CACHE/clones/${slug/\//__}"
  if [[ -d "$dest/.git" ]]; then
    info "Updating $slug..."
    git -C "$dest" pull --quiet --ff-only || info "(could not update; using the existing clone)"
  else
    bold "Cloning $slug (full history, needed for blame and log)..."
    mkdir -p "$(dirname "$dest")"
    git clone --quiet "https://github.com/$slug.git" "$dest" || die "could not clone $slug"
  fi
  repo="$dest"
fi
[[ -d "$repo" ]] || die "no such directory: $repo"
repo="$(cd "$repo" && pwd)"
git -C "$repo" rev-parse --is-inside-work-tree >/dev/null 2>&1 || die "not a git repository: $repo"

bold "Indexing $repo (first run downloads a small embedding model)..."
arch index "$repo"

# -- ask --------------------------------------------------------------------------------
if [[ $# -ge 2 ]]; then
  arch ask "$repo" "$2" --steps "${@:3}"
  exit $?
fi

bold "Ask why the code is the way it is. Empty line or Ctrl-D quits."
[[ "$provider" == "gemini" ]] && info "(Gemini free tier: expect a few minutes per question)"
while read -r -e -p $'\n? ' question; do
  [[ -z "$question" ]] && break
  arch ask "$repo" "$question" --steps || info "(that question failed; try another)"
done
