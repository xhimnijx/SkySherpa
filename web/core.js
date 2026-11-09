// SkySherpa core: pure planning maths. No DOM, no fetch, no framework.
//
// The browser does not run the forecasting model. It receives a probability grid per FIR
// and per airport (168 hours of the week, one byte each) and does arithmetic on it:
// sample the great circle, look up which FIR each sample falls in, read the probability
// for that hour, aggregate, score, sort. That is the whole reason a worldwide plan takes
// tens of milliseconds and needs no server.

export const HOURS_PER_WEEK = 168;
const EARTH_RADIUS_NM = 3440.065;
const RAD = Math.PI / 180;

// Terminal layers, shallow to deep. Each value is P(that service is available), already
// including top-down cover from above in the export.
export const LAYERS = ["del", "gnd", "twr", "app"];
const LAYER_NAMES = { del: "delivery", gnd: "ground", twr: "tower", app: "approach" };

// What each layer is worth on departure and on arrival. Approach and tower are the
// moments where a human actually changes your flight; delivery and ground are the
// polish that makes a big airport feel alive. Arrival has no delivery: you are not
// asking for a clearance at the gate after you land.
export const DEPARTURE_WEIGHTS = { del: 0.15, gnd: 0.15, twr: 0.35, app: 0.35 };
export const ARRIVAL_WEIGHTS = { del: 0.0, gnd: 0.15, twr: 0.35, app: 0.5 };

// Presets. `renormalise` divides the terminal score by the weight of the layers an
// airport has ever staffed, which is right for GA (a grass strip should not be punished
// for having no delivery position) and wrong for airline ops (depth is the point).
// When the visitor has named no aircraft, these are the classes we assume they might fly.
// Military and vintage types are deliberate choices -- a Concorde or an F-16 is not a
// sensible default for "where should I fly", and both win any speed-based ranking outright.
const ANY_CLASSES = new Set(["airliner", "regional", "general", "business", "helicopter"]);
//: Beyond this, extra speed stops earning credit: a 520 kt business jet is not a better
//: answer than a 450 kt airliner merely because it is faster.
const COMFORTABLE_TAS = 430;

export const PRESETS = {
  // The default. A first-time visitor has told us nothing yet, so nothing is assumed:
  // every airframe in the catalogue, airline-style service weights, no class filter.
  any: {
    label: "Any flying",
    weights: { atc: 0.5, duration: 0.2, fit: 0.15, popularity: 0.15 },
    departure: DEPARTURE_WEIGHTS,
    arrival: ARRIVAL_WEIGHTS,
    renormalise: false,
    classes: null,
  },
  airline: {
    label: "Airline ops",
    weights: { atc: 0.6, duration: 0.2, fit: 0.1, popularity: 0.1 },
    departure: DEPARTURE_WEIGHTS,
    arrival: ARRIVAL_WEIGHTS,
    renormalise: false,
    classes: ["airliner", "regional"],
  },
  ga: {
    label: "GA / sightseeing",
    weights: { atc: 0.45, duration: 0.25, fit: 0.15, popularity: 0.15 },
    departure: { del: 0, gnd: 0, twr: 0.5, app: 0.5 },
    arrival: { del: 0, gnd: 0, twr: 0.5, app: 0.5 },
    renormalise: true,
    classes: ["general", "helicopter"],
  },
  max: {
    label: "Maximum ATC",
    weights: { atc: 0.85, duration: 0.05, fit: 0.05, popularity: 0.05 },
    departure: DEPARTURE_WEIGHTS,
    arrival: ARRIVAL_WEIGHTS,
    renormalise: false,
    classes: null,
  },
};

export function layerScore(layers, weights, mask = 0, renormalise = false) {
  let weightSum = 0;
  let total = 0;
  for (let index = 0; index < LAYERS.length; index += 1) {
    const layer = LAYERS[index];
    const weight = weights[layer] || 0;
    if (weight <= 0) continue;
    if (renormalise && mask && !(mask & (1 << index))) continue;
    weightSum += weight;
    total += weight * (layers[layer] || 0);
  }
  return weightSum > 0 ? total / weightSum : 0;
}

// ---------------------------------------------------------------- preferences

// The features a pairwise choice can teach, named exactly as the utility weights are, so
// a learned vector can be dropped straight into `plan()` without a translation table.
// Deliberately the same four numbers the ranking already uses: a preference model that
// invented new features would be unexplainable, and an unexplainable ranking is what this
// project exists to avoid.
export const FEATURES = ["atc", "duration", "fit", "popularity"];

export function normalizeWeights(weights) {
  let total = 0;
  for (const key of FEATURES) total += Math.max(0, weights[key] || 0);
  if (total <= 0) return { ...weights };
  const out = {};
  for (const key of FEATURES) out[key] = Math.max(0, weights[key] || 0) / total;
  return out;
}

/**
 * Bradley-Terry preference learning on feature differences.
 *
 * The preset is treated as `priorWeight` pseudo-comparisons and the learned weights are
 * blended with it by count: ten choices move the ranking noticeably, one choice barely
 * moves it at all. An earlier version relied on L2 alone and a single click shifted the
 * ATC weight by 0.31 -- which is what "overfitting on one data point" looks like when it
 * happens to a person.
 */
export function fitPreferenceWeights(choices, prior, options = {}) {
  const { epochs = 200, learningRate = 0.5, l2 = 0.001, priorWeight = 5 } = options;
  const priorVector = FEATURES.map((key) => Math.max(0, prior[key] || 0));
  if (!choices.length) return normalizeWeights(prior);

  const weights = priorVector.slice();
  for (let epoch = 0; epoch < epochs; epoch += 1) {
    const gradient = new Array(FEATURES.length).fill(0);
    for (const choice of choices) {
      const difference = FEATURES.map(
        (key, index) => (choice.a[key] ?? 0) - (choice.b[key] ?? 0),
      );
      let dot = 0;
      for (let index = 0; index < weights.length; index += 1) dot += weights[index] * difference[index];
      const probability = 1 / (1 + Math.exp(-Math.max(-30, Math.min(30, dot))));
      const error = (choice.winner === "a" ? 1 : 0) - probability;
      for (let index = 0; index < gradient.length; index += 1) {
        gradient[index] += error * difference[index];
      }
    }
    for (let index = 0; index < weights.length; index += 1) {
      const pull = l2 * (priorVector[index] - weights[index]);
      const step = learningRate * (gradient[index] / choices.length + pull);
      // Clamp into [0, 1]: an unbounded ascent lets one comparison push a weight past
      // 1.0, which then survives the prior blend and looks like confidence.
      weights[index] = Math.min(1, Math.max(0, weights[index] + step));
    }
  }

  const fitted = normalizeWeights(
    Object.fromEntries(FEATURES.map((key, index) => [key, weights[index]])),
  );
  const priorNormalised = normalizeWeights(prior);
  const count = choices.length;
  const blended = Object.fromEntries(
    FEATURES.map((key) => [
      key,
      (count * fitted[key] + priorWeight * priorNormalised[key]) / (count + priorWeight),
    ]),
  );
  return normalizeWeights(blended);
}

// En-route: a centre controls, a flight service station informs. FSS is worth something
// when nobody is controlling, and nothing extra when a centre is already on.
const FSS_VALUE = 0.35;

export function terminalScore(layers, weights) {
  let total = 0;
  for (const layer of LAYERS) total += (weights[layer] || 0) * (layers[layer] || 0);
  return total;
}

export function describeStack(layers, label) {
  if (!layers) return "";
  const present = LAYERS.filter((layer) => layers[layer] >= 0.5).map((layer) => LAYER_NAMES[layer]);
  if (present.length === 4) return `${label}: full stack`;
  if (present.length === 0) return `${label}: no ATC`;
  return `${label}: ${present.join(" + ")}`;
}

export function haversineNm(a, b) {
  const [lat1, lon1] = a;
  const [lat2, lon2] = b;
  const dLat = (lat2 - lat1) * RAD;
  const dLon = (lon2 - lon1) * RAD;
  const h =
    Math.sin(dLat / 2) ** 2 +
    Math.cos(lat1 * RAD) * Math.cos(lat2 * RAD) * Math.sin(dLon / 2) ** 2;
  return 2 * EARTH_RADIUS_NM * Math.asin(Math.min(1, Math.sqrt(h)));
}

export function greatCircle(a, b, fraction) {
  const toVec = ([lat, lon]) => {
    const phi = lat * RAD;
    const lambda = lon * RAD;
    return [Math.cos(phi) * Math.cos(lambda), Math.cos(phi) * Math.sin(lambda), Math.sin(phi)];
  };
  const v0 = toVec(a);
  const v1 = toVec(b);
  let dot = v0[0] * v1[0] + v0[1] * v1[1] + v0[2] * v1[2];
  dot = Math.max(-1, Math.min(1, dot));
  const omega = Math.acos(dot);
  if (omega < 1e-9) return [a[0], a[1]];
  const s0 = Math.sin((1 - fraction) * omega) / Math.sin(omega);
  const s1 = Math.sin(fraction * omega) / Math.sin(omega);
  const x = s0 * v0[0] + s1 * v1[0];
  const y = s0 * v0[1] + s1 * v1[1];
  const z = s0 * v0[2] + s1 * v1[2];
  return [Math.atan2(z, Math.hypot(x, y)) / RAD, Math.atan2(y, x) / RAD];
}

// Index into a week that starts on Monday, matching the Python export.
export function hourIndex(date) {
  return ((date.getUTCDay() + 6) % 7) * 24 + date.getUTCHours();
}

// Mirrors skysherpa.aircraft.estimate_block_minutes; kept in parity by a Node probe in CI.
export function estimateBlockMinutes(aircraft, distanceNm, options = {}) {
  const cruiseAlt = Math.min(options.cruiseAltFt ?? aircraft.fl * 100, 60000);
  const ground = (aircraft.taxi ?? 0) + (options.approachMin ?? 3);
  if (distanceNm <= 0) return Math.max(1, Math.round(ground));

  const climbRate = Math.max(100, aircraft.climb);
  const descentRate = Math.max(100, aircraft.descent);
  const climbSpeed = Math.max(110, 0.62 * aircraft.tas);
  const descentSpeed = Math.max(110, 0.8 * aircraft.tas);

  let climbMin = cruiseAlt / climbRate;
  let descentMin = cruiseAlt / descentRate;
  let climbDist = (climbMin / 60) * climbSpeed;
  let descentDist = (descentMin / 60) * descentSpeed;

  const profile = climbDist + descentDist;
  if (profile > 0 && distanceNm < profile) {
    const share = distanceNm / profile;
    climbMin *= share;
    descentMin *= share;
    climbDist *= share;
    descentDist *= share;
  }
  const cruiseDist = Math.max(0, distanceNm - climbDist - descentDist);
  const cruiseMin = (cruiseDist / Math.max(60, aircraft.tas)) * 60;
  return Math.max(1, Math.round(ground + climbMin + cruiseMin + descentMin));
}

export function createPlanner(bundle) {
  const {
    meta,
    firs,
    firCtr,
    firFss,
    firGrid,
    airports,
    airportLayers,
    positions,
    pairs,
    aircraft,
    firDays = new Uint8Array(0),
    airportDays = new Uint8Array(0),
  } = bundle;
  const width = meta.grid.width;
  const height = meta.grid.height;
  const resolution = meta.grid.resolution;

  const airportByIcao = new Map(airports.map((a) => [a.icao, a]));
  const airportSlot = new Map(airports.map((a, i) => [a.icao, i]));
  const aircraftByIcao = new Map(aircraft.map((a) => [a.icao, a]));
  const firCodeOf = (index) => (index > 0 && index <= firs.length ? firs[index - 1].code : "");
  const firSlot = new Map(firs.map((entry, index) => [entry.code, index + 1]));

  // Live anchoring. The historical model answers "who is usually online"; the feed
  // answers "who is online right now". For a departure in the next hour the second is
  // strictly better, so it is blended in with a two-hour decay: certainty at the moment
  // of the snapshot, nothing by the time the flight is well clear of it.
  const positionByKey = new Map();
  const positionByStem = new Map();
  for (const entry of positions || []) {
    positionByKey.set(entry.k, entry);
    const stem = entry.k.replace(/_[A-Z]{2,4}$/, "");
    if (stem && !positionByStem.has(stem)) positionByStem.set(stem, entry);
  }
  const LIVE_LAYER_MASK = { DEL: 0b0001, GND: 0b0011, TWR: 0b0111, APP: 0b1111, CTR: 0b1111, FSS: 0 };
  const FACILITY_BY_CODE = { 1: "FSS", 2: "DEL", 3: "GND", 4: "TWR", 5: "APP", 6: "CTR" };
  let liveState = null;

  function setLive(payload) {
    if (!payload || !Array.isArray(payload.positions)) {
      liveState = null;
      return;
    }
    const at = payload.at instanceof Date ? payload.at : new Date(payload.at);
    const staffedFirs = new Set();
    const staffedAirports = new Map();
    for (const item of payload.positions) {
      const name = String(item.key || "").toUpperCase();
      if (!name) continue;
      const record = positionByKey.get(name) || positionByStem.get(name.replace(/_[A-Z]{2,4}$/, ""));
      if (!record) continue;
      const facility = FACILITY_BY_CODE[item.facility] || record.t;
      // A live centre working top-down anchors every terminal layer beneath it too.
      if (facility === "CTR" && record.f) {
        const index = firSlot.get(record.f) || firSlot.get(record.f.split("-")[0]);
        if (index) staffedFirs.add(index);
      }
      const mask = LIVE_LAYER_MASK[facility] || 0;
      if (record.a && mask) staffedAirports.set(record.a, (staffedAirports.get(record.a) || 0) | mask);
    }
    liveState = { at: Number.isFinite(at.getTime()) ? at : new Date(), firs: staffedFirs, airports: staffedAirports };
  }

  function liveWeight(when) {
    if (!liveState || !when) return 0;
    const minutes = Math.abs(when.getTime() - liveState.at.getTime()) / 60000;
    if (minutes >= 120) return 0;
    return 1 - minutes / 120;
  }

  function applyLive(base, target, weight) {
    return weight > 0 ? base + (target - base) * weight : base;
  }

  function firIndexAt(lat, lon) {
    const x = Math.floor((lon + 180) / resolution);
    const y = Math.floor((lat + 90) / resolution);
    if (x < 0 || x >= width || y < 0 || y >= height) return 0;
    return firGrid[y * width + x];
  }

  function firCtrProbability(index, hour) {
    if (!index) return 0;
    return firCtr[(index - 1) * HOURS_PER_WEEK + hour] / 255;
  }

  function firFssProbability(index, hour) {
    if (!index) return 0;
    return firFss[(index - 1) * HOURS_PER_WEEK + hour] / 255;
  }

  function firProbability(index, hour, when = null) {
    const control = firCtrProbability(index, hour);
    const service = firFssProbability(index, hour);
    const base = control + FSS_VALUE * service * (1 - control);
    if (when && liveState && liveState.firs.has(index)) {
      return applyLive(base, 0.95, liveWeight(when));
    }
    return base;
  }

  function airportLayer(icao, layerIndex, hour, when = null) {
    const slot = airportSlot.get(icao);
    if (slot === undefined) return 0;
    const offset = (slot * LAYERS.length + layerIndex) * HOURS_PER_WEEK + hour;
    const base = airportLayers[offset] / 255;
    if (when && liveState) {
      const mask = liveState.airports.get(icao) || 0;
      if (mask & (1 << layerIndex)) {
        return applyLive(base, 0.95, liveWeight(when));
      }
    }
    return base;
  }

  function layersAt(icao, hour, when = null) {
    return {
      del: airportLayer(icao, 0, hour, when),
      gnd: airportLayer(icao, 1, hour, when),
      twr: airportLayer(icao, 2, hour, when),
      app: airportLayer(icao, 3, hour, when),
    };
  }

  // How many days of history support the number for this FIR or airport. Zero means the
  // probability is the prior, which is a different kind of claim from an observation.
  function firDayCount(index) {
    return index > 0 && index <= firDays.length ? firDays[index - 1] : 0;
  }

  function airportDayCount(icao) {
    const slot = airportSlot.get(icao);
    return slot === undefined ? 0 : airportDays[slot] || 0;
  }

  function routeProfile(departure, arrival, departAt, aircraftSpec, preset = PRESETS.airline) {
    const distance = haversineNm(
      [departure.lat, departure.lon],
      [arrival.lat, arrival.lon],
    );
    const block = estimateBlockMinutes(aircraftSpec, distance);
    const samples = Math.max(4, Math.ceil(distance / 120));

    let enroute = 0;
    let covered = 0;
    let gapRun = 0;
    let longestGap = 0;
    let evidenceSum = 0;
    let evidenceCount = 0;
    const noteEvidence = (days) => {
      if (days > 0) {
        evidenceSum += days;
        evidenceCount += 1;
      }
    };

    for (let i = 0; i <= samples; i += 1) {
      const fraction = i / samples;
      const point = greatCircle(
        [departure.lat, departure.lon],
        [arrival.lat, arrival.lon],
        fraction,
      );
      const moment = new Date(departAt.getTime() + block * 60000 * fraction);
      const firIndex = firIndexAt(point[0], point[1]);
      const probability = firProbability(firIndex, hourIndex(moment), moment);
      noteEvidence(firDayCount(firIndex));
      enroute += probability;
      if (probability >= 0.5) covered += 1;
      if (probability < 0.25) {
        gapRun += 1;
        longestGap = Math.max(longestGap, gapRun);
      } else {
        gapRun = 0;
      }
    }
    enroute /= samples + 1;
    noteEvidence(airportDayCount(departure.icao));
    noteEvidence(airportDayCount(arrival.icao));
    // Mean depth of the evidence we actually have, not of the airspace we do not: an
    // unobserved FIR should not drag down a route that is otherwise well covered.
    const days = evidenceCount ? Math.round(evidenceSum / evidenceCount) : 0;
    const arrivalMoment = new Date(departAt.getTime() + block * 60000);
    const departureLayers = layersAt(departure.icao, hourIndex(departAt), departAt);
    const arrivalLayers = layersAt(arrival.icao, hourIndex(arrivalMoment), arrivalMoment);
    const departureP = layerScore(
      departureLayers,
      preset.departure,
      departure.stack || 0,
      preset.renormalise,
    );
    const arrivalP = layerScore(
      arrivalLayers,
      preset.arrival,
      arrival.stack || 0,
      preset.renormalise,
    );
    return {
      distance,
      block,
      enroute,
      departureP,
      arrivalP,
      departureLayers,
      arrivalLayers,
      days,
      coveredShare: covered / (samples + 1),
      unicomShare: longestGap / (samples + 1),
    };
  }

  function score(pair, departAt, aircraftSpec, weights, preset, durationWindow, fastestKt) {
    const departure = airportByIcao.get(pair.d);
    const arrival = airportByIcao.get(pair.a);
    if (!departure || !arrival) return null;

    const profile = routeProfile(departure, arrival, departAt, aircraftSpec, preset);
    if (profile.distance > aircraftSpec.range_nm * 0.92) return null;
    // A duration window, not a target: "I have between two and four hours" is the question
    // people actually ask. Inside the window every flight is equally acceptable, which is
    // why durationFit becomes a filter rather than a preference.
    if (durationWindow && (profile.block < durationWindow.min || profile.block > durationWindow.max)) return null;

    const atc = 0.55 * profile.enroute + 0.2 * profile.departureP + 0.25 * profile.arrivalP;
    const durationFit = durationWindow ? 1 : 0.5;
    const popularity = Math.min(1, Math.log1p(pair.n) / Math.log(50));

    // Using the right aeroplane for the sector. A 777 on a 900 nm hop is technically
    // legal and practically absurd; a light aircraft at 90% of its range is the opposite
    // mistake. Both are penalised, the middle of the envelope is not. Speed matters too:
    // when the fleet is "anything", the range term alone picked a Twin Otter for
    // Heathrow-Frankfurt because its range sits near twice the sector.
    const utilisation = profile.distance / Math.max(1, aircraftSpec.range_nm);
    const rangeFit =
      utilisation < 0.2
        ? Math.max(0, utilisation / 0.2)
        : utilisation > 0.75
          ? Math.max(0, (1 - utilisation) / 0.25)
          : 1;
    const fastest = Math.min(fastestKt || COMFORTABLE_TAS, COMFORTABLE_TAS) || 1;
    const speedFit = Math.min(1, (aircraftSpec.tas || 0) / fastest);
    const aircraftFit = rangeFit * (0.25 + 0.75 * speedFit);

    const utility =
      weights.atc * atc +
      weights.duration * durationFit +
      (weights.fit || 0) * aircraftFit +
      weights.popularity * popularity;

    return {
      dep: pair.d,
      arr: pair.a,
      aircraft: aircraftSpec.icao,
      depName: departure.name,
      arrName: arrival.name,
      block: profile.block,
      distance: Math.round(profile.distance),
      enroute: profile.enroute,
      departureP: profile.departureP,
      arrivalP: profile.arrivalP,
      departureLayers: profile.departureLayers,
      arrivalLayers: profile.arrivalLayers,
      days: profile.days,
      atc,
      durationFit,
      aircraftFit,
      popularity,
      utility,
      observed: pair.n,
      coverage: profile.coveredShare,
      unicom: profile.unicomShare,
      departAt: new Date(departAt),
    };
  }

  function plan(query) {
    const started = performance.now();
    const preset = PRESETS[query.preset] || PRESETS.any;
    const weights = { ...preset.weights, ...(query.weights || {}) };

    // No aircraft named means "anything goes": the visitor has not told us what they fly,
    // so every type in the catalogue is a candidate and the best one is picked per route.
    const requested =
      query.aircraft == null
        ? []
        : Array.isArray(query.aircraft)
          ? query.aircraft
          : [query.aircraft];
    let specs = requested
      .map((code) => aircraftByIcao.get(code))
      .filter((spec) => spec !== undefined);
    const flyingAnything = specs.length === 0;
    if (flyingAnything) specs = aircraft.filter((spec) => ANY_CLASSES.has(spec.class));
    // A preset narrows the fleet to what the style implies, but never to nothing: if the
    // user owns only airliners, GA mode still has to produce an answer.
    if (preset.classes) {
      const suited = specs.filter((spec) => preset.classes.includes(spec.class));
      if (suited.length) specs = suited;
    }
    const departAt = query.departAt instanceof Date ? query.departAt : new Date(query.departAt);

    // Duration range. The old single target is still accepted so saved permalinks keep
    // working; new callers pass blockMin/blockMax.
    let durationWindow = null;
    if (Number.isFinite(query.blockMin) || Number.isFinite(query.blockMax)) {
      durationWindow = { min: query.blockMin ?? 0, max: query.blockMax ?? 24 * 60 };
    } else if (Number.isFinite(query.blockMinutes)) {
      const tolerance = query.tolerance ?? 45;
      durationWindow = { min: query.blockMinutes - tolerance, max: query.blockMinutes + tolerance };
    }

    const results = [];
    let evaluated = 0;

    // When the visitor has named no aircraft, scoring every airframe against every route
    // is 195 x 1300 profiles on a real bundle -- half a second per plan, five for the
    // quiet-hour scan. Instead consider the handful whose range is closest to twice the
    // sector (where rangeFit peaks) plus the fastest few, so a jet is always on the table
    // for a long-ish hop instead of losing to a turboprop on range alone.
    function candidates(distance, take) {
      if (!flyingAnything) return specs;
      const eligible = specs.filter((spec) => spec.range_nm * 0.92 >= distance);
      if (!eligible.length) return [];
      const ideal = Math.max(distance * 2, 60);
      const byFit = [...eligible]
        .sort((a, b) => Math.abs(a.range_nm - ideal) - Math.abs(b.range_nm - ideal))
        .slice(0, Math.max(1, take - 3));
      const bySpeed = [...eligible]
        .sort((a, b) => (b.tas || 0) - (a.tas || 0))
        .slice(0, 3);
      const seen = new Set();
      const shortlist = [];
      for (const spec of [...byFit, ...bySpeed]) {
        if (seen.has(spec.icao)) continue;
        seen.add(spec.icao);
        shortlist.push(spec);
      }
      return shortlist;
    }

    for (const pair of pairs) {
      if (query.from && pair.d !== query.from) continue;
      if (query.to && pair.a !== query.to) continue;
      evaluated += 1;

      const departure = airportByIcao.get(pair.d);
      const arrival = airportByIcao.get(pair.a);
      if (!departure || !arrival) continue;
      const distance = haversineNm(
        [departure.lat, departure.lon],
        [arrival.lat, arrival.lon],
      );

      // One route, the best airframe for it. With no fleet named, prefer what the network
      // actually flies on that pair, and fall back to the range/speed heuristic only when
      // the pair has never been observed with a usable type.
      let best = null;
      const observed = flyingAnything && pair.ac ? aircraftByIcao.get(pair.ac) : undefined;
      const shortlist = observed
        ? [observed]
        : candidates(distance, flyingAnything ? 6 : specs.length);
      const fastestKt = shortlist.reduce(
        (fastest, spec) => Math.max(fastest, spec.tas || 0),
        0,
      );
      for (const spec of shortlist) {
        const candidate = score(pair, departAt, spec, weights, preset, durationWindow, fastestKt);
        if (candidate && (!best || candidate.utility > best.utility)) best = candidate;
      }
      if (!best && flyingAnything && durationWindow) {
        // The shortlist all missed the time window; widen before giving up.
        for (const spec of candidates(distance, 24)) {
          const candidate = score(pair, departAt, spec, weights, preset, durationWindow, fastestKt);
          if (candidate && (!best || candidate.utility > best.utility)) best = candidate;
        }
      }
      if (best) {
        best.presetKey = query.preset || "any";
        results.push(best);
      }
    }
    results.sort((a, b) => b.utility - a.utility);
    const limit = query.limit ?? 20;
    return {
      results: results.slice(0, limit),
      evaluated,
      matched: results.length,
      aircraft: flyingAnything ? "any" : specs.map((spec) => spec.icao).join(", "),
      preset: query.preset || "any",
      elapsedMs: performance.now() - started,
    };
  }

  // Scan the next few hours for the best moment. Used when the current hour is quiet:
  // rather than showing a board of 2% flights, tell the user when to fly instead.
  function bestWindow(query, options = {}) {
    const hours = options.hours ?? 10;
    const base = new Date(query.departAt);
    base.setUTCMinutes(0, 0, 0);
    let best = null;
    for (let step = 0; step < hours; step += 1) {
      const when = new Date(base.getTime() + step * 3600000);
      const outcome = plan({ ...query, departAt: when, limit: 1 });
      const top = outcome.results[0];
      if (!top) continue;
      if (!best || top.atc > best.atc) {
        best = {
          at: when,
          atc: top.atc,
          dep: top.dep,
          arr: top.arr,
          block: top.block,
        };
      }
    }
    return best;
  }

  // Coverage along the route, sample by sample, for the ribbon under the map. Only ever
  // called for the selected result: doing it for every candidate would multiply the cost
  // of a plan by the number of samples for no benefit.
  function ribbon(result, bins = 72) {
    const departure = airportByIcao.get(result.dep);
    const arrival = airportByIcao.get(result.arr);
    const spec = aircraftByIcao.get(result.aircraft);
    if (!departure || !arrival || !spec) return null;
    const departAt = result.departAt instanceof Date ? result.departAt : new Date(result.departAt);
    const samples = [];
    for (let index = 0; index < bins; index += 1) {
      const fraction = index / (bins - 1);
      const point = greatCircle(
        [departure.lat, departure.lon],
        [arrival.lat, arrival.lon],
        fraction,
      );
      const moment = new Date(departAt.getTime() + result.block * 60000 * fraction);
      const firIndex = firIndexAt(point[0], point[1]);
      // The first and last few percent are the terminal environment, where the airport's
      // service stack is what you actually talk to, not the overlying FIR.
      const probability =
        fraction <= 0.05
          ? result.departureP
          : fraction >= 0.95
            ? result.arrivalP
            : firProbability(firIndex, hourIndex(moment), moment);
      samples.push({
        fraction,
        probability,
        fir: firCodeOf(firIndex),
        terminal: fraction <= 0.05 || fraction >= 0.95,
      });
    }
    return { samples, departure: departure.icao, arrival: arrival.icao, block: result.block };
  }

  function coverageAt(date) {
    const hour = hourIndex(date);
    return firs.map((_, index) => firProbability(index + 1, hour));
  }

  return {
    plan,
    bestWindow,
    ribbon,
    setLive,
    hasLive: () => Boolean(liveState),
    routeProfile,
    firIndexAt,
    firProbability,
    firCtrProbability,
    firFssProbability,
    firDayCount,
    airportDayCount,
    airportLayer,
    layersAt,
    firCodeOf,
    coverageAt,
    hourIndex,
    airportByIcao,
    aircraftByIcao,
    meta,
    airports,
    aircraft,
    pairs,
    polygons: bundle.polygons,
    firs,
  };
}

export function explain(result) {
  // The percentage is already on the board; this line explains *why*, and which
  // controllers you would actually talk to.
  const parts = [];
  if (result.enroute >= 0.55) parts.push("solid en-route cover");
  else if (result.enroute >= 0.25) parts.push("patchy en-route");
  else if (result.enroute >= 0.05) parts.push("thin en-route cover");
  else parts.push("en-route mostly unicom");
  const departure = describeStack(result.departureLayers, "departure");
  const arrival = describeStack(result.arrivalLayers, "arrival");
  if (departure) parts.push(departure);
  if (arrival) parts.push(arrival);
  if (result.observed >= 5) parts.push(`${result.observed} recent flights`);
  return parts.join(" · ");
}
