"""Does the forecast actually work? Rolling-origin backtest, with gates.

A coverage model is easy to make plausible and hard to make honest. The protocol here is the
one used for weather and demand forecasting: for each fold, train strictly on data before a
cutoff, predict the following week, and compare against what happened. Nothing from the
future leaks into training, and the baseline is the position's own base rate -- so "skill"
means "better than knowing how often this position is usually online".

The interval check compares the *observed rate* for each (position, weekday, slot) cell
against the interval the model claimed for it. A Bernoulli draw has no meaningful interval
of its own; the claim being tested is "this position is online between 15% and 45% of
Tuesday evenings".
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import timedelta

from .model import Calibrator, CoverageModel, ModelParams, PresenceMatrix

__all__ = ["BacktestReport", "rolling_origin"]


#: A held-out prediction counts as "backed" when the training data behind its cell is worth
#: more than the prior. Below this the model is mostly repeating the group average, and a
#: good Brier score there says more about the prior than about the forecast.
BACKED_MIN = 3.0


@dataclass(slots=True)
class BacktestReport:
    folds: int = 0
    predictions: int = 0
    positions: int = 0
    brier: float = 0.0
    brier_baseline: float = 0.0
    brier_skill: float = 0.0
    backed_predictions: int = 0
    backed_share: float = 0.0
    brier_skill_backed: float = 0.0
    auc: float = 0.0
    ece: float = 0.0
    calibrated_ece: float = 0.0
    reliability_max_dev: float = 0.0
    interval_coverage: float = 0.0
    cells_checked: int = 0
    bins: list[tuple[float, float, int]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {
            "folds": self.folds,
            "predictions": self.predictions,
            "positions": self.positions,
            "brier": round(self.brier, 4),
            "brier_baseline": round(self.brier_baseline, 4),
            "brier_skill": round(self.brier_skill, 4),
            "backed_predictions": self.backed_predictions,
            "backed_share": round(self.backed_share, 4),
            "brier_skill_backed": round(self.brier_skill_backed, 4),
            "auc": round(self.auc, 4),
            "ece": round(self.ece, 4),
            "reliability_max_dev": round(self.reliability_max_dev, 4),
            "interval_coverage": round(self.interval_coverage, 4),
            "cells_checked": self.cells_checked,
        }

    def gates(self) -> list[tuple[str, bool | None, str]]:
        """Pass, fail, or None for "could not be evaluated".

        The interval check needs at least three held-out observations per cell. With few
        folds nothing qualifies, and reporting that as coverage 0.000 and a failed gate is a
        false negative: the check did not run, it did not fail.
        """
        interval: bool | None = None if self.cells_checked == 0 else self.interval_coverage >= 0.85
        interval_value = (
            "n/a, 0 cells" if self.cells_checked == 0 else f"{self.interval_coverage:.3f}"
        )
        return [
            ("brier skill >= 0.25", self.brier_skill >= 0.25, f"{self.brier_skill:.3f}"),
            ("calibration error <= 0.05", self.ece <= 0.05, f"{self.ece:.3f}"),
            ("auc >= 0.80", self.auc >= 0.80, f"{self.auc:.3f}"),
            ("interval coverage >= 0.85", interval, interval_value),
        ]


def _auc(samples: list[tuple[float, int]]) -> float:
    positives = sum(1 for _, y in samples if y == 1)
    negatives = len(samples) - positives
    if not positives or not negatives:
        return 0.5
    ordered = sorted(samples, key=lambda item: item[0])
    ranks: list[float] = []
    index = 0
    while index < len(ordered):
        end = index
        while end + 1 < len(ordered) and ordered[end + 1][0] == ordered[index][0]:
            end += 1
        average_rank = (index + end) / 2.0 + 1.0
        ranks.extend([average_rank] * (end - index + 1))
        index = end + 1
    rank_sum = sum(rank for rank, (_, y) in zip(ranks, ordered, strict=False) if y == 1)
    return (rank_sum - positives * (positives + 1) / 2.0) / (positives * negatives)


def _reliability(
    samples: list[tuple[float, int]], bins: int = 10
) -> tuple[float, float, list[tuple[float, float, int]]]:
    """Expected calibration error, worst meaningful bin, and the diagram.

    Max deviation over *all* bins is a trap: a bin holding ten predictions can dominate
    the statistic and send you chasing noise. The calibration error is weighted by bin
    size, which is what a user actually experiences; the worst-bin figure ignores bins
    with fewer than 1% of predictions.
    """
    buckets: list[list[tuple[float, int]]] = [[] for _ in range(bins)]
    for p, y in samples:
        index = min(bins - 1, max(0, int(p * bins)))
        buckets[index].append((p, y))
    rows: list[tuple[float, float, int]] = []
    total = len(samples) or 1
    ece = 0.0
    worst = 0.0
    for bucket in buckets:
        if not bucket:
            continue
        mean_p = sum(p for p, _ in bucket) / len(bucket)
        observed = sum(y for _, y in bucket) / len(bucket)
        rows.append((mean_p, observed, len(bucket)))
        ece += (len(bucket) / total) * abs(mean_p - observed)
        if len(bucket) >= max(50, 0.01 * total):
            worst = max(worst, abs(mean_p - observed))
    return ece, worst, rows


def _beta_binomial_interval(a: float, b: float, n: int, q: float = 0.90) -> tuple[int, int]:
    """Predictive interval for the *count* of online days out of ``n``.

    The model's posterior is on the underlying rate; what we can actually observe is a
    finite sample from it. Checking a rate interval against a seven-day empirical rate
    would fail almost every time for purely binomial reasons, so the check is done on the
    Beta-Binomial predictive distribution instead.
    """
    if n <= 0:
        return (0, 0)
    log_beta_ab = math.lgamma(a) + math.lgamma(b) - math.lgamma(a + b)
    weights: list[float] = []
    for k in range(n + 1):
        log_pmf = (
            math.lgamma(n + 1)
            - math.lgamma(k + 1)
            - math.lgamma(n - k + 1)
            + math.lgamma(k + a)
            + math.lgamma(n - k + b)
            - math.lgamma(n + a + b)
            - log_beta_ab
        )
        weights.append(math.exp(log_pmf))
    total = sum(weights) or 1.0
    probabilities = [weight / total for weight in weights]
    tail = (1.0 - q) / 2.0

    low = 0
    cumulative = 0.0
    for k, probability in enumerate(probabilities):
        cumulative += probability
        if cumulative >= tail:
            low = k
            break
    high = n
    cumulative = 0.0
    for k, probability in enumerate(probabilities):
        cumulative += probability
        if cumulative >= 1.0 - tail:
            high = k
            break
    return low, high


def latest_calibrator(
    matrix: PresenceMatrix,
    *,
    horizon_days: int = 7,
    params: ModelParams | None = None,
) -> Calibrator:
    """Fit an isotonic map on a genuinely out-of-sample holdout.

    Train on everything before the last ``horizon_days``, predict that holdout, and fit the
    map to those predictions. The production model is then refitted on *all* the data and
    given this map -- the standard walk-forward arrangement, and the only way the numbers
    mean anything.
    """
    days = sorted({day for rows in matrix.days.values() for day, _, _ in rows})
    if len(days) < horizon_days * 2:
        return Calibrator(knots=[])

    last_day = days[-1]
    cutoff = last_day - timedelta(days=horizon_days)
    training_rows = {
        position: [row for row in rows if row[0] < cutoff] for position, rows in matrix.days.items()
    }
    training = PresenceMatrix(
        days={position: rows for position, rows in training_rows.items() if rows}
    )
    if not training.days:
        return Calibrator(knots=[])

    model = CoverageModel.fit(training, reference_date=cutoff, params=params)
    samples: list[tuple[float, int]] = []
    for position, rows in matrix.days.items():
        for day, slot, online in rows:
            if cutoff <= day <= last_day:
                # No interval needed here: computing Beta quantiles for every holdout
                # row is the difference between a 2-second and a 60-second backtest.
                samples.append(
                    (model.predict(position, day.weekday(), slot, with_interval=False).p, online)
                )
    return Calibrator.fit(samples)


def rolling_origin(
    matrix: PresenceMatrix,
    *,
    folds: int = 4,
    horizon_days: int = 7,
    params: ModelParams | None = None,
) -> BacktestReport:
    report = BacktestReport()
    all_days = sorted({day for rows in matrix.days.values() for day, _, _ in rows})
    needed = horizon_days * (folds + 1)
    if len(all_days) < needed:
        report.notes.append(
            f"only {len(all_days)} day(s) of presence data; {needed} needed for "
            f"{folds} rolling-origin folds"
        )
        return report

    last_day = all_days[-1]
    samples: list[tuple[float, int]] = []
    baseline: list[tuple[float, int]] = []
    backed_samples: list[tuple[float, int]] = []
    backed_baseline: list[tuple[float, int]] = []
    fold_samples: list[list[tuple[float, int]]] = []
    cell_params: dict[tuple[str, int, int], list[tuple[float, float]]] = defaultdict(list)
    cell_outcomes: dict[tuple[str, int, int], list[int]] = defaultdict(list)

    for fold in range(folds):
        cutoff = last_day - timedelta(days=horizon_days * (folds - fold))
        window_end = cutoff + timedelta(days=horizon_days)

        training_rows = {
            position: [row for row in rows if row[0] < cutoff]
            for position, rows in matrix.days.items()
        }
        training = PresenceMatrix(
            days={position: rows for position, rows in training_rows.items() if rows}
        )
        if not training.days:
            continue

        model = CoverageModel.fit(training, reference_date=cutoff, params=params)
        base_rates = {
            position: (sum(1 for row in rows if row[2]), len(rows))
            for position, rows in training.days.items()
        }

        current: list[tuple[float, int]] = []
        for position, rows in matrix.days.items():
            for day, slot, online in rows:
                if not (cutoff <= day < window_end):
                    continue
                forecast = model.predict(position, day.weekday(), slot, with_interval=False)
                samples.append((forecast.p, online))
                current.append((forecast.p, online))
                hits, total = base_rates.get(position, (1, 2))
                baseline.append(((hits + 1) / (total + 2), online))
                if forecast.n_eff >= BACKED_MIN:
                    backed_samples.append((forecast.p, online))
                    backed_baseline.append(((hits + 1) / (total + 2), online))
                key = (position, day.weekday(), slot)
                cell_params[key].append(model.beta_params(position, day.weekday(), slot))
                cell_outcomes[key].append(online)
        fold_samples.append(current)
        report.folds += 1

    if not samples:
        report.notes.append("no held-out observations; dataset too small")
        return report

    report.predictions = len(samples)
    report.positions = len(matrix.days)
    report.brier = sum((p - y) ** 2 for p, y in samples) / len(samples)
    report.brier_baseline = sum((p - y) ** 2 for p, y in baseline) / len(baseline)
    report.brier_skill = (
        1.0 - report.brier / report.brier_baseline if report.brier_baseline > 0 else 0.0
    )
    report.auc = _auc(samples)
    report.ece, report.reliability_max_dev, report.bins = _reliability(samples)

    # How much of that score rests on cells the model had actually seen, rather than on the
    # prior it emits for thin ones. A run whose skill is carried by prior-only cells is not
    # evidence of anything.
    report.backed_predictions = len(backed_samples)
    report.backed_share = len(backed_samples) / len(samples)
    if backed_samples:
        brier_backed = sum((p - y) ** 2 for p, y in backed_samples) / len(backed_samples)
        baseline_backed = sum((p - y) ** 2 for p, y in backed_baseline) / len(backed_baseline)
        report.brier_skill_backed = (
            1.0 - brier_backed / baseline_backed if baseline_backed > 0 else 0.0
        )

    covered = 0
    checked = 0
    for key, params_per_fold in cell_params.items():
        outcomes = cell_outcomes[key]
        if len(outcomes) < 3:
            continue
        checked += 1
        a = sum(item[0] for item in params_per_fold) / len(params_per_fold)
        b = sum(item[1] for item in params_per_fold) / len(params_per_fold)
        low, high = _beta_binomial_interval(a, b, len(outcomes))
        if low <= sum(outcomes) <= high:
            covered += 1
    report.cells_checked = checked
    report.interval_coverage = covered / checked if checked else 0.0
    if not checked:
        report.notes.append(
            "interval coverage not evaluated: no cell had three or more held-out observations "
            "(try more folds)"
        )

    # Walk-forward calibration: fit the isotonic map on one fold, apply it to the next.
    # Fitting and evaluating on the same data would flatter the map into meaninglessness.
    if len(fold_samples) > 1:
        calibrated: list[tuple[float, int]] = []
        for index in range(1, len(fold_samples)):
            calibrator = Calibrator.fit(fold_samples[index - 1])
            if not calibrator.knots:
                continue
            calibrated.extend(
                (calibrator.apply(probability), outcome)
                for probability, outcome in fold_samples[index]
            )
        if calibrated:
            report.calibrated_ece, _, _ = _reliability(calibrated)
            report.notes.append(
                f"walk-forward calibration: ECE {report.ece:.3f} -> {report.calibrated_ece:.3f}"
            )

    if report.brier_skill < 0.25:
        report.notes.append("below the 0.25 skill gate")
    if report.backed_share < 0.5:
        report.notes.append(
            f"only {report.backed_share:.0%} of held-out cells had >= {BACKED_MIN:g} "
            f"observations; skill on those is {report.brier_skill_backed:.3f}"
        )
    return report
