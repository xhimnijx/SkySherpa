// End-to-end check of the browser planner against a published bundle.
// Usage: node scripts/verify_web.mjs [dist/data]

import { readFile } from "node:fs/promises";
import { createPlanner, estimateBlockMinutes, explain } from "../web/core.js";

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
  firProb: await bin("fir_prob.bin"),
  airportProb: await bin("airport_prob.bin"),
  firGrid: new Uint16Array((await bin("fir_grid.bin")).buffer),
};

const planner = createPlanner(bundle);
const hourArg = process.argv.find((item) => item.startsWith("--hour="));
let departAt = new Date(Date.now() + 10 * 60000);
if (hourArg) {
  departAt = new Date();
  departAt.setUTCHours(Number(hourArg.split("=")[1]), 0, 0, 0);
}
const outcome = planner.plan({
  departAt,
  blockMinutes: 150,
  tolerance: 45,
  aircraft: "A20N",
  limit: 8,
});

const hour = planner.hourIndex(departAt);
const staffed = planner.firs.filter((_, index) => planner.firProbability(index + 1, hour) > 0.5);

const blockTimes = {};
for (const icao of ["A20N", "B738", "C172", "DH8D", "A388"]) {
  const spec = planner.aircraftByIcao.get(icao);
  if (spec) blockTimes[icao] = [200, 600, 1000].map((nm) => estimateBlockMinutes(spec, nm));
}

console.log(
  JSON.stringify(
    {
      meta: bundle.meta,
      bundle: {
        firs: bundle.firs.length,
        airports: bundle.airports.length,
        pairs: bundle.pairs.length,
        polygons: bundle.polygons.length,
        aircraft: bundle.aircraft.length,
      },
      evaluated: outcome.evaluated,
      matched: outcome.matched,
      elapsed_ms: Number(outcome.elapsedMs.toFixed(1)),
      staffed_firs_now: staffed.length,
      block_times: blockTimes,
      top: outcome.results.map((result) => ({
        route: `${result.dep}-${result.arr}`,
        block_min: result.block,
        distance_nm: result.distance,
        atc_pct: Number((result.atc * 100).toFixed(1)),
        enroute_pct: Number((result.enroute * 100).toFixed(1)),
        utility: Number(result.utility.toFixed(4)),
        why: explain(result),
      })),
    },
    null,
    2,
  ),
);
