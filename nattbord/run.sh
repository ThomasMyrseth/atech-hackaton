#!/usr/bin/env bash
# Runs the assistant. Arguments pass through, e.g. ./run.sh --no-board
set -euo pipefail
cd "$(dirname "$0")"
exec ../.venv/bin/python -m backend.main "$@"
