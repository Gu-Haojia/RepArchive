#!/bin/sh
set -eu
cd -- "$(dirname -- "$0")"
if [ -x .venv/bin/python ]; then
  exec .venv/bin/python src/bootstrap.py "$@"
fi
exec python3 src/bootstrap.py "$@"
