#!/bin/zsh
cd "${0:A:h}"
if [[ -x .venv/bin/python ]]; then
  exec .venv/bin/python src/bootstrap.py "$@"
fi
exec python3 src/bootstrap.py "$@"
