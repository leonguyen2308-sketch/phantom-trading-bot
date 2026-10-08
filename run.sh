#!/usr/bin/env bash
# Start the bot on your Mac. Usage:
#   ./run.sh                 paper trading (default)
#   ./run.sh backtest --jev  any other command: see ./run.sh --help
set -euo pipefail
cd "$(dirname "$0")"

if ! command -v uv >/dev/null 2>&1; then
  echo "This needs uv (it installs Python and the bot's packages)."
  echo "Install it with:  curl -LsSf https://astral.sh/uv/install.sh | sh"
  echo "then open a new Terminal window and run ./run.sh again."
  exit 1
fi

if [ ! -f .env ]; then
  cp .env.example .env
  chmod 600 .env
  echo "Created .env. Open it (open -e .env), add your TypeSafe key, then run ./run.sh again."
  exit 1
fi
chmod 600 .env

if [ $# -eq 0 ]; then set -- paper; fi
exec uv run python -m phantom "$@"
