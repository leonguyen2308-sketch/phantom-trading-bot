#!/usr/bin/env bash
# The FOMO copy bot on your Mac. Usage:
#   ./fomo.sh demo        simulation with the dashboard, no keys needed
#   ./fomo.sh paper       copy the real weekly top with fake money
#   ./fomo.sh live        real money, after ./fomo.sh golive-check passes
#   ./fomo.sh --help      every command
set -euo pipefail
cd "$(dirname "$0")"

if ! command -v uv >/dev/null 2>&1; then
  echo "This needs uv (it installs Python and the bot's packages)."
  echo "Install it with:  curl -LsSf https://astral.sh/uv/install.sh | sh"
  echo "then open a new Terminal window and run ./fomo.sh again."
  exit 1
fi

if [ ! -f .env ]; then
  cp .env.example .env
  chmod 600 .env
  echo "Created .env. For the demo you need nothing else: ./fomo.sh demo"
  echo "For paper or live, open it (open -e .env) and add your keys (see FOMO_BOT.md)."
fi
chmod 600 .env

if [ $# -eq 0 ]; then set -- demo; fi
exec uv run python -m fomo "$@"
