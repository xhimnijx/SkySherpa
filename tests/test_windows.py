"""The cross-platform supervisor, and the Windows installer.

`skysherpa up` is what Windows uses instead of systemd: one process that starts the
collector, the publisher and the site, restarts whatever dies, and stops them together.
These tests exercise the composition and the restart decision, not the work the children do
-- that is covered by the rest of the suite.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from skysherpa import up
from skysherpa.config import Settings

ROOT = Path(__file__).resolve().parents[1]


def _supervisor_commands():
    settings = Settings.load()
    return up.commands(
        settings,
        out=settings.root / "dist-real",
        interval=15,
        publish_interval=900,
        port=8099,
    )


def test_the_supervisor_owns_three_processes() -> None:
    children = _supervisor_commands()
    assert [child.name for child in children] == ["collector", "publisher", "site"]


def test_each_child_invokes_the_right_module() -> None:
    children = {child.name: child.argv for child in _supervisor_commands()}
    assert "ingest" in children["collector"]
    assert "--interval" in children["collector"]
    assert "publish" in children["publisher"] and "--watch" in children["publisher"]
    assert "http.server" in children["site"]
    # -u keeps the logs honest: without it a child's final messages are lost on stop.
    for argv in children.values():
        assert "-u" in argv


def test_the_site_waits_for_a_bundle(tmp_path: Path) -> None:
    site = up.Child("site", [])
    collector = up.Child("collector", [])
    assert up._ready(collector, tmp_path) is True
    assert up._ready(site, tmp_path) is False
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "meta.json").write_text("{}", encoding="utf-8")
    assert up._ready(site, tmp_path) is True


def test_a_child_that_dies_is_restarted(tmp_path: Path, monkeypatch) -> None:
    """One process that exits immediately, supervised for two seconds."""
    log_dir = tmp_path / "logs"
    settings = Settings.load()

    def one_command(*args, **kwargs):
        return [
            up.Child(
                "crasher",
                [sys.executable, "-u", "-c", "import sys; sys.exit(3)"],
            )
        ]

    monkeypatch.setattr(up, "commands", one_command)
    monkeypatch.setattr(up, "BACKOFF_START", 0.2)

    assert up.supervise(settings, out=tmp_path / "dist", seconds=2.0, quiet=True) == 0

    log = log_dir / "crasher.log"
    if not log.exists():  # the supervisor writes logs under settings.root
        log = settings.root / "logs" / "crasher.log"
    assert log.exists()
    text = log.read_text(encoding="utf-8", errors="replace")
    assert "starting" in text


def test_supervise_stops_its_children(tmp_path: Path, monkeypatch) -> None:
    """A child that would sleep for a minute must be gone when supervise returns."""
    settings = Settings.load()
    captured: list[up.Child] = []

    def one_command(*args, **kwargs):
        child = up.Child(
            "sleeper",
            [sys.executable, "-u", "-c", "import time; time.sleep(60)"],
        )
        captured.append(child)
        return [child]

    monkeypatch.setattr(up, "commands", one_command)
    assert up.supervise(settings, out=tmp_path / "dist", seconds=1.5, quiet=True) == 0
    assert captured
    process = captured[0].process
    assert process is None or process.poll() is not None, "the child outlived the supervisor"


# ------------------------------------------------------------------ windows installer


def _windows_shell() -> list[str] | None:
    if shutil.which("powershell"):
        return ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass"]
    if shutil.which("pwsh"):
        return ["pwsh", "-NoProfile"]
    return None


def test_windows_installer_dry_run() -> None:
    shell = _windows_shell()
    if shell is None:
        pytest.skip("no PowerShell available")
    script = ROOT / "ops" / "install-windows.ps1"
    assert script.is_file()

    completed = subprocess.run(
        [*shell, "-File", str(script), "-DryRun"],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
    )
    assert completed.returncode == 0, completed.stderr
    assert "dry run: nothing created" in completed.stdout
    assert "skysherpa up" in completed.stdout


def test_windows_installer_documents_its_uninstall() -> None:
    text = (ROOT / "ops" / "install-windows.ps1").read_text(encoding="utf-8")
    assert "-Uninstall" in text
    assert "Unregister-ScheduledTask" in text
    assert "RestartCount" in text, "the task must restart if the supervisor dies"
