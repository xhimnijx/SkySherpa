// Live anchoring probe: with a controller online at EGTT and a tower at EGLL, the
// current-hour probabilities must rise, the terminal layers the tower covers must rise,
// the layer it does not cover must not, and the effect must vanish two hours out.

import { readFile } from "node:fs/promises";
import { createPlanner } from "../web/core.js";

const dir = process.argv[2] ?? "dist/data";
const json = async (name) => JSON.parse(await readFile(`${dir}/${name}`, "utf8"));
const bin = async (name) => new Uint8Array(await readFile(`${dir}/${name}`));

const bundle = {
  meta: await json("meta.json"),
  firs: await json("firs.json"),
  airports: await json("airports.json"),
  pairs: await json("pairs.json"),
  aircraft: await json("aircraft.json"),
  polygons: await json("polygons.json"),
  positions: await json("positions.json"),
  firCtr: await bin("fir_ctr.bin"),
  firFss: await bin("fir_fss.bin"),
  airportLayers: await bin("airport_layers.bin"),
  firGrid: new Uint16Array((await bin("fir_grid.bin")).buffer),
};

const planner = createPlanner(bundle);
const now = new Date();
const threeHoursLater = new Date(now.getTime() + 3 * 3600 * 1000);
const egtt = bundle.firs.findIndex((entry) => entry.code === "EGTT") + 1;

const before = planner.firProbability(egtt, planner.hourIndex(now), now);
const baseLater = planner.firProbability(
  egtt,
  planner.hourIndex(threeHoursLater),
  threeHoursLater,
);
const baseLayers = planner.layersAt("EGLL", planner.hourIndex(now), null);

planner.setLive({
  at: now,
  positions: [
    { key: "EGTT_CTR", facility: 6 },
    { key: "EGLL_TWR", facility: 4 },
  ],
});

const after = planner.firProbability(egtt, planner.hourIndex(now), now);
const later = planner.firProbability(egtt, planner.hourIndex(threeHoursLater), threeHoursLater);
const liveLayers = planner.layersAt("EGLL", planner.hourIndex(now), now);

console.log(
  JSON.stringify({
    has_live: planner.hasLive(),
    fir_before: Number(before.toFixed(4)),
    fir_after: Number(after.toFixed(4)),
    fir_base_three_hours_later: Number(baseLater.toFixed(4)),
    fir_live_three_hours_later: Number(later.toFixed(4)),
    tower_before: Number(baseLayers.twr.toFixed(4)),
    tower_after: Number(liveLayers.twr.toFixed(4)),
    approach_before: Number(baseLayers.app.toFixed(4)),
    approach_after: Number(liveLayers.app.toFixed(4)),
  }),
);
