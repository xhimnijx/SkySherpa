"""Run the three long-lived processes from one command, on any platform.

The product is three processes: a collector, a publisher, and a static server. On Ubuntu
systemd supervises them. Windows has no systemd, and asking a user to keep three consoles
open is not a product, so this module is the cross-platform supervisor: one command that
starts all three, restarts whatever dies, and shuts them down cleanly.

It is deliberately stdlib-only and deliberately simple -- a poll loop, not a process
manager. Each child's output goes to its own file under ``logs/`` because a supervisor that
swallows output is worse than no supervisor.
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from .config import Settings

__all__ = ["INSTALL_HINT", "Child", "commands", "supervise"]

#: Why a child might keep dying, in the order the causes are worth checking.
INSTALL_HINT = (
    "if a child keeps exiting immediately, check `logs/<name>.log`; the usual causes are "
    "a missing database (start the collector first) and a port already in use"
)

BACKOFF_START = 5.0
BACKOFF_MAX = 60.0
POLL_SECONDS = 0.5


@dataclass
class Child:
    name: str
    argv: list[str]
    process: subprocess.Popen | None = None
    restarts: int = 0
    backoff: float = BACKOFF_START
    next_start: float = 0.0
    ready_at: float = 0.0


def commands(
    settings: Settings,
    *,
    out: Path,
    interval: int,
    publish_interval: int,
    port: int,
    host: str = "127.0.0.1",
) -> list[Child]:
    python = sys.executable
    # -u: the children write to files, where Python block-buffers. Without it a clean
    # shutdown's final messages are lost and a killed child looks identical to a stopped one.
    return [
        Child(
            "collector",
            [python, "-u", "-m", "skysherpa", "ingest", "--interval", str(interval)],
        ),
        Child(
            "publisher",
            [
                python,
                "-u",
                "-m",
                "skysherpa",
                "publish",
                "--watch",
                "--out",
                str(out),
                "--interval",
                str(publish_interval),
                "--timeout",
                "300",
            ],
        ),
        Child(
            "site",
            [
                python,
                "-u",
                "-m",
                "http.server",
                str(port),
                "--bind",
                host,
                "--directory",
                str(out),
            ],
        ),
    ]


def _spawn(child: Child, log_dir: Path) -> subprocess.Popen:
    log_dir.mkdir(parents=True, exist_ok=True)
    handle = open(log_dir / f"{child.name}.log", "a", encoding="utf-8", buffering=1)  # noqa: SIM115
    handle.write(f"\n--- {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} starting ---\n")
    creation = 0
    if os.name == "nt":  # a new group so CTRL_BREAK can be delivered to this child alone
        creation = subprocess.CREATE_NEW_PROCESS_GROUP
    return subprocess.Popen(
        child.argv,
        stdout=handle,
        stderr=subprocess.STDOUT,
        env=os.environ.copy(),
        cwd=str(log_dir.parent),
        creationflags=creation,
    )


def _stop(child: Child, *, grace: float = 5.0) -> None:
    process = child.process
    if process is None or process.poll() is not None:
        return
    try:
        if os.name == "nt":
            # Python maps CTRL_BREAK to KeyboardInterrupt, which runs the same cleanup as
            # Ctrl-C. It is only delivered when the child shares a console with a parent
            # that has one -- under a captured stdout it does not arrive, so the wait below
            # falls through to kill(). The collector recovers orphaned flights on its next
            # start, which is why an abrupt stop is survivable rather than corrupting.
            process.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            process.terminate()
        process.wait(timeout=grace)
    except (subprocess.TimeoutExpired, OSError):
        process.kill()


def _ready(child: Child, out: Path) -> bool:
    """The site has nothing to serve until the first bundle exists."""
    if child.name != "site":
        return True
    return (out / "data" / "meta.json").is_file()


def supervise(
    settings: Settings,
    *,
    out: Path,
    interval: int = 15,
    publish_interval: int = 900,
    port: int = 8099,
    seconds: float | None = None,
    quiet: bool = False,
) -> int:
    """Run until told to stop. ``seconds`` bounds the run, for tests and short demos."""
    log_dir = settings.root / "logs"
    children = commands(
        settings,
        out=out,
        interval=interval,
        publish_interval=publish_interval,
        port=port,
    )

    stopping = False

    def _request_stop(signum: int, frame: object) -> None:
        nonlocal stopping
        stopping = True

    previous = {}
    for name in ("SIGINT", "SIGTERM"):
        if hasattr(signal, name):
            with contextlib.suppress(ValueError, OSError):
                previous[name] = signal.signal(getattr(signal, name), _request_stop)

    started = time.monotonic()
    say = (lambda *_: None) if quiet else (lambda message: print(message, flush=True))
    say(
        f"supervising {', '.join(child.name for child in children)} "
        f"| logs in {log_dir} | ctrl-c to stop"
    )

    try:
        while not stopping:
            now = time.monotonic()
            if seconds is not None and now - started >= seconds:
                break

            for child in children:
                if child.process is not None and child.process.poll() is None:
                    child.backoff = BACKOFF_START
                    continue

                if child.process is not None:  # it exited since the last pass
                    code = child.process.returncode
                    child.process = None
                    child.restarts += 1
                    child.next_start = now + child.backoff
                    say(
                        f"{child.name} exited with {code}; "
                        f"restarting in {child.backoff:.0f}s ({INSTALL_HINT})"
                    )
                    child.backoff = min(BACKOFF_MAX, child.backoff * 2)
                    continue

                if now >= child.next_start and _ready(child, out):
                    child.process = _spawn(child, log_dir)
                    say(f"{child.name} started (pid {child.process.pid})")

            time.sleep(POLL_SECONDS)
    finally:
        for child in children:
            _stop(child)
        say("stopped")
        for name, handler in previous.items():
            with contextlib.suppress(ValueError, OSError):
                signal.signal(getattr(signal, name), handler)

    return 0
