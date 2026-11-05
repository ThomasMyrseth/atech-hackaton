#!/usr/bin/env bash
# Runs the sonar. Arguments pass through, e.g. ./run.sh --selftest
set -euo pipefail
cd "$(dirname "$0")"
exec ../venv/bin/python sonar.py "$@"
