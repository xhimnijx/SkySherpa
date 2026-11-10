#!/usr/bin/env bash
# Install SkySherpa as systemd *user* units on Ubuntu.
#
# User units rather than system units: this is a single-person laptop service, it needs no
# root, and `loginctl enable-linger` keeps it running after logout. If you would rather run
# it as a system service, copy the same units to /etc/systemd/system, add User=, and drop
# the --user flag below.
set -euo pipefail

repo=$(cd "$(dirname "$0")/.." && pwd)
python=${PYTHON:-python3}

say() { printf '%s\n' "$*"; }

command -v "$python" >/dev/null || { say "error: $python not found"; exit 1; }
version=$("$python" -c 'import sys; print("%d%02d" % sys.version_info[:2])')
if [ "$version" -lt 311 ]; then
  say "error: Python 3.11+ required, found $("$python" -V 2>&1)"
  say "       Ubuntu 24.04 ships 3.12; on 22.04 install python3.11 or use a newer release."
  exit 1
fi

if [ ! -d "$repo/.venv" ]; then
  say "creating $repo/.venv"
  "$python" -m venv "$repo/.venv"
fi
"$repo/.venv/bin/pip" install --quiet --upgrade pip
say "installing skysherpa into the venv"
"$repo/.venv/bin/pip" install --quiet -e "$repo"

unit_dir="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
mkdir -p "$unit_dir"
for unit in "$repo"/ops/systemd/*.service "$repo"/ops/systemd/*.timer; do
  sed "s|@REPO@|$repo|g" "$unit" >"$unit_dir/$(basename "$unit")"
done
say "installed units into $unit_dir"

systemctl --user daemon-reload
# The publisher needs a database to publish; it retries until the collector has made one.
systemctl --user enable --now skysherpa-ingest.service
systemctl --user enable --now skysherpa-publish.service
systemctl --user enable --now skysherpa-web.service
systemctl --user enable --now skysherpa-check.timer

# Without lingering, user units are stopped when the last session closes.
if command -v loginctl >/dev/null; then
  loginctl enable-linger "$USER" || say "warning: could not enable lingering; the service will stop at logout"
fi

say ""
say "installed. Useful commands:"
say "  systemctl --user status skysherpa-ingest skysherpa-publish"
say "  journalctl --user -u skysherpa-publish -f"
say "  systemctl --user list-timers skysherpa-check.timer"
say "  $repo/.venv/bin/python -m skysherpa status --check"
say ""
say "site: http://127.0.0.1:8099/ (it appears once the first publish cycle completes)"
