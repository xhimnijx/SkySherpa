#!/usr/bin/env sh
# Run skysherpa from the source tree without installing it.
set -eu
here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
root=$(dirname "$here")

# Ubuntu has `python3` and usually no `python`. Prefer the project venv, which the
# installer creates, then python3, and only then whatever `python` resolves to.
if [ -x "$root/.venv/bin/python" ]; then
  py="$root/.venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
  py="python3"
else
  py="python"
fi

PYTHONPATH="$root/src${PYTHONPATH:+:$PYTHONPATH}" exec "$py" -m skysherpa "$@"
