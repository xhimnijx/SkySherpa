"""Oracle tests: beta quantiles, model behaviour on known schedules, backtest sanity."""

from __future__ import annotations

import random
from datetime import date, timedelta

import pytest

from skysherpa.oracle import (
    Calibrator,
    CoverageModel,
    ModelParams,
    PresenceMatrix,
    beta_ppf,
    latest_calibrator,
    rolling_origin,
)


def test_beta_ppf_uniform() -> None:
    assert beta_ppf(0.05, 1, 1) == pytest.approx(0.05, abs=1e-4)
    assert beta_ppf(0.50, 1, 1) == pytest.approx(0.50, abs=1e-4)
    assert beta_ppf(0.95, 1, 1) == pytest.approx(0.95, abs=1e-4)


def test_beta_ppf_symmetric() -> None:
    assert beta_ppf(0.5, 2, 2) == pytest.approx(0.5, abs=1e-4)
    # Beta(8, 2) is left-skewed, so its median sits above its mean of 0.8.
    assert beta_ppf(0.5, 8, 2) == pytest.approx(0.82, abs=0.01)


def test_beta_ppf_interval_shrinks_with_data() -> None:
    narrow = beta_ppf(0.95, 200, 200) - beta_ppf(0.05, 200, 200)
    wide = beta_ppf(0.95, 2, 2) - beta_ppf(0.05, 2, 2)
    assert narrow < wide


def _matrix(position: str, days: int, slots_online: set[int]) -> PresenceMatrix:
    start = date(2026, 9, 1)
    rows = []
    for offset in range(days):
        day = start + timedelta(days=offset)
        for slot in range(96):
            if day.weekday() < 5:
                rows.append((day, slot, 1 if slot in slots_online else 0))
    return PresenceMatrix(days={position: rows})


def test_model_learns_a_simple_schedule() -> None:
    matrix = _matrix("EGLL_TWR", 56, {72, 73, 74, 75, 76, 77})  # 18:00-19:30
    model = CoverageModel.fit(matrix, reference_date=date(2026, 10, 27))
    at_peak = model.predict("EGLL_TWR", 1, 74).p
    at_night = model.predict("EGLL_TWR", 1, 20).p
    assert at_peak > 0.6
    # A slot never seen online still carries a small prior floor: 8 observations of
    # absence move the estimate toward zero without ever reaching it.
    assert at_night < 0.15


def test_model_interval_brackets_estimate() -> None:
    matrix = _matrix("EGLL_TWR", 28, {72, 73})
    model = CoverageModel.fit(matrix, reference_date=date(2026, 9, 29))
    forecast = model.predict("EGLL_TWR", 2, 72)
    assert forecast.p_lo <= forecast.p <= forecast.p_hi


def test_unknown_position_falls_back_to_prior() -> None:
    matrix = _matrix("EGLL_TWR", 14, {72})
    model = CoverageModel.fit(matrix, reference_date=date(2026, 9, 15))
    forecast = model.predict("ZZZZ_CTR", 0, 0)
    assert 0.0 < forecast.p < 1.0
    assert forecast.n_eff == 0.0


def test_few_observations_do_not_imply_certainty() -> None:
    start = date(2026, 9, 1)
    rows = []
    for offset in range(2):  # a position seen on only two days
        day = start + timedelta(days=offset)
        for slot in range(96):
            rows.append((day, slot, 1 if 72 <= slot <= 76 else 0))
    matrix = PresenceMatrix(days={"EGLL_TWR": rows})
    model = CoverageModel.fit(matrix, reference_date=start + timedelta(days=1))
    forecast = model.predict("EGLL_TWR", 0, 74)
    assert 0.5 < forecast.p < 0.95, "two perfect observations must not mean certainty"
    assert forecast.p_hi - forecast.p_lo > 0.15, "sparse data must widen the interval"


def test_recent_data_outweighs_old() -> None:
    start = date(2026, 9, 1)
    rows_all = []
    rows_old = []
    for offset in range(56):
        day = start + timedelta(days=offset)
        for slot in range(96):
            # The position only starts appearing in the last two weeks.
            online = 0 if offset < 40 else 1
            is_evening = 72 <= slot <= 76
            rows_all.append((day, slot, online if is_evening else 0))
            if offset < 40:
                rows_old.append((day, slot, 0))
    model_all = CoverageModel.fit(
        PresenceMatrix(days={"EGLL_TWR": rows_all}), reference_date=start + timedelta(days=55)
    )
    model_old = CoverageModel.fit(
        PresenceMatrix(days={"EGLL_TWR": rows_old}), reference_date=start + timedelta(days=39)
    )
    recent = model_all.predict("EGLL_TWR", 5, 74).p
    stale = model_old.predict("EGLL_TWR", 5, 74).p
    assert recent > stale + 0.1, "the model must notice a position that just started"


def test_calibrator_is_monotone() -> None:
    calibrator = Calibrator.fit([(0.1, 0), (0.2, 0), (0.3, 1), (0.4, 1), (0.5, 1)])
    values = [calibrator.apply(p) for p in (0.05, 0.15, 0.25, 0.35, 0.45, 0.55)]
    assert values == sorted(values)
    assert all(0.0 <= value <= 1.0 for value in values)


def test_model_applies_calibrator() -> None:
    matrix = _matrix("EGLL_TWR", 28, {72, 73})
    model = CoverageModel.fit(matrix, reference_date=date(2026, 9, 29))
    raw = model.predict("EGLL_TWR", 2, 72).p
    model.calibrate_with(Calibrator(knots=[(0.0, 0.0), (1.0, 0.5)]))
    assert model.predict("EGLL_TWR", 2, 72).p == pytest.approx(raw * 0.5, abs=1e-6)


def test_latest_calibrator_needs_two_windows() -> None:
    matrix = _matrix("EGLL_TWR", 5, {72})
    assert latest_calibrator(matrix).knots == []


def test_latest_calibrator_fits_on_a_holdout() -> None:
    start = date(2026, 9, 1)
    rng = random.Random(9)
    rows = []
    for offset in range(40):
        day = start + timedelta(days=offset)
        for slot in range(96):
            probability = 0.8 if 70 <= slot <= 80 else 0.05
            rows.append((day, slot, 1 if rng.random() < probability else 0))
    matrix = PresenceMatrix(days={"EGLL_TWR": rows})
    calibrator = latest_calibrator(matrix, horizon_days=7)
    assert calibrator.knots, "40 days is plenty for a one-week holdout"
    assert all(0.0 <= knot[1] <= 1.0 for knot in calibrator.knots)


def test_backtest_reports_walk_forward_calibration() -> None:
    start = date(2026, 9, 1)
    rng = random.Random(5)
    rows = []
    for offset in range(60):
        day = start + timedelta(days=offset)
        for slot in range(96):
            probability = 0.9 if 70 <= slot <= 78 else 0.02
            rows.append((day, slot, 1 if rng.random() < probability else 0))
    matrix = PresenceMatrix(days={"EGLL_TWR": rows})
    report = rolling_origin(matrix, folds=3, horizon_days=7)
    assert report.calibrated_ece >= 0.0
    assert any("walk-forward" in note for note in report.notes)
    matrix = _matrix("EGLL_TWR", 5, {72})
    report = rolling_origin(matrix, folds=2, horizon_days=7)
    assert report.predictions == 0
    assert report.notes


def test_backtest_runs_and_reports() -> None:
    start = date(2026, 9, 1)
    rng = random.Random(5)
    rows = []
    for offset in range(60):
        day = start + timedelta(days=offset)
        for slot in range(96):
            p = 0.9 if 70 <= slot <= 78 else 0.02
            rows.append((day, slot, 1 if rng.random() < p else 0))
    matrix = PresenceMatrix(days={"EGLL_TWR": rows})
    report = rolling_origin(matrix, folds=3, horizon_days=7)
    assert report.folds >= 1
    assert report.predictions > 0
    assert 0.0 <= report.brier <= 1.0
    assert 0.0 <= report.auc <= 1.0
    assert report.brier_skill > 0.0
    assert len(report.gates()) == 4


def test_model_params_are_honoured() -> None:
    matrix = _matrix("EGLL_TWR", 30, {72})
    wide = CoverageModel.fit(
        matrix, reference_date=date(2026, 9, 30), params=ModelParams(slot_bandwidth=6)
    )
    narrow = CoverageModel.fit(
        matrix, reference_date=date(2026, 9, 30), params=ModelParams(slot_bandwidth=0)
    )
    # Slot 76 is four slots from the staffed hour: only the wide kernel reaches it.
    wide_p = wide.predict("EGLL_TWR", 1, 76).p
    narrow_p = narrow.predict("EGLL_TWR", 1, 76).p
    assert wide_p > narrow_p
