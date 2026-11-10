# Verification log

What has been checked end to end, when, and against which commit. The test suite is the
test plan; this is the record of the *throw-it-away-and-start-again* checks, because those
are the ones that catch "works on my machine" — a missing file, a dependency on untracked
state, a command the README documents that no longer runs.

Each entry is reproducible from the commit named.

---

## 2026-11-10 — the Ubuntu target, after the lint pass

Commit `a256259`. Python 3.14.3 on the development box; Ubuntu is exercised by CI.

| check | result |
| --- | --- |
| `ruff check` / `ruff format --check` | clean (31 findings fixed, 22 files reformatted) |
| `pytest -m "not network"` | 338 passed |
| end to end, 40 simulated days | 783 KB bundle · `verify` coherent · 4/4 gates · exit 0 |
| gate numbers at 40 days | skill 0.488 · ECE 0.046 → 0.040 calibrated · AUC 0.963 · coverage 0.997 |
| installer | `--dry-run` exits 0 writing nothing; `--uninstall` removes only its own units |
| shell | `bash -n` clean on both scripts; `scripts/dev.sh version` runs |
| live network | `doctor`: 7/8 healthy, the only failure the documented `atc_history` timeout |

CI on `ubuntu-latest` runs the same checks on Python 3.11, 3.12 and 3.13, plus `make help`
and `make -n` for three targets — the only place the Makefile is ever executed.

### The real-data gate

Two days of real history are in the database; the gate needs 35. `skysherpa status` prints
the date it opens, currently **2026-12-14**. Until then every number above is synthetic,
and says nothing about forecast accuracy on VATSIM.

---

## 2026-11-09 — clean clone, full pipeline

Commit `5a83998`. Python 3.14.3, Node 24.14.1, Windows.

| check | result |
| --- | --- |
| `git clone` → tracked files | 76 |
| `pytest -m "not network"` | 303 passed, 4 skipped |
| `simulate --days 56 --force` | 350 sessions · 1,232 flights · 64,512 presence slots |
| `publish` | 783 KB bundle, 17 files |
| `verify` | bundle is coherent |
| `backtest` | 4/4 gates pass, exit 0 |
| site from the clone's `dist/` | 36 routes ranked, ribbon, evidence badges |

Backtest detail: Brier skill 0.543 (baseline 0.096), calibration error 0.036 → 0.016 after
walk-forward calibration, AUC 0.969, predictive-interval coverage 0.998.

The four skips are `tests/test_web_live.py` and `tests/test_web_taste.py`, which read a
published bundle; they pass once `publish` has run. That ordering quirk is deliberate and
not worth fixing.

### What this does not prove

Every number above comes from the **synthetic** network. It shows the pipeline is
mechanically sound from a cold start — not that the forecast is accurate on VATSIM. That
is the real-data backtest, which needs 35 days of collection and is tracked by
`skysherpa status`. Expect worse numbers there; the gate exists to find out.

---

## How to repeat it

```powershell
$tmp = Join-Path $env:TEMP "skysherpa-verify"
git clone . $tmp
cd $tmp
$env:PYTHONPATH = "src"
python -m pytest -m "not network"
python -m skysherpa simulate --days 56 --force
python -m skysherpa publish
python -m skysherpa verify
python -m skysherpa backtest
```
