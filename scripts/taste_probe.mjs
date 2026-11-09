// Preference-learning probe: does the Bradley-Terry update move the weights the way the
// choices say they should, and does it stay anchored to the preset?

import { PRESETS, fitPreferenceWeights, normalizeWeights } from "../web/core.js";

const prior = PRESETS.airline.weights;

const highAtc = { atc: 0.9, duration: 0.4, fit: 0.9, popularity: 0.5 };
const lowAtc = { atc: 0.2, duration: 0.9, fit: 0.9, popularity: 0.5 };

const atcChoices = Array.from({ length: 10 }, () => ({ a: highAtc, b: lowAtc, winner: "a" }));
const timeChoices = Array.from({ length: 10 }, () => ({ a: highAtc, b: lowAtc, winner: "b" }));

const atcLover = fitPreferenceWeights(atcChoices, prior);
const timeLover = fitPreferenceWeights(timeChoices, prior);
const oneChoice = fitPreferenceWeights([atcChoices[0]], prior);

const sum = (weights) => Object.values(weights).reduce((total, value) => total + value, 0);

console.log(
  JSON.stringify({
    prior: normalizeWeights(prior),
    no_choices: fitPreferenceWeights([], prior),
    atc_lover: atcLover,
    time_lover: timeLover,
    one_choice: oneChoice,
    sums: [sum(atcLover), sum(timeLover), sum(oneChoice)],
    non_negative: Object.values(atcLover).every((value) => value >= 0),
  }),
);
