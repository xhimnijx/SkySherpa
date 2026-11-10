"""Capture one real feed snapshot, scrubbed, as a test fixture.

Real payloads contain quirks no hand-written fixture has: seven-digit timestamps, altitude
as a string, sentinel frequencies, controllers with empty callsigns, pilots on the ground.
They also contain member names and CIDs, which must not be committed -- ADR-0003 exists
precisely so this project never stores a real CID. So: keep the shapes, replace the
identities.
"""

import json
import pathlib
import sys

sys.path.insert(0, "src")

from skysherpa.config import Settings  # noqa: E402
from skysherpa.feed import ENDPOINTS  # noqa: E402
from skysherpa import net  # noqa: E402

OUT = pathlib.Path("tests/data/feed_sample.json")
KEEP_PILOTS = 30
KEEP_CONTROLLERS = 25
KEEP_ATIS = 6
KEEP_PREFILES = 6


def scrub_plan(plan: dict | None) -> dict | None:
    if not plan:
        return plan
    return {
        "departure": plan.get("departure", ""),
        "arrival": plan.get("arrival", ""),
        "alternate": plan.get("alternate", ""),
        "aircraft_short": plan.get("aircraft_short", ""),
        "aircraft": plan.get("aircraft", ""),
        "flight_rules": plan.get("flight_rules", ""),
        "cruise_tas": plan.get("cruise_tas", ""),
        "altitude": plan.get("altitude", ""),
        "deptime": plan.get("deptime", ""),
        "enroute_time": plan.get("enroute_time", ""),
        "route": plan.get("route", ""),
        "remarks": "",
    }


def main() -> int:
    settings = Settings.load()
    payload, response = net.get_json(ENDPOINTS["feed"], settings)
    if payload is None:
        print(f"feed failed: {response.error}")
        return 1

    general = dict(payload.get("general") or {})
    general.pop("update_timestamp", None)  # re-stamped so the fixture never looks stale

    pilots = []
    for index, pilot in enumerate((payload.get("pilots") or [])[:KEEP_PILOTS]):
        pilots.append(
            {
                "cid": index + 1,
                "callsign": pilot.get("callsign"),
                "latitude": pilot.get("latitude"),
                "longitude": pilot.get("longitude"),
                "altitude": pilot.get("altitude"),
                "groundspeed": pilot.get("groundspeed"),
                "heading": pilot.get("heading"),
                "transponder": pilot.get("transponder"),
                "logon_time": pilot.get("logon_time"),
                "last_updated": pilot.get("last_updated"),
                "flight_plan": scrub_plan(pilot.get("flight_plan")),
            }
        )

    controllers = []
    for index, controller in enumerate((payload.get("controllers") or [])[:KEEP_CONTROLLERS]):
        controllers.append(
            {
                "cid": 1000 + index,
                "callsign": controller.get("callsign"),
                "frequency": controller.get("frequency"),
                "facility": controller.get("facility"),
                "rating": controller.get("rating"),
                "logon_time": controller.get("logon_time"),
            }
        )

    atis = []
    for index, station in enumerate((payload.get("atis") or [])[:KEEP_ATIS]):
        atis.append(
            {
                "cid": 2000 + index,
                "callsign": station.get("callsign"),
                "frequency": station.get("frequency"),
                "atis_code": station.get("atis_code"),
                "text_atis": (station.get("text_atis") or [])[:4],
                "logon_time": station.get("logon_time"),
            }
        )

    prefiles = []
    for index, prefile in enumerate((payload.get("prefiles") or [])[:KEEP_PREFILES]):
        prefiles.append(
            {
                "cid": 3000 + index,
                "callsign": prefile.get("callsign"),
                "flight_plan": scrub_plan(prefile.get("flight_plan")),
            }
        )

    fixture = {
        "general": general,
        "pilots": pilots,
        "controllers": controllers,
        "atis": atis,
        "prefiles": prefiles,
        "servers": [],
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(fixture, indent=1) + "\n", encoding="utf-8")
    print(
        f"wrote {OUT} ({OUT.stat().st_size / 1024:.0f} KB): "
        f"{len(pilots)} pilots, {len(controllers)} controllers, {len(atis)} atis, "
        f"{len(prefiles)} prefiles"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
