// Prints preset/layer scoring for a handful of synthetic cases, so pytest can prove the
// weighting and the renormalisation rules behave as documented.

import { PRESETS, describeStack, layerScore } from "../web/core.js";

const full = { del: 1, gnd: 1, twr: 1, app: 1 };
const towerOnly = { del: 0, gnd: 0, twr: 1, app: 0 };
const TWR_BIT = 1 << 2;

console.log(
  JSON.stringify({
    airline_full: layerScore(full, PRESETS.airline.departure),
    airline_tower_only: layerScore(towerOnly, PRESETS.airline.departure),
    ga_tower_only_renormalised: layerScore(towerOnly, PRESETS.ga.departure, TWR_BIT, true),
    ga_tower_only_raw: layerScore(towerOnly, PRESETS.ga.departure, TWR_BIT, false),
    arrival_full: layerScore(full, PRESETS.airline.arrival),
    weights_sum: Object.values(PRESETS.airline.weights).reduce((a, b) => a + b, 0),
    departure_weights_sum: Object.values(PRESETS.airline.departure).reduce((a, b) => a + b, 0),
    arrival_weights_sum: Object.values(PRESETS.airline.arrival).reduce((a, b) => a + b, 0),
    stack_text: describeStack({ del: 1, gnd: 1, twr: 1, app: 0 }, "departure"),
    empty_stack_text: describeStack({ del: 0, gnd: 0, twr: 0, app: 0 }, "arrival"),
  }),
);
