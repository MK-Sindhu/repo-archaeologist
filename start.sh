#!/usr/bin/env bash
# Repo Archaeologist launcher.
#
#   ./start.sh                         # start the website and open it in your browser
#   ./start.sh --port 9000 --no-open   # website on another port, without opening a browser
#   ./start.sh ask <repo> ["why ...?"] [flags]   # terminal mode (one question, or interactive)
#
# <repo> (terminal mode) is a local path, a GitHub "owner/name", or a GitHub URL; GitHub
# repos are cloned with full history into $ARCH_HOME/clones. Keys live in .env, created
# from .env.example on first run.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CACHE="${ARCH_HOME:-$HOME/.cache/repo-archaeologist}"
STATIC="$ROOT/src/archaeologist/web/static"

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

info "Checking Python dependencies..."
uv sync --quiet --project "$ROOT" --extra embed --extra outline

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
from archaeologist.backends import llm_ready
from archaeologist.config import llm_provider, load_env_file
load_env_file()
print(llm_provider() if llm_ready()[0] else "")
')" || die "could not read settings (check ARCH_PROVIDER in .env)"

need_key() {
  [[ -n "$provider" ]] || die "no LLM API key found. Add one to $ROOT/.env:
  GEMINI_API_KEY=...     free tier: https://aistudio.google.com/apikey
  ANTHROPIC_API_KEY=...  paid:      https://console.anthropic.com"
}

# -- website (default) ------------------------------------------------------------------
build_frontend() {
  if ! command -v npm >/dev/null; then
    [[ -f "$STATIC/index.html" ]] && return 0
    info "npm is not installed, so the website can't be built (install Node.js from"
    info "https://nodejs.org). The API still runs; terminal mode: ./start.sh ask <repo>"
    return 0
  fi
  local fe="$ROOT/frontend"
  if [[ ! -d "$fe/node_modules" || "$fe/package-lock.json" -nt "$fe/node_modules/.package-lock.json" ]]; then
    info "Installing website dependencies..."
    (cd "$fe" && npm install --no-audit --no-fund --loglevel=error)
  fi
  if [[ ! -f "$STATIC/index.html" ]] || [[ -n "$(find "$fe/src" "$fe/index.html" "$fe/package.json" \
        "$fe/vite.config.ts" -newer "$STATIC/index.html" -print -quit 2>/dev/null)" ]]; then
    info "Building the website..."
    (cd "$fe" && npm run --silent build >/dev/null) || die "the website build failed; run: cd frontend && npm run build"
  fi
}

if [[ "${1:-}" != "ask" ]]; then
  if [[ -z "$provider" ]]; then
    info "No LLM API key yet: the website will open, and asking questions needs a key in .env."
  else
    info "LLM backend: $provider"
  fi
  build_frontend
  exec uv run --quiet --project "$ROOT" arch serve "$@"
fi

# -- terminal mode: ./start.sh ask <repo> ["question"] [flags] ---------------------------
shift
need_key
info "LLM backend: $provider"
repo="${1:-}"
if [[ -z "$repo" ]]; then
  read -r -e -p "Repo (local path, owner/name or GitHub URL) [.]: " repo || exit 0
  repo="${repo:-.}"
fi

slug=""
if [[ "$repo" =~ ^(https?://)?(www\.)?github\.com/([A-Za-z0-9][A-Za-z0-9-]*)/([A-Za-z0-9_.-]+) ]]; then
  slug="${BASH_REMATCH[3]}/${BASH_REMATCH[4]%.git}"
elif [[ "$repo" =~ ^git@github\.com:([A-Za-z0-9][A-Za-z0-9-]*)/([A-Za-z0-9_.-]+)$ ]]; then
  slug="${BASH_REMATCH[1]}/${BASH_REMATCH[2]%.git}"
elif [[ ! -e "$repo" && "$repo" =~ ^[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9_.-]+$ ]]; then
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
