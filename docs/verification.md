# Verification log

What has been checked end to end, when, and against which commit. The test suite is the
test plan; this is the record of the *throw-it-away-and-start-again* checks, because those
are the ones that catch "works on my machine" — a missing file, a dependency on untracked
state, a command the README documents that no longer runs.

Each entry is reproducible from the commit named.

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
