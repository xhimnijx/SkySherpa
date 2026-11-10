"""The systemd units are configuration, and configuration fails quietly.

These tests parse them the way systemd would (roughly) and assert the contract the
installer relies on: every unit points at the venv interpreter, every long-running service
restarts on failure, the health check is driven by a timer, and nobody forgot to leave the
@REPO@ placeholder in place -- an unsubstituted path starts a unit that exits immediately.
"""

from __future__ import annotations

import configparser
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SYSTEMD = ROOT / "ops" / "systemd"
SERVICES = sorted(SYSTEMD.glob("*.service"))
TIMERS = sorted(SYSTEMD.glob("*.timer"))
PLACEHOLDER = "@REPO@"


def _parse(path: Path) -> configparser.ConfigParser:
    parser = configparser.ConfigParser(strict=False, interpolation=None)
    parser.read(path, encoding="utf-8")
    return parser


def test_units_exist() -> None:
    names = {path.name for path in SERVICES + TIMERS}
    assert names == {
        "skysherpa-ingest.service",
        "skysherpa-publish.service",
        "skysherpa-web.service",
        "skysherpa-check.service",
        "skysherpa-check.timer",
    }


@pytest.mark.parametrize("unit", SERVICES + TIMERS, ids=lambda path: path.name)
def test_unit_parses(unit: Path) -> None:
    parser = _parse(unit)
    assert parser.sections(), f"{unit.name} has no sections"
    if unit.suffix == ".service":
        assert "Unit" in parser and "Service" in parser
        # The oneshot service is triggered by its timer, so it is never enabled itself.
        if unit.name != "skysherpa-check.service":
            assert "Install" in parser
    else:
        assert "Timer" in parser and "Install" in parser


@pytest.mark.parametrize("unit", SERVICES, ids=lambda path: path.name)
def test_services_run_the_venv_python(unit: Path) -> None:
    service = _parse(unit)["Service"]
    assert service["ExecStart"].startswith(f"{PLACEHOLDER}/.venv/bin/python")
    # Unbuffered output matters when stdout is journald rather than a terminal.
    if "Environment" in service:
        assert "PYTHONUNBUFFERED=1" in service["Environment"]


@pytest.mark.parametrize("unit", SERVICES, ids=lambda path: path.name)
def test_long_running_services_restart(unit: Path) -> None:
    service = _parse(unit)["Service"]
    if unit.name == "skysherpa-check.service":  # oneshot, runs from the timer
        assert service.get("Type") == "oneshot"
        assert "Restart" not in service
        return
    assert service["Restart"] == "always"
    assert int(service["RestartSec"]) >= 5


def test_the_health_check_is_scheduled() -> None:
    timer = _parse(SYSTEMD / "skysherpa-check.timer")["Timer"]
    assert timer["OnUnitActiveSec"] == "10min"
    assert timer["OnBootSec"]


def test_the_installer_substitutes_every_placeholder() -> None:
    """An unsubstituted @REPO@ starts a unit that exits immediately, with a confusing error."""
    installer = (ROOT / "ops" / "install-ubuntu.sh").read_text(encoding="utf-8")
    assert "sed" in installer and PLACEHOLDER in installer
    # The timer schedules the check by unit name, so only the services carry a path.
    for unit in SERVICES:
        assert PLACEHOLDER in unit.read_text(encoding="utf-8")


def test_the_installer_requires_python_311() -> None:
    installer = (ROOT / "ops" / "install-ubuntu.sh").read_text(encoding="utf-8")
    assert "311" in installer
    assert "loginctl enable-linger" in installer


def test_the_web_unit_waits_for_the_first_bundle() -> None:
    """http.server exits on a missing directory; on first boot the bundle does not exist."""
    service = _parse(SYSTEMD / "skysherpa-web.service")["Service"]
    assert "ExecStartPre" in service
    assert "dist-real" in service["ExecStartPre"]
    assert int(service["TimeoutStartSec"]) >= 300


def test_the_installer_checks_for_venv_support() -> None:
    """A stock image often lacks python3-venv and the raw error does not say so."""
    installer = (ROOT / "ops" / "install-ubuntu.sh").read_text(encoding="utf-8")
    assert "ensurepip" in installer
    assert "python3-venv" in installer
    assert "skysherpa version" in installer


def test_launchers_do_not_assume_a_python_binary() -> None:
    """Ubuntu ships python3 and, on a clean image, no `python` at all."""
    dev = (ROOT / "scripts" / "dev.sh").read_text(encoding="utf-8")
    assert ".venv/bin/python" in dev
    assert "python3" in dev

    make = (ROOT / "Makefile").read_text(encoding="utf-8")
    assert "PYTHON ?=" in make
    assert "python3" in make
    # Every command must go through $(PYTHON) rather than a bare interpreter name.
    for line in make.splitlines():
        stripped = line.strip()
        if stripped.startswith(("python ", "PYTHONPATH=src python ")):
            raise AssertionError(f"Makefile hardcodes an interpreter: {stripped}")

