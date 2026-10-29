#!/bin/bash
set -e
cd "$(dirname "$0")"
if ! command -v python3 >/dev/null 2>&1; then
  echo "Python 3 is required. Install Python 3.10+ and try again."
  read -r -p "Press Enter to close..." _
  exit 1
fi
if [ ! -d .venv ]; then python3 -m venv .venv; fi
source .venv/bin/activate
python -m pip install -r requirements.txt
if [ -f .env ]; then set -a; source .env; set +a; fi
python app.py
