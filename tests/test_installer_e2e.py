"""The Ubuntu installer, executed end to end with stubbed system tools.

There is no systemd on the development box, so the installer's real path -- venv, unit
writing, the systemctl sequence, the database wait -- had only ever been read, never run.
This executes the actual script with `systemctl`, `loginctl`, `pip` and `python` replaced by
stubs that record what they were asked to do. Everything except systemd itself is exercised.

Paths are compared after normalisation: bash reports `/c/repo` where Python says `C:/repo`,
so both sides are folded to lowercase forward-slash form without the drive letter.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "ops" / "install-ubuntu.sh"


def _bash() -> str | None:
    found = shutil.which("bash")
    if found:
        return found
    for candidate in (r"C:\Program Files\Git\bin\bash.exe", "/bin/bash"):
        if Path(candidate).exists():
            return candidate
    return None


def _norm(text: str) -> str:
    """Fold a Windows or MSYS path into one comparable form."""
    folded = text.strip().lower().replace("\\", "/")
    if len(folded) > 1 and folded[1] == ":":
        folded = "/" + folded[0] + folded[2:]
    return folded.rstrip("/")


def _stub(path: Path, log: Path, body: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f'#!/bin/sh\necho "$(basename "$0") $*" >> "{log.as_posix()}"\n' + body + "exit 0\n",
        encoding="utf-8",
    )
    path.chmod(0o755)


@pytest.fixture()
def installed():
    bash = _bash()
    if bash is None:
        pytest.skip("no bash available")

    # The installer refuses a repository path containing a space, and pytest's temp root
    # sits under a user profile that has one. Use a space-free sandbox in the repo instead.
    sandbox = ROOT / ".sandbox"
    sandbox.mkdir(exist_ok=True)
    base = Path(tempfile.mkdtemp(prefix="installer-", dir=sandbox))
    try:
        repo = base / "repo"
        (repo / "data").mkdir(parents=True)
        shutil.copytree(ROOT / "ops", repo / "ops")

        log = base / "calls.log"
        bin_dir = base / "bin"
        bin_dir.mkdir()
        _stub(bin_dir / "systemctl", log)
        _stub(bin_dir / "loginctl", log)
        _stub(
            bin_dir / "python3",
            log,
            'case "$*" in *version_info*) echo 312 ;; *) echo "Python 3.12.0" ;; esac\n',
        )
        for name in ("pip", "python"):
            _stub(repo / ".venv" / "bin" / name, log)

        env = {
            **os.environ,
            "PATH": f"{bin_dir.as_posix()}:{os.environ.get('PATH', '')}",
            "HOME": str(base),
            "XDG_CONFIG_HOME": str(base / "config"),
            "PYTHON": "python3",
        }
        completed = subprocess.run(
            [bash, str(repo / "ops" / "install-ubuntu.sh")],
            capture_output=True,
            text=True,
            env=env,
            cwd=str(repo),
        )
        yield completed, repo, log, base
    finally:
        shutil.rmtree(base, ignore_errors=True)
        shutil.rmtree(sandbox, ignore_errors=True)


def test_the_installer_runs_to_completion(installed) -> None:
    completed, _, _, _ = installed
    assert completed.returncode == 0, (
        f"exit={completed.returncode}\nstdout={completed.stdout}\nstderr={completed.stderr}"
    )
    assert "installed units into" in completed.stdout


def test_units_are_written_with_the_repo_path_substituted(installed) -> None:
    _, repo, _, base = installed
    unit_dir = base / "config" / "systemd" / "user"
    names = sorted(path.name for path in unit_dir.iterdir())
    assert names == [
        "skysherpa-check.service",
        "skysherpa-check.timer",
        "skysherpa-ingest.service",
        "skysherpa-publish.service",
        "skysherpa-web.service",
    ]
    for unit in unit_dir.iterdir():
        text = _norm(unit.read_text(encoding="utf-8"))
        assert "@repo@" not in text
        if unit.suffix == ".service":
            assert f"{_norm(repo.as_posix())}/.venv/bin/python" in text, unit.name


def test_the_systemctl_sequence_is_ordered_collector_first(installed) -> None:
    _, _, log, _ = installed
    calls = [line.lower() for line in log.read_text(encoding="utf-8").splitlines()]
    systemctl = [call for call in calls if call.startswith("systemctl ")]

    # The first systemctl call is the reachability check; the reload must precede any start.
    reload_at = next(i for i, call in enumerate(systemctl) if call.endswith("daemon-reload"))
    order = ["ingest.service", "publish.service", "web.service", "check.timer"]
    positions = [next(i for i, call in enumerate(systemctl) if name in call) for name in order]
    assert reload_at < positions[0], systemctl
    assert positions == sorted(positions), systemctl
    assert any("enable-linger" in call for call in calls)


def test_the_package_is_installed_and_smoke_tested(installed) -> None:
    _, repo, log, _ = installed
    calls = _norm(log.read_text(encoding="utf-8"))
    assert "pip install --quiet --upgrade pip" in calls
    assert f"pip install --quiet -e {_norm(repo.as_posix())}" in calls
    assert "python -m skysherpa version" in calls
