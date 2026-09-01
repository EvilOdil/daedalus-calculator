#!/usr/bin/env bash
# Launch the calculator. First run creates the virtualenv.
set -euo pipefail
cd "$(dirname "$0")"
if [ ! -d .venv ]; then
  echo "Creating .venv ..."
  python3 -m venv .venv
  ./.venv/bin/pip install -q --upgrade pip
  ./.venv/bin/pip install -q -r requirements.txt
fi
exec ./.venv/bin/streamlit run app/Home.py "$@"
