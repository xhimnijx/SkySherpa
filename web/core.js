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
export const PRESETS = {
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
  const { meta, firs, firCtr, firFss, firGrid, airports, airportLayers, pairs, aircraft } = bundle;
  const width = meta.grid.width;
  const height = meta.grid.height;
  const resolution = meta.grid.resolution;

  const airportByIcao = new Map(airports.map((a) => [a.icao, a]));
  const airportSlot = new Map(airports.map((a, i) => [a.icao, i]));
  const aircraftByIcao = new Map(aircraft.map((a) => [a.icao, a]));
  const firCodeOf = (index) => (index > 0 && index <= firs.length ? firs[index - 1].code : "");

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

  function firProbability(index, hour) {
    const control = firCtrProbability(index, hour);
    const service = firFssProbability(index, hour);
    return control + FSS_VALUE * service * (1 - control);
  }

  function airportLayer(icao, layerIndex, hour) {
    const slot = airportSlot.get(icao);
    if (slot === undefined) return 0;
    const offset = (slot * LAYERS.length + layerIndex) * HOURS_PER_WEEK + hour;
    return airportLayers[offset] / 255;
  }

  function layersAt(icao, hour) {
    return {
      del: airportLayer(icao, 0, hour),
      gnd: airportLayer(icao, 1, hour),
      twr: airportLayer(icao, 2, hour),
      app: airportLayer(icao, 3, hour),
    };
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
    for (let i = 0; i <= samples; i += 1) {
      const fraction = i / samples;
      const point = greatCircle(
        [departure.lat, departure.lon],
        [arrival.lat, arrival.lon],
        fraction,
      );
      const moment = new Date(departAt.getTime() + block * 60000 * fraction);
      const probability = firProbability(firIndexAt(point[0], point[1]), hourIndex(moment));
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
    const arrivalMoment = new Date(departAt.getTime() + block * 60000);
    const departureLayers = layersAt(departure.icao, hourIndex(departAt));
    const arrivalLayers = layersAt(arrival.icao, hourIndex(arrivalMoment));
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
      coveredShare: covered / (samples + 1),
      unicomShare: longestGap / (samples + 1),
    };
  }

  function score(pair, departAt, aircraftSpec, weights, preset, blockTarget, tolerance) {
    const departure = airportByIcao.get(pair.d);
    const arrival = airportByIcao.get(pair.a);
    if (!departure || !arrival) return null;

    const profile = routeProfile(departure, arrival, departAt, aircraftSpec, preset);
    if (profile.distance > aircraftSpec.range_nm * 0.92) return null;
    if (blockTarget) {
      if (Math.abs(profile.block - blockTarget) > tolerance) return null;
    }

    const atc = 0.55 * profile.enroute + 0.2 * profile.departureP + 0.25 * profile.arrivalP;
    const durationFit = blockTarget
      ? Math.max(0, 1 - Math.abs(profile.block - blockTarget) / Math.max(1, tolerance))
      : 0.5;
    const popularity = Math.min(1, Math.log1p(pair.n) / Math.log(50));

    // Using the right aeroplane for the sector. A 777 on a 900 nm hop is technically
    // legal and practically absurd; a light aircraft at 90% of its range is the opposite
    // mistake. Both are penalised, the middle of the envelope is not.
    const utilisation = profile.distance / Math.max(1, aircraftSpec.range_nm);
    const aircraftFit =
      utilisation < 0.2
        ? Math.max(0, utilisation / 0.2)
        : utilisation > 0.75
          ? Math.max(0, (1 - utilisation) / 0.25)
          : 1;

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
    const preset = PRESETS[query.preset] || PRESETS.airline;
    const weights = { ...preset.weights, ...(query.weights || {}) };
    const requested = Array.isArray(query.aircraft) ? query.aircraft : [query.aircraft];
    let specs = requested
      .map((code) => aircraftByIcao.get(code))
      .filter((spec) => spec !== undefined);
    // A preset narrows the fleet to what the style implies, but never to nothing: if the
    // user owns only airliners, GA mode still has to produce an answer.
    if (preset.classes) {
      const suited = specs.filter((spec) => preset.classes.includes(spec.class));
      if (suited.length) specs = suited;
    }
    if (!specs.length) specs = [aircraftByIcao.get("A20N")].filter(Boolean);
    const departAt = query.departAt instanceof Date ? query.departAt : new Date(query.departAt);

    const results = [];
    let evaluated = 0;
    for (const pair of pairs) {
      if (query.from && pair.d !== query.from) continue;
      if (query.to && pair.a !== query.to) continue;
      evaluated += 1;

      // One route, every aircraft the user owns: keep the best airframe for it.
      let best = null;
      for (const spec of specs) {
        const candidate = score(
          pair,
          departAt,
          spec,
          weights,
          preset,
          query.blockMinutes,
          query.tolerance ?? 45,
        );
        if (candidate && (!best || candidate.utility > best.utility)) best = candidate;
      }
      if (best) results.push(best);
    }
    results.sort((a, b) => b.utility - a.utility);
    const limit = query.limit ?? 20;
    return {
      results: results.slice(0, limit),
      evaluated,
      matched: results.length,
      aircraft: specs.map((spec) => spec.icao).join(", "),
      preset: query.preset || "airline",
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

  function coverageAt(date) {
    const hour = hourIndex(date);
    return firs.map((_, index) => firProbability(index + 1, hour));
  }

  return {
    plan,
    bestWindow,
    routeProfile,
    firIndexAt,
    firProbability,
    firCtrProbability,
    firFssProbability,
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
