#!/usr/bin/env bash
# The Grok bot on your Mac: copies the weekly top #1 and #2 overnight, Grok picks and checks. Usage:
#   ./grok.sh demo        a made-up night with the dashboard, no keys needed
#   ./grok.sh paper       real traders, fake money
#   ./grok.sh live        real money, after ./grok.sh golive-check passes
#   ./grok.sh --help      every command
set -euo pipefail
cd "$(dirname "$0")"

if ! command -v uv >/dev/null 2>&1; then
  echo "This needs uv (it installs Python and the bot's packages)."
  echo "Install it with:  curl -LsSf https://astral.sh/uv/install.sh | sh"
  echo "then open a new Terminal window and run ./grok.sh again."
  exit 1
fi

if [ ! -f .env ]; then
  cp .env.example .env
  chmod 600 .env
  echo "Created .env. For the demo you need nothing else: ./grok.sh demo"
  echo "For paper or live, open it (open -e .env) and add your keys (see HUONG_DAN_GROK.md)."
fi
chmod 600 .env

if [ $# -eq 0 ]; then set -- demo; fi
exec uv run python -m fomo --profile grok "$@"
