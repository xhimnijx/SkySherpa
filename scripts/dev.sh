#!/usr/bin/env sh
# Run skysherpa from the source tree without installing it.
set -eu
here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PYTHONPATH="$here/../src${PYTHONPATH:+:$PYTHONPATH}" exec python -m skysherpa "$@"
