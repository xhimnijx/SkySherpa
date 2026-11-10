"""The doctor's validators: how an API shape change is noticed.

Every check fetches something and then decides whether the payload still looks like what
the code expects. That decision logic is the part worth testing offline: if VATSIM renames
a field, the check must say so in one line rather than letting ingest fail later with a
confusing error.
"""

from __future__ import annotations

from datetime import UTC, datetime

from skysherpa import doctor


def test_transceivers_accepts_the_documented_shape() -> None:
    ok, detail = doctor._transceivers(
        [
            {"callsign": "EGLL_TWR", "transceivers": [{}, {}]},
            {"callsign": "EDDF_TWR", "transceivers": [{}]},
        ]
    )
    assert ok is True
    assert "2 clients" in detail
    assert "3 radio sites" in detail


def test_transceivers_rejects_an_empty_or_renamed_payload() -> None:
    assert doctor._transceivers([])[0] is False
    assert doctor._transceivers({"clients": []})[0] is False
    ok, detail = doctor._transceivers([{"callsign": "EGLL_TWR"}])
    assert ok is False
    assert "schema changed" in detail


def test_atc_online_accepts_a_list_only() -> None:
    assert doctor._atc_online([])[0] is True
    assert doctor._atc_online([{}, {}])[1].startswith("2 online")
    assert doctor._atc_online({"online": 3})[0] is False


def test_vatspy_needs_both_sections_and_enough_records() -> None:
    good = "[Airports]\n" + "\n".join("EGLL|London|51.47|-0.46|25|0" for _ in range(6000))
    good += "\n[FIRs]\n" + "\n".join("EGTT|London|LON" for _ in range(600))
    assert doctor._vatspy(good)[0] is True

    assert doctor._vatspy("[Airports]\nEGLL|London") != (True, doctor._vatspy("x")[1])
    ok, detail = doctor._vatspy("[Airports]\nEGLL|London")
    assert ok is False
    assert "sections" in detail

    thin = "[Airports]\n[FIRs]\n" + "EGLL|London|51.4|-0.4|25|0\n" * 10
    ok, detail = doctor._vatspy(thin)
    assert ok is False
    assert "pipe-delimited" in detail


def test_boundaries_needs_a_feature_collection_of_enough_features() -> None:
    features = [{"type": "Feature"}] * (doctor.MIN_BOUNDARIES + 5)
    ok, detail = doctor._boundaries({"type": "FeatureCollection", "features": features})
    assert ok is True
    assert "polygons" in detail

    assert doctor._boundaries({"type": "Feature"})[0] is False
    ok, detail = doctor._boundaries({"type": "FeatureCollection", "features": []})
    assert ok is False
    assert "boundary features" in detail


def test_weather_and_taf_notice_renamed_fields() -> None:
    assert (
        doctor._weather([{"rawOb": "EGLL 101220Z", "fltCat": "VFR", "icaoId": "EGLL"}])[0] is True
    )
    ok, detail = doctor._weather([{"metar": "renamed"}])
    assert ok is False and "rawOb" in detail
    assert doctor._weather([])[0] is False

    assert doctor._taf([{"fcsts": [{}, {}]}])[1].startswith("1 station")
    ok, detail = doctor._taf([{"forecasts": []}])
    assert ok is False and "fcsts" in detail
    assert doctor._taf([])[0] is False


def test_timed_turns_an_exception_into_a_failed_check() -> None:
    def boom() -> tuple[bool, str]:
        raise RuntimeError("connection reset")

    check = doctor._timed("feed", boom)
    assert check.ok is False
    assert "RuntimeError" in check.detail
    assert check.critical is True

    slow = doctor._timed("optional", boom, critical=False)
    assert slow.critical is False


def test_render_marks_critical_and_optional_failures_differently() -> None:
    checks = [
        doctor.Check(name="feed", ok=True, detail="fine"),
        doctor.Check(name="optional", ok=False, detail="timeout", critical=False),
    ]
    text = doctor.render(checks, now=datetime(2026, 11, 10, 12, 0, tzinfo=UTC))
    assert "OK" in text
    assert "WARN" in text
    assert "2026-11-10T12:00:00Z" in text


def test_exit_code_only_fails_on_critical_checks() -> None:
    healthy = [doctor.Check(name="feed", ok=True, detail="fine")]
    optional = [doctor.Check(name="metar", ok=False, detail="timeout", critical=False)]
    critical = [doctor.Check(name="feed", ok=False, detail="403", critical=True)]

    assert doctor.exit_code(healthy) == 0
    assert doctor.exit_code(optional) == 0
    assert doctor.exit_code(critical) == 1
    assert doctor.exit_code(healthy + optional + critical) == 1
