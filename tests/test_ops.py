"""The systemd units are configuration, and configuration fails quietly.

These tests parse them the way systemd would (roughly) and assert the contract the
installer relies on: every unit points at the venv interpreter, every long-running service
restarts on failure, the health check is driven by a timer, and nobody forgot to leave the
@REPO@ placeholder in place -- an unsubstituted path starts a unit that exits immediately.
"""

from __future__ import annotations

import configparser
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SYSTEMD = ROOT / "ops" / "systemd"
SERVICES = sorted(SYSTEMD.glob("*.service"))
TIMERS = sorted(SYSTEMD.glob("*.timer"))
PLACEHOLDER = "@REPO@"
STR_PUBLISH_PLACEHOLDER = "enable --now skysherpa-publish.service"


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


def test_the_installer_starts_the_collector_before_the_publisher() -> None:
    """The publisher exits immediately without a database, and `set -e` would abort."""
    installer = (ROOT / "ops" / "install-ubuntu.sh").read_text(encoding="utf-8")
    lines = installer.splitlines()

    def executable_index(unit: str) -> int:
        # Skip the dry-run's "would run: ..." lines; find the real invocation.
        return next(
            index
            for index, line in enumerate(lines)
            if line.startswith(f"systemctl --user enable --now {unit}")
        )

    ingest_at = executable_index("skysherpa-ingest.service")
    publish_at = executable_index("skysherpa-publish.service")
    assert ingest_at < publish_at
    assert "skysherpa.sqlite3" in installer
    # A failed first start must be reported, not fatal to the install.
    assert lines[publish_at].rstrip().endswith("||")


def test_the_installer_guards_against_common_first_run_failures() -> None:
    installer = (ROOT / "ops" / "install-ubuntu.sh").read_text(encoding="utf-8")
    # A space in the path breaks the unquoted ExecStart that sed writes into the units.
    assert 'case "$repo" in' in installer
    # Without a session bus, systemctl --user fails with an error about D-Bus.
    assert "systemctl --user show-environment" in installer


def _substituted(unit: Path) -> configparser.ConfigParser:
    """Apply the same substitution the installer's sed performs, then parse."""
    text = unit.read_text(encoding="utf-8").replace(PLACEHOLDER, ROOT.as_posix())
    parser = configparser.ConfigParser(strict=False, interpolation=None)
    parser.read_string(text)
    return parser


def test_substituted_units_point_at_the_repo() -> None:
    """What the installer writes must be an absolute path to this checkout, not a template."""
    for unit in SERVICES:
        parser = _substituted(unit)
        service = parser["Service"]
        assert PLACEHOLDER not in service["ExecStart"]
        assert service["WorkingDirectory"] == ROOT.as_posix()
        assert service["ExecStart"].startswith(f"{ROOT.as_posix()}/.venv/bin/python")


def test_substituted_units_invoke_the_right_commands() -> None:
    expected = {
        "skysherpa-ingest.service": "-m skysherpa ingest --interval 15",
        "skysherpa-publish.service": "-m skysherpa publish --watch",
        "skysherpa-web.service": "-m http.server 8099",
        "skysherpa-check.service": "-m skysherpa status --check",
    }
    for name, command in expected.items():
        service = _substituted(SYSTEMD / name)["Service"]
        assert command in service["ExecStart"], name
    publish = _substituted(SYSTEMD / "skysherpa-publish.service")["Service"]["ExecStart"]
    assert "--timeout" in publish and "--interval" in publish


def _bash() -> str | None:
    """Find a POSIX shell; the Windows dev box has one inside Git for Windows."""
    found = shutil.which("bash")
    if found:
        return found
    for candidate in (
        r"C:\Program Files\Git\bin\bash.exe",
        r"C:\Program Files\Git\usr\bin\bash.exe",
        "/bin/bash",
    ):
        if Path(candidate).exists():
            return candidate
    return None


def test_shell_scripts_parse_with_bash() -> None:
    """Run the real shell parser when one is available; skip on a bare Windows box."""
    bash = _bash()
    if bash is None:
        pytest.skip("no bash available")
    for script in (ROOT / "scripts" / "dev.sh", ROOT / "ops" / "install-ubuntu.sh"):
        completed = subprocess.run([bash, "-n", str(script)], capture_output=True, text=True)
        assert completed.returncode == 0, f"{script.name}: {completed.stderr}"


def test_installer_dry_run_plans_without_writing(tmp_path: Path) -> None:
    """The dry run is the safe way to find out what the installer would do."""
    bash = _bash()
    if bash is None:
        pytest.skip("no bash available")

    env = {**os.environ, "XDG_CONFIG_HOME": str(tmp_path), "HOME": str(tmp_path)}
    completed = subprocess.run(
        [bash, str(ROOT / "ops" / "install-ubuntu.sh"), "--dry-run"],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(ROOT),
    )
    assert completed.returncode == 0, completed.stderr
    assert "dry run: nothing created" in completed.stdout
    assert "enable --now skysherpa-ingest.service" in completed.stdout
    assert STR_PUBLISH_PLACEHOLDER in completed.stdout
    # Nothing may be written: no unit directory, and no virtualenv.
    assert not (tmp_path / "systemd" / "user").exists()
    assert not (ROOT / ".venv-dryrun-probe").exists()


def test_installer_rejects_unknown_arguments() -> None:
    bash = _bash()
    if bash is None:
        pytest.skip("no bash available")
    completed = subprocess.run(
        [bash, str(ROOT / "ops" / "install-ubuntu.sh"), "--nope"],
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 1
    assert "unknown argument" in completed.stdout


def test_installer_uninstall_removes_only_its_own_units(tmp_path: Path) -> None:
    """Uninstalling must not delete the collected history, and must not touch other units."""
    bash = _bash()
    if bash is None:
        pytest.skip("no bash available")

    unit_dir = tmp_path / "systemd" / "user"
    unit_dir.mkdir(parents=True)
    ours = ["skysherpa-ingest.service", "skysherpa-publish.service", "skysherpa-check.timer"]
    for name in ours:
        (unit_dir / name).write_text("x", encoding="utf-8")
    (unit_dir / "something-else.service").write_text("x", encoding="utf-8")
    data = tmp_path / "data"
    data.mkdir()
    (data / "skysherpa.sqlite3").write_text("pretend history", encoding="utf-8")

    env = {**os.environ, "XDG_CONFIG_HOME": str(tmp_path), "HOME": str(tmp_path)}
    completed = subprocess.run(
        [bash, str(ROOT / "ops" / "install-ubuntu.sh"), "--uninstall"],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(ROOT),
    )
    assert completed.returncode == 0, completed.stderr
    for name in ours:
        assert not (unit_dir / name).exists(), name
    assert (unit_dir / "something-else.service").exists()
    assert (data / "skysherpa.sqlite3").read_text(encoding="utf-8") == "pretend history"
    assert "left in place" in completed.stdout


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

