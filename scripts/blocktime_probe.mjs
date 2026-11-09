// Prints JS block times for a set of airframes, so pytest can prove the browser port
// agrees with skysherpa.aircraft.estimate_block_minutes.
// Usage: node scripts/blocktime_probe.mjs <aircraft.json>

import { readFile } from "node:fs/promises";
import { estimateBlockMinutes } from "../web/core.js";

const path = process.argv[2];
const aircraft = JSON.parse(await readFile(path, "utf8"));
const distances = [50, 150, 300, 600, 1000, 2000];
const out = {};
for (const airframe of aircraft) {
  out[airframe.icao] = distances.map((nm) => estimateBlockMinutes(airframe, nm));
}
console.log(JSON.stringify({ distances, block_times: out }));
