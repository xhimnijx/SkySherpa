"""The oracle: calibrated forecasts of "is this position staffed?".

Model, in one paragraph. For every position, keep a recency-weighted presence matrix over
(weekday, 15-minute slot). Smooth it across adjacent slots, because controllers work shifts
rather than isolated quarter-hours. Shrink each weekday toward the average for that *slot*,
because Tuesday alone is noisy. Then shrink the whole position toward a prior for its
facility group **at that hour of day**, because most positions are rare -- and because a
position's 24-hour average is dominated by the hours it is offline, which would drag every
evening forecast down. The result is a Beta posterior per slot, which gives an honest
interval as well as a point estimate.

That last detail is the difference between a model that passes its reliability gate and one
that quietly predicts 22% for a slot that is staffed 45% of the time.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import date

from ..store import Database

__all__ = [
    "Calibrator",
    "CoverageModel",
    "Forecast",
    "ModelParams",
    "PresenceMatrix",
    "beta_ppf",
]

SLOTS = 96
WEEKDAYS = 7


# ---------------------------------------------------------------- beta quantiles


def _betacf(a: float, b: float, x: float) -> float:
    """Continued fraction for the incomplete beta function (Lentz's method)."""
    max_iter, eps, tiny = 200, 3e-14, 1e-300
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < tiny:
        d = tiny
    d = 1.0 / d
    h = d
    for m in range(1, max_iter + 1):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < tiny:
            d = tiny
        c = 1.0 + aa / c
        if abs(c) < tiny:
            c = tiny
        d = 1.0 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < tiny:
            d = tiny
        c = 1.0 + aa / c
        if abs(c) < tiny:
            c = tiny
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < eps:
            break
    return h


def _betainc(a: float, b: float, x: float) -> float:
    """Regularised incomplete beta I_x(a, b)."""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    log_beta = math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
    front = math.exp(log_beta + a * math.log(x) + b * math.log1p(-x))
    if x < (a + 1.0) / (a + b + 2.0):
        return front * _betacf(a, b, x) / a
    return 1.0 - front * _betacf(b, a, 1.0 - x) / b


def beta_ppf(q: float, a: float, b: float, *, iterations: int = 48) -> float:
    """Quantile of Beta(a, b) by bisection. Accurate to ~1e-6, which is plenty here."""
    if q <= 0.0:
        return 0.0
    if q >= 1.0:
        return 1.0
    lo, hi = 0.0, 1.0
    for _ in range(iterations):
        mid = (lo + hi) / 2.0
        if _betainc(a, b, mid) < q:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2.0


# --------------------------------------------------------------------- presence


@dataclass(slots=True)
class PresenceMatrix:
    """``position -> [(day, slot, online)]``, exactly what ``position_slot`` holds."""

    days: dict[str, list[tuple[date, int, int]]]

    @classmethod
    def from_db(cls, db: Database) -> PresenceMatrix:
        rows = db.query("SELECT position_key, day, slot, online FROM position_slot")
        matrix: dict[str, list[tuple[date, int, int]]] = defaultdict(list)
        for row in rows:
            try:
                day = date.fromisoformat(row["day"])
            except (TypeError, ValueError):
                continue
            matrix[str(row["position_key"])].append(
                (day, int(row["slot"]), int(row["online"]))
            )
        return cls(days=dict(matrix))

    @property
    def positions(self) -> list[str]:
        return sorted(self.days)

    def observations(self) -> int:
        return sum(len(rows) for rows in self.days.values())


@dataclass(frozen=True, slots=True)
class ModelParams:
    #: Recency half-life. Controller populations turn over; last autumn is not this spring.
    half_life_days: float = 56.0
    #: Triangular kernel half-width, in 15-minute slots (2 = 30 minutes either side).
    slot_bandwidth: int = 2
    #: Pseudo-observations pulling a weekday toward the position's same-slot rate.
    #: Kept tiny: with two months of data each weekday has enough of its own signal, and
    #: pooling harder erases real weekday structure (a weekday-only tower is not 30%
    #: likely on Saturday, it is absent).
    weekday_pool: float = 0.5


@dataclass(frozen=True, slots=True)
class Forecast:
    position_key: str
    weekday: int
    slot: int
    p: float
    p_lo: float
    p_hi: float
    n_eff: float


def _group_of(position_key: str) -> str:
    return position_key.rsplit("_", 1)[-1].upper()


def _smooth(values: list[list[float]], bandwidth: int) -> list[list[float]]:
    if bandwidth <= 0:
        return [row[:] for row in values]
    out = [[0.0] * SLOTS for _ in range(WEEKDAYS)]
    for weekday in range(WEEKDAYS):
        row = values[weekday]
        for slot in range(SLOTS):
            acc = 0.0
            weight_sum = 0.0
            for delta in range(-bandwidth, bandwidth + 1):
                weight = bandwidth + 1 - abs(delta)
                acc += weight * row[(slot + delta) % SLOTS]
                weight_sum += weight
            out[weekday][slot] = acc / weight_sum
    return out


def _method_of_moments(pairs: list[tuple[float, float]]) -> tuple[float, float]:
    """Fit a Beta prior to a set of observed rates. Returns (a0, b0).

    The zero-variance case matters more than it looks. When every position in a group has
    the same rate -- typically 0, at 03:00 on a Tuesday -- the moment estimator divides by
    zero. The old fallback was Beta(2, 2), a prior centred on 50%, which turned "nobody
    ever staffs this slot" into a 23% forecast. Agreement among positions is *information*,
    so the limit is a strong prior at the observed mean, not a weak one in the middle.
    """
    rates = [k / n for k, n in pairs if n > 0]
    if len(rates) < 3:
        return (1.0, 1.0)
    mean = sum(rates) / len(rates)
    var = sum((rate - mean) ** 2 for rate in rates) / len(rates)
    if var <= 1e-9:
        strength = 20.0
    else:
        strength = mean * (1.0 - mean) / var - 1.0
    strength = max(0.5, min(60.0, strength))
    return (max(0.5, mean * strength), max(0.5, (1.0 - mean) * strength))


class CoverageModel:
    """A hierarchical Beta-Binomial schedule model."""

    def __init__(
        self,
        *,
        posteriors: dict[str, tuple[list[list[float]], list[list[float]]]],
        prior_sums: dict[str, list[list[float]]],
        global_slot: list[tuple[float, float]],
        global_prior: tuple[float, float],
        reference_date: date,
        params: ModelParams,
        positions: list[str],
        observations: int,
    ) -> None:
        self._posteriors = posteriors
        self._prior_sums = prior_sums
        self._global_slot = global_slot
        self._global_prior = global_prior
        self.reference_date = reference_date
        self.params = params
        self.positions = positions
        self.observations = observations

    # ---- fitting -------------------------------------------------------

    @classmethod
    def fit(
        cls,
        matrix: PresenceMatrix,
        *,
        reference_date: date,
        params: ModelParams | None = None,
    ) -> CoverageModel:
        params = params or ModelParams()
        raw: dict[str, tuple[list[list[float]], list[list[float]]]] = {}

        for position, rows in matrix.days.items():
            k = [[0.0] * SLOTS for _ in range(WEEKDAYS)]
            n = [[0.0] * SLOTS for _ in range(WEEKDAYS)]
            for day, slot, online in rows:
                if not 0 <= slot < SLOTS:
                    continue
                age = max(0, (reference_date - day).days)
                weight = 0.5 ** (age / params.half_life_days)
                weekday = day.weekday()
                n[weekday][slot] += weight
                if online:
                    k[weekday][slot] += weight
            raw[position] = (k, n)

        # --- priors, per hour of day (and per weekday where the data allows) ---
        # A position's 24-hour average would give a 6-hour tower a prior of 0.25 for its
        # busy evening slots. And a slot-only prior still assumes all seven days are alike,
        # which is false: a weekday-only position is not "30% likely" on Saturday, it is
        # absent. So the prior is as local as the data supports: (group, weekday, slot),
        # then (group, slot), then (all, slot), then a flat Beta(1,1).
        def _table_all_days(
            items: list[tuple[list[list[float]], list[list[float]]]],
        ) -> list[tuple[float, float]]:
            table: list[tuple[float, float]] = []
            for slot in range(SLOTS):
                pairs = [
                    (
                        sum(k[weekday][slot] for weekday in range(WEEKDAYS)),
                        sum(n[weekday][slot] for weekday in range(WEEKDAYS)),
                    )
                    for k, n in items
                ]
                table.append(_method_of_moments([p for p in pairs if p[1] > 0]))
            return table

        def _table_for_weekday(
            items: list[tuple[list[list[float]], list[list[float]]]],
            weekday: int,
        ) -> list[tuple[float, float]]:
            table: list[tuple[float, float]] = []
            for slot in range(SLOTS):
                pairs = [(k[weekday][slot], n[weekday][slot]) for k, n in items]
                table.append(_method_of_moments([p for p in pairs if p[1] > 0]))
            return table

        global_slot = _table_all_days(list(raw.values()))
        by_group: dict[str, list[tuple[list[list[float]], list[list[float]]]]] = defaultdict(list)
        for position, counts in raw.items():
            by_group[_group_of(position)].append(counts)

        group_slot: dict[str, list[tuple[float, float]]] = {}
        group_weekday: dict[tuple[str, int], list[tuple[float, float]]] = {}
        for group, members in by_group.items():
            if len(members) >= 3:
                group_slot[group] = _table_all_days(members)
                for weekday in range(WEEKDAYS):
                    group_weekday[(group, weekday)] = _table_for_weekday(members, weekday)

        def _prior_at(group: str, weekday: int, slot: int) -> tuple[float, float]:
            table = group_weekday.get((group, weekday))
            if table is None:
                table = group_slot.get(group)
            if table is None:
                table = global_slot
            return table[slot]

        global_prior = _method_of_moments(
            [
                (sum(sum(row) for row in k), sum(sum(row) for row in n))
                for k, n in raw.values()
            ]
        )

        # --- posteriors ---
        posteriors: dict[str, tuple[list[list[float]], list[list[float]]]] = {}
        prior_sums: dict[str, list[list[float]]] = {}
        for position, (k, n) in raw.items():
            group = _group_of(position)
            smoothed_k = _smooth(k, params.slot_bandwidth)
            smoothed_n = _smooth(n, params.slot_bandwidth)
            total_k = sum(sum(row) for row in smoothed_k)
            total_n = sum(sum(row) for row in smoothed_n)
            pooled = (total_k / total_n) if total_n > 0 else 0.5

            # Weekday pooling borrows across days *for the same slot*: a Tuesday 18:00
            # estimate leans on the average 18:00, not on the 24-hour average.
            column_k = [
                sum(smoothed_k[weekday][slot] for weekday in range(WEEKDAYS))
                for slot in range(SLOTS)
            ]
            column_n = [
                sum(smoothed_n[weekday][slot] for weekday in range(WEEKDAYS))
                for slot in range(SLOTS)
            ]
            pooled_slot = [(k / n) if n > 0 else pooled for k, n in zip(column_k, column_n)]

            alpha = [[0.0] * SLOTS for _ in range(WEEKDAYS)]
            beta = [[0.0] * SLOTS for _ in range(WEEKDAYS)]
            sums = [[0.0] * SLOTS for _ in range(WEEKDAYS)]
            for weekday in range(WEEKDAYS):
                for slot in range(SLOTS):
                    a0, b0 = _prior_at(group, weekday, slot)
                    k_cell = smoothed_k[weekday][slot]
                    n_cell = smoothed_n[weekday][slot]
                    target = pooled_slot[slot]
                    p_cell = (k_cell + params.weekday_pool * target) / (
                        n_cell + params.weekday_pool
                    )
                    n_eff = n_cell + params.weekday_pool
                    alpha[weekday][slot] = a0 + p_cell * n_eff
                    beta[weekday][slot] = b0 + (1.0 - p_cell) * n_eff
                    sums[weekday][slot] = a0 + b0
            posteriors[position] = (alpha, beta)
            prior_sums[position] = sums

        return cls(
            posteriors=posteriors,
            prior_sums=prior_sums,
            global_slot=global_slot,
            global_prior=global_prior,
            reference_date=reference_date,
            params=params,
            positions=sorted(matrix.days),
            observations=matrix.observations(),
        )

    # ---- prediction ----------------------------------------------------

    def beta_params(self, position_key: str, weekday: int, slot: int) -> tuple[float, float]:
        """Posterior Beta parameters. The predictive check downstream needs these."""
        weekday %= WEEKDAYS
        slot %= SLOTS
        if position_key in self._posteriors:
            alpha, beta = self._posteriors[position_key]
            return alpha[weekday][slot], beta[weekday][slot]
        return self._global_slot[slot] if self._global_slot else self._global_prior

    def predict(
        self, position_key: str, weekday: int, slot: int, *, with_interval: bool = True
    ) -> Forecast:
        weekday %= WEEKDAYS
        slot %= SLOTS
        a, b = self.beta_params(position_key, weekday, slot)
        total = a + b
        p = a / total if total > 0 else 0.5

        if position_key in self._prior_sums:
            n_eff = max(0.0, total - self._prior_sums[position_key][weekday][slot])
        else:
            n_eff = 0.0

        if with_interval:
            lo = beta_ppf(0.05, a, b)
            hi = beta_ppf(0.95, a, b)
        else:
            lo = hi = p
        return Forecast(
            position_key=position_key,
            weekday=weekday,
            slot=slot,
            p=p,
            p_lo=lo,
            p_hi=hi,
            n_eff=n_eff,
        )

    def predict_week(self, position_key: str) -> list[Forecast]:
        return [
            self.predict(position_key, weekday, slot, with_interval=False)
            for weekday in range(WEEKDAYS)
            for slot in range(SLOTS)
        ]

    def mean_probability(self, position_key: str) -> float:
        values = [forecast.p for forecast in self.predict_week(position_key)]
        return sum(values) / len(values) if values else 0.0


# ------------------------------------------------------------------ calibration


@dataclass(slots=True)
class Calibrator:
    """Isotonic (PAVA) recalibration of raw probabilities."""

    knots: list[tuple[float, float]]

    def apply(self, p: float) -> float:
        if not self.knots:
            return p
        if p <= self.knots[0][0]:
            return self.knots[0][1]
        if p >= self.knots[-1][0]:
            return self.knots[-1][1]
        for (x0, y0), (x1, y1) in zip(self.knots, self.knots[1:]):
            if x0 <= p <= x1:
                if x1 == x0:
                    return y1
                weight = (p - x0) / (x1 - x0)
                return y0 + weight * (y1 - y0)
        return p

    @classmethod
    def fit(cls, samples: list[tuple[float, int]], *, bins: int = 20) -> Calibrator:
        if not samples:
            return cls(knots=[])
        ordered = sorted(samples, key=lambda item: item[0])
        chunk = max(1, len(ordered) // bins)
        points: list[tuple[float, float]] = []
        for start in range(0, len(ordered), chunk):
            window = ordered[start : start + chunk]
            if not window:
                continue
            mean_p = sum(p for p, _ in window) / len(window)
            observed = sum(y for _, y in window) / len(window)
            points.append((mean_p, observed))
        if len(points) < 2:
            return cls(knots=[])
        blocks: list[list[float]] = []
        for x, y in points:
            blocks.append([x, y, 1.0])
            while len(blocks) > 1 and blocks[-2][1] > blocks[-1][1]:
                bx, by, bw = blocks.pop()
                ax, ay, aw = blocks.pop()
                weight = aw + bw
                blocks.append([(ax * aw + bx * bw) / weight, (ay * aw + by * bw) / weight, weight])
        return cls(knots=[(b[0], b[1]) for b in blocks])
