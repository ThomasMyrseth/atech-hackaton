#!/usr/bin/env bash
# Runs the assistant with the demo alarm 10 s after start. Arguments pass through, e.g. ./run.sh --no-board
set -euo pipefail
cd "$(dirname "$0")"
exec ../.venv/bin/python -m backend.main --demo-alarm "$@"
