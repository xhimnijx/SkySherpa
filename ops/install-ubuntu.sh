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
dry_run=0
# $USER is not set in every environment that runs this (cron, bare sh); id always works.
login_user=$(id -un)

say() { printf '%s\n' "$*"; }

for arg in "$@"; do
  case "$arg" in
    --dry-run) dry_run=1 ;;
    -h | --help)
      say "usage: $0 [--dry-run]"
      say ""
      say "Installs SkySherpa as systemd user units. --dry-run checks the environment and"
      say "prints what it would do, without creating a venv or touching systemd."
      exit 0
      ;;
    *)
      say "error: unknown argument: $arg (try --help)"
      exit 1
      ;;
  esac
done

# ExecStart is not shell-quoted, so a space in the path produces a unit that cannot start.
case "$repo" in
  *" "*)
    say "error: the repository path contains a space: $repo"
    say "       move it somewhere without spaces (for example ~/skysherpa)"
    exit 1
    ;;
esac

# Without a user session bus, systemctl --user fails with a message about D-Bus that does
# not mention the real cause: running from a bare SSH login or a cron job.
if ! systemctl --user show-environment >/dev/null 2>&1; then
  if [ "$dry_run" = 1 ]; then
    say "warning: no user systemd manager reachable (expected outside a desktop session)"
  else
    say "error: cannot reach the user systemd manager"
    say "       run this from a graphical login, or as:"
    say "       XDG_RUNTIME_DIR=/run/user/\$(id -u) ./ops/install-ubuntu.sh"
    exit 1
  fi
fi

command -v "$python" >/dev/null || { say "error: $python not found"; exit 1; }
version=$("$python" -c 'import sys; print("%d%02d" % sys.version_info[:2])')
if [ "$version" -lt 311 ]; then
  say "error: Python 3.11+ required, found $("$python" -V 2>&1)"
  say "       Ubuntu 24.04 ships 3.12; on 22.04 install python3.11 or use a newer release."
  exit 1
fi

# A stock Ubuntu image often lacks venv/ensurepip, and the failure message ("ensurepip is
# not available") does not say what to install.
if ! "$python" -c 'import venv, ensurepip' >/dev/null 2>&1; then
  say "error: $python cannot create virtual environments"
  say "       install it first:  sudo apt install python3-venv"
  exit 1
fi

unit_dir="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"

if [ "$dry_run" = 1 ]; then
  say ""
  say "dry run: nothing created, nothing written, nothing started"
  say "  python    $("$python" -V 2>&1)"
  say "  repo      $repo"
  say "  units     $unit_dir"
  for unit in "$repo"/ops/systemd/*.service "$repo"/ops/systemd/*.timer; do
    say "  would install $(basename "$unit")"
  done
  say "  would create $repo/.venv and install the package into it"
  say "  would run: systemctl --user enable --now skysherpa-ingest.service"
  say "  would run: systemctl --user enable --now skysherpa-publish.service"
  say "  would run: systemctl --user enable --now skysherpa-web.service"
  say "  would run: systemctl --user enable --now skysherpa-check.timer"
  say "  would run: loginctl enable-linger $login_user"
  exit 0
fi

if [ ! -d "$repo/.venv" ]; then
  say "creating $repo/.venv"
  "$python" -m venv "$repo/.venv"
fi
"$repo/.venv/bin/pip" install --quiet --upgrade pip
say "installing skysherpa into the venv"
"$repo/.venv/bin/pip" install --quiet -e "$repo"

# Fail here rather than in a unit that restarts forever.
"$repo/.venv/bin/python" -m skysherpa version >/dev/null || {
  say "error: the installed package does not run; check the pip output above"
  exit 1
}

mkdir -p "$unit_dir"
for unit in "$repo"/ops/systemd/*.service "$repo"/ops/systemd/*.timer; do
  sed "s|@REPO@|$repo|g" "$unit" >"$unit_dir/$(basename "$unit")"
done
say "installed units into $unit_dir"

systemctl --user daemon-reload

# Start the collector first: it creates the database on its first poll, and everything
# downstream needs it. `set -e` plus a failing start job is how the first version of this
# script died -- the publisher has nothing to publish yet and exits immediately.
systemctl --user enable --now skysherpa-ingest.service

say "waiting for the first poll to create the database..."
for _ in $(seq 1 30); do
  [ -f "$repo/data/skysherpa.sqlite3" ] && break
  sleep 2
done

systemctl --user enable --now skysherpa-publish.service ||
  say "note: the publisher will retry until the collector has collected something"

# The web unit waits for dist-real itself, so this returns once the first bundle exists.
systemctl --user enable --now skysherpa-web.service ||
  say "note: the site will appear once the first publish cycle completes"

systemctl --user enable --now skysherpa-check.timer

# Without lingering, user units are stopped when the last session closes.
if command -v loginctl >/dev/null; then
  loginctl enable-linger "$login_user" || say "warning: could not enable lingering; the service will stop at logout"
fi

say ""
say "installed. Useful commands:"
say "  systemctl --user status skysherpa-ingest skysherpa-publish"
say "  journalctl --user -u skysherpa-publish -f"
say "  systemctl --user list-timers skysherpa-check.timer"
say "  $repo/.venv/bin/python -m skysherpa status --check"
say ""
say "site: http://127.0.0.1:8099/ (it appears once the first publish cycle completes)"
