#!/bin/bash
# Creates native/.venv and installs deps. Run once (or after requirements change).
set -e
cd "$(dirname "$0")"
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
echo "venv ready: $(pwd)/.venv/bin/python"
