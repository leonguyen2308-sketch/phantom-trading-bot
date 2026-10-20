#!/usr/bin/env bash
# The Memecoin Desk on your Mac. Usage:
#   ./desk.sh demo        simulation with the dashboard, no keys needed
#   ./desk.sh scan        one look at the real market (no trading, no AI)
#   ./desk.sh paper       the real market with fake money
#   ./desk.sh live        real money, after ./desk.sh golive-check passes
#   ./desk.sh --help      every command
set -euo pipefail
cd "$(dirname "$0")"

if ! command -v uv >/dev/null 2>&1; then
  echo "This needs uv (it installs Python and the bot's packages)."
  echo "Install it with:  curl -LsSf https://astral.sh/uv/install.sh | sh"
  echo "then close Terminal completely (Cmd+Q), open it again and run ./desk.sh again."
  exit 1
fi

if [ ! -f .env ]; then
  cp .env.example .env
  echo "Created .env. The demo needs nothing else: ./desk.sh demo"
fi
chmod 600 .env

if [ $# -eq 0 ]; then set -- demo; fi
exec uv run python -m desk "$@"
