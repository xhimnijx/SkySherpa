// SkySherpa app: load the published bundle, plan locally, render a board and a map.
// No framework, no build step. Everything below is DOM + canvas.

import {
  PRESETS,
  createPlanner,
  explain,
  fitPreferenceWeights,
  greatCircle,
  hourIndex,
} from "./core.js";

const statusEl = document.getElementById("status");
const boardEl = document.getElementById("board");
const canvas = document.getElementById("map");
const ctx = canvas.getContext("2d");
const ribbonCanvas = document.getElementById("ribbon");
const ribbonCaption = document.getElementById("ribbonCaption");

const controls = {
  when: document.getElementById("when"),
  block: document.getElementById("block"),
  blockOut: document.getElementById("blockOut"),
  from: document.getElementById("from"),
  to: document.getElementById("to"),
  preset: document.getElementById("preset"),
  fleetButton: document.getElementById("fleetButton"),
  fleetCount: document.getElementById("fleetCount"),
  fleetDialog: document.getElementById("fleetDialog"),
  fleetList: document.getElementById("fleetList"),
  fleetSearch: document.getElementById("fleetSearch"),
  fleetApply: document.getElementById("fleetApply"),
  fleetCancel: document.getElementById("fleetCancel"),
  atc: document.getElementById("wAtc"),
  duration: document.getElementById("wDuration"),
  popularity: document.getElementById("wPopularity"),
  refresh: document.getElementById("refresh"),
  tasteButton: document.getElementById("tasteButton"),
  tasteDialog: document.getElementById("tasteDialog"),
  duelA: document.getElementById("duelA"),
  duelB: document.getElementById("duelB"),
  tasteProgress: document.getElementById("tasteProgress"),
  tasteReset: document.getElementById("tasteReset"),
  tasteClose: document.getElementById("tasteClose"),
  watch: document.getElementById("watch"),
  watchOut: document.getElementById("watchOut"),
  watchButton: document.getElementById("watchButton"),
  watchState: document.getElementById("watchState"),
  toast: document.getElementById("toast"),
};

let planner = null;
let selected = null;
let lastResults = [];
let live = null;
let suggestion = null; // an explicit departure time the visitor asked to preview
let suggestionInfo = null; // the best upcoming window, only when "now" is quiet
let pendingDuel = false; // deep link asked for the taste dialog once a plan exists
let pendingWatch = 0; // deep link asked to arm a watch once a plan exists

function formatZ(date) {
  return `${date.toISOString().slice(11, 16)}Z`;
}

// ---- fleet ----------------------------------------------------------------

const FLEET_KEY = "skysherpa.fleet";
let fleet = ["A20N"];

function loadFleet() {
  try {
    const raw = localStorage.getItem(FLEET_KEY);
    if (!raw) return ["A20N"];
    const parsed = JSON.parse(raw);
    const valid = Array.isArray(parsed)
      ? parsed.filter((code) => planner.aircraftByIcao.has(code))
      : [];
    return valid.length ? valid : ["A20N"];
  } catch (error) {
    return ["A20N"];
  }
}

function saveFleet() {
  try {
    localStorage.setItem(FLEET_KEY, JSON.stringify(fleet));
  } catch (error) {
    /* private mode: the session still works, it just will not remember */
  }
}

function updateFleetLabel() {
  controls.fleetCount.textContent = `(${fleet.length})`;
  if (fleet.length === 1) {
    const aircraft = planner.aircraftByIcao.get(fleet[0]);
    controls.fleetButton.textContent = aircraft ? `${fleet[0]} — ${aircraft.name}` : fleet[0];
  } else {
    controls.fleetButton.textContent = `${fleet.slice(0, 3).join(", ")}${fleet.length > 3 ? "…" : ""}`;
  }
}

function buildFleetOptions() {
  const rows = [...planner.aircraft]
    .sort((a, b) => a.class.localeCompare(b.class) || a.name.localeCompare(b.name))
    .map(
      (aircraft) => `
      <label class="fleet-item" data-search="${`${aircraft.icao} ${aircraft.name} ${aircraft.class}`.toLowerCase()}">
        <input type="checkbox" value="${aircraft.icao}" ${fleet.includes(aircraft.icao) ? "checked" : ""} />
        <span><b>${aircraft.icao}</b> ${aircraft.name}</span>
        <em>${aircraft.class}</em>
      </label>`,
    )
    .join("");
  controls.fleetList.innerHTML = rows;
}

function openFleet() {
  buildFleetOptions();
  controls.fleetSearch.value = "";
  controls.fleetDialog.showModal();
}

function applyFleet() {
  const checked = [...controls.fleetList.querySelectorAll("input:checked")].map(
    (input) => input.value,
  );
  if (checked.length) fleet = checked;
  saveFleet();
  updateFleetLabel();
  controls.fleetDialog.close();
  recompute();
}

// ---- taste ----------------------------------------------------------------

const TASTE_KEY = "skysherpa.taste";
let taste = { choices: [], weights: null };
let weightsTouched = false;
let duelPair = null;

function loadTaste() {
  try {
    const raw = localStorage.getItem(TASTE_KEY);
    if (!raw) return;
    const parsed = JSON.parse(raw);
    if (Array.isArray(parsed.choices) && parsed.choices.length) {
      taste.choices = parsed.choices;
      // Refit rather than trusting stored weights: if the learner improves, the user gets
      // the improvement instead of a stale vector.
      taste.weights = fitPreferenceWeights(taste.choices, PRESETS.airline.weights);
    }
  } catch (error) {
    /* corrupt or unavailable storage: fall back to the preset */
  }
}

function saveTaste() {
  try {
    localStorage.setItem(TASTE_KEY, JSON.stringify({ choices: taste.choices }));
  } catch (error) {
    /* private mode */
  }
}

function presetWeights() {
  return (PRESETS[controls.preset.value] || PRESETS.airline).weights;
}

function applyPresetToSliders() {
  const weights = presetWeights();
  controls.atc.value = String(Math.round(weights.atc * 100));
  controls.duration.value = String(Math.round(weights.duration * 100));
  controls.popularity.value = String(Math.round(weights.popularity * 100));
}

function manualWeights() {
  return {
    atc: Number(controls.atc.value) / 100,
    duration: Number(controls.duration.value) / 100,
    popularity: Number(controls.popularity.value) / 100,
    fit: presetWeights().fit,
  };
}

function activeWeights() {
  if (taste.weights) return taste.weights;
  if (weightsTouched) return manualWeights();
  return undefined;
}

function featureVector(result) {
  return {
    atc: result.atc,
    duration: result.durationFit,
    fit: result.aircraftFit,
    popularity: result.popularity,
  };
}

function cardMarkup(result) {
  const hours = Math.floor(result.block / 60);
  const minutes = String(result.block % 60).padStart(2, "0");
  return `<span class="duel-route">${result.dep} → ${result.arr}</span>
    <span class="duel-meta">${result.aircraft} · ${hours}h${minutes} · ${result.distance} nm</span>
    <span class="duel-pct">${Math.round(result.atc * 100)}%</span>
    <span class="duel-why">${explain(result)}</span>`;
}

function pickDuel() {
  const pool = lastResults.slice(0, Math.min(8, lastResults.length));
  if (pool.length < 2) return null;
  const first = pool[Math.floor(Math.random() * pool.length)];
  let second = first;
  for (let guard = 0; guard < 20 && second === first; guard += 1) {
    second = pool[Math.floor(Math.random() * pool.length)];
  }
  return second === first ? null : [first, second];
}

function tasteProgress() {
  const weights = taste.weights;
  if (!weights) return "No choices yet - the ranking uses the preset.";
  const count = taste.choices.length;
  return (
    `${count} choice${count === 1 ? "" : "s"} · ATC ${weights.atc.toFixed(2)} · ` +
    `time ${weights.duration.toFixed(2)} · fit ${weights.fit.toFixed(2)} · ` +
    `popular ${weights.popularity.toFixed(2)}`
  );
}

function renderDuel() {
  duelPair = pickDuel();
  if (!duelPair) {
    controls.duelA.disabled = true;
    controls.duelB.disabled = true;
    controls.duelA.innerHTML = `<span class="duel-why">Not enough flights to compare. Widen the time window or clear the airports.</span>`;
    controls.duelB.innerHTML = "";
  } else {
    controls.duelA.disabled = false;
    controls.duelB.disabled = false;
    controls.duelA.innerHTML = cardMarkup(duelPair[0]);
    controls.duelB.innerHTML = cardMarkup(duelPair[1]);
  }
  controls.tasteProgress.textContent = tasteProgress();
}

function recordChoice(winner) {
  if (!duelPair) return;
  taste.choices.push({
    a: featureVector(duelPair[0]),
    b: featureVector(duelPair[1]),
    winner,
  });
  taste.weights = fitPreferenceWeights(taste.choices, presetWeights());
  saveTaste();
  recompute();
  renderDuel();
}

function openTaste() {
  if (!taste.weights && taste.choices.length) {
    taste.weights = fitPreferenceWeights(taste.choices, presetWeights());
  }
  renderDuel();
  controls.tasteDialog.showModal();
}

// ---- watch ----------------------------------------------------------------

const WATCH_KEY = "skysherpa.watch";
let watch = null;
const alerted = new Set();

function loadWatch() {
  try {
    const raw = localStorage.getItem(WATCH_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw);
    return parsed && typeof parsed.threshold === "number" ? parsed : null;
  } catch (error) {
    return null;
  }
}

function saveWatch() {
  try {
    if (watch) localStorage.setItem(WATCH_KEY, JSON.stringify(watch));
    else localStorage.removeItem(WATCH_KEY);
  } catch (error) {
    /* private mode */
  }
}

function watchSummary() {
  const hours = Math.floor(watch.blockMinutes / 60);
  const minutes = String(watch.blockMinutes % 60).padStart(2, "0");
  const scope =
    watch.from || watch.to ? `${watch.from || "anywhere"} → ${watch.to || "anywhere"}` : "anywhere";
  return `Watching for >=${watch.threshold}% ATC · ${hours}h${minutes} · ${scope}`;
}

function updateWatchState() {
  controls.watchState.textContent = watch ? watchSummary() : "";
  controls.watchButton.textContent = watch ? "Stop watching" : "Watch this window";
}

function armWatch(threshold) {
  watch = {
    threshold: Math.round(threshold),
    blockMinutes: Number(controls.block.value),
    from: controls.from.value || null,
    to: controls.to.value || null,
    fleet: fleet.slice(),
    preset: controls.preset.value || "airline",
    weights: activeWeights() || null,
  };
  alerted.clear();
  saveWatch();
  controls.watch.value = String(watch.threshold);
  controls.watchOut.textContent = String(watch.threshold);
  updateWatchState();
  // Permission must be requested from a user gesture; a deep link simply gets the
  // in-page toast instead.
  if ("Notification" in window && Notification.permission === "default") {
    try {
      Notification.requestPermission().catch(() => {});
    } catch (error) {
      /* not available */
    }
  }
  checkWatch();
}

function disarmWatch() {
  watch = null;
  saveWatch();
  updateWatchState();
  controls.toast.classList.remove("show");
}

function watchQuery() {
  return {
    departAt: departureTime(),
    blockMinutes: watch.blockMinutes,
    tolerance: Math.max(30, Math.round(watch.blockMinutes * 0.25)),
    from: watch.from,
    to: watch.to,
    aircraft: watch.fleet,
    preset: watch.preset,
    weights: watch.weights || undefined,
    limit: 6,
  };
}

function checkWatch() {
  if (!watch || !planner) return;
  const outcome = planner.plan(watchQuery());
  const hit = outcome.results.find((result) => result.atc * 100 >= watch.threshold);
  if (!hit) return;
  const hourKey = new Date().toISOString().slice(0, 13);
  const key = `${hit.dep}-${hit.arr}-${hit.aircraft}-${hourKey}`;
  if (alerted.has(key)) return;
  alerted.add(key);
  showToast(hit);
}

function showToast(hit) {
  const hours = Math.floor(hit.block / 60);
  const minutes = String(hit.block % 60).padStart(2, "0");
  controls.toast.innerHTML = `
    <b>${hit.dep} → ${hit.arr}</b> is at ${Math.round(hit.atc * 100)}% ATC
    <span class="toast-meta">${hit.aircraft} · ${hours}h${minutes}</span>
    <div class="toast-actions">
      <button id="toastGo" type="button" class="primary">Show me</button>
      <button id="toastClose" type="button" class="ghost">Dismiss</button>
    </div>`;
  controls.toast.classList.add("show");

  const go = document.getElementById("toastGo");
  if (go) {
    go.addEventListener("click", () => {
      controls.from.value = hit.dep;
      controls.to.value = hit.arr;
      suggestion = null;
      controls.toast.classList.remove("show");
      recompute();
    });
  }
  const close = document.getElementById("toastClose");
  if (close) close.addEventListener("click", () => controls.toast.classList.remove("show"));

  if ("Notification" in window && Notification.permission === "granted") {
    try {
      new Notification("SkySherpa", {
        body: `${hit.dep} → ${hit.arr} · ${Math.round(hit.atc * 100)}% ATC · ${hours}h${minutes}`,
      });
    } catch (error) {
      /* some browsers require a service worker for notifications */
    }
  }
}

function toggleWatch() {
  if (watch) disarmWatch();
  else armWatch(Number(controls.watch.value));
}

async function loadBundle() {
  // meta.json is fetched with no-store, then every other file carries the generation
  // timestamp as a query string. Without this a browser can serve yesterday's forecast
  // from cache while the publisher is happily rewriting it every fifteen minutes.
  const metaResponse = await fetch("data/meta.json", { cache: "no-store" });
  if (!metaResponse.ok) throw new Error(`meta.json: HTTP ${metaResponse.status}`);
  const meta = await metaResponse.json();
  const version = encodeURIComponent(meta.generated_at || String(Date.now()));

  const binary = async (name) => (await fetch(`data/${name}?v=${version}`)).arrayBuffer();
  const json = async (name) => (await fetch(`data/${name}?v=${version}`)).json();

  const [firs, airports, positions, pairs, aircraft, polygons] = await Promise.all([
    json("firs.json"),
    json("airports.json"),
    json("positions.json"),
    json("pairs.json"),
    json("aircraft.json"),
    json("polygons.json"),
  ]);
  const [firCtrBuf, firFssBuf, firGridBuf, airportLayersBuf, firDaysBuf, airportDaysBuf] =
    await Promise.all([
      binary("fir_ctr.bin"),
      binary("fir_fss.bin"),
      binary("fir_grid.bin"),
      binary("airport_layers.bin"),
      binary("fir_days.bin"),
      binary("airport_days.bin"),
    ]);

  return {
    meta,
    firs,
    airports,
    pairs,
    aircraft,
    polygons,
    positions,
    firCtr: new Uint8Array(firCtrBuf),
    firFss: new Uint8Array(firFssBuf),
    firGrid: new Uint16Array(firGridBuf),
    airportLayers: new Uint8Array(airportLayersBuf),
    firDays: new Uint8Array(firDaysBuf),
    airportDays: new Uint8Array(airportDaysBuf),
  };
}

function populateSelects() {
  const airports = [...planner.airports].sort((a, b) => b.traffic - a.traffic);
  const options = airports
    .slice(0, 400)
    .map((a) => `<option value="${a.icao}">${a.icao} — ${a.name}</option>`)
    .join("");
  controls.from.innerHTML = `<option value="">anywhere</option>${options}`;
  controls.to.innerHTML = `<option value="">anywhere</option>${options}`;
}

function departureTime() {
  if (suggestion) return new Date(suggestion);
  const value = controls.when.value;
  const now = new Date();
  if (value === "now") return new Date(now.getTime() + 10 * 60000);
  if (value === "1h") return new Date(now.getTime() + 70 * 60000);
  if (value === "3h") return new Date(now.getTime() + 190 * 60000);
  if (value === "evening") {
    const target = new Date(now);
    target.setUTCHours(19, 0, 0, 0);
    if (target < now) target.setUTCDate(target.getUTCDate() + 1);
    return target;
  }
  return now;
}

function recompute() {
  if (!planner) return;
  const block = Number(controls.block.value);
  controls.blockOut.textContent = `${Math.floor(block / 60)}h${String(block % 60).padStart(2, "0")}`;

  const query = {
    departAt: departureTime(),
    blockMinutes: block,
    tolerance: Math.max(30, Math.round(block * 0.25)),
    from: controls.from.value || null,
    to: controls.to.value || null,
    aircraft: fleet,
    preset: controls.preset.value || "airline",
    weights: activeWeights(),
    limit: 18,
  };

  const outcome = planner.plan(query);
  lastResults = outcome.results;

  // If the next hour is quiet, find out when it is not. Showing a board of 2%
  // flights is technically honest and practically useless.
  if (!suggestion && outcome.results.length) {
    const top = outcome.results[0];
    if (top.atc < 0.2) {
      const window = planner.bestWindow({ ...query, limit: 1 }, { hours: 24 });
      suggestionInfo = window && window.atc >= top.atc + 0.08 ? window : null;
    } else {
      suggestionInfo = null;
    }
  }

  syncWhenSelect();
  if (!selected || !outcome.results.some((r) => r.dep === selected.dep && r.arr === selected.arr)) {
    selected = outcome.results[0] ?? null;
  }
  writeHash(query);
  renderBoard(outcome, query);
  drawMap(query.departAt);
  drawRibbon(selected);
}

// ---- ribbon ----------------------------------------------------------------

function ribbonColour(probability) {
  if (probability <= 0.02) return "rgba(70, 92, 112, 0.55)";
  return `hsl(${(205 - probability * 60).toFixed(0)}, 78%, 52%)`;
}

function drawRibbon(result) {
  const dpr = window.devicePixelRatio || 1;
  const width = ribbonCanvas.clientWidth || 600;
  const height = 54;
  ribbonCanvas.width = width * dpr;
  ribbonCanvas.height = height * dpr;
  const g = ribbonCanvas.getContext("2d");
  g.setTransform(dpr, 0, 0, dpr, 0, 0);
  g.clearRect(0, 0, width, height);
  g.font = "10px ui-monospace, monospace";
  g.textBaseline = "top";

  if (!result || !planner) {
    ribbonCaption.textContent = "";
    return;
  }
  const data = planner.ribbon(result);
  if (!data) return;

  const samples = data.samples;
  const step = width / samples.length;
  const barTop = 14;
  const barHeight = 22;

  samples.forEach((sample, index) => {
    g.fillStyle = ribbonColour(sample.probability);
    g.fillRect(index * step, barTop, step + 0.8, barHeight);
  });

  g.strokeStyle = "rgba(120, 170, 220, 0.22)";
  g.lineWidth = 1;
  g.strokeRect(0.5, barTop + 0.5, width - 1, barHeight - 1);

  // Airspace boundaries and their codes, when the segment is wide enough to label.
  let segmentStart = 0;
  for (let index = 1; index <= samples.length; index += 1) {
    const boundary =
      index === samples.length || samples[index].fir !== samples[segmentStart].fir;
    if (!boundary) continue;
    const x = index * step;
    if (index < samples.length) {
      g.fillStyle = "rgba(8, 17, 28, 0.9)";
      g.fillRect(x - 0.5, barTop, 1, barHeight);
    }
    const code = samples[segmentStart].fir;
    if (code && x - segmentStart * step > 34) {
      g.fillStyle = "rgba(148, 163, 184, 0.9)";
      g.fillText(code, segmentStart * step + 3, barTop + barHeight + 4);
    }
    segmentStart = index;
  }

  g.fillStyle = "#cbd5e1";
  g.fillText(data.departure, 0, 0);
  const arrivalWidth = g.measureText(data.arrival).width;
  g.fillText(data.arrival, width - arrivalWidth, 0);

  const worst = Math.min(...samples.map((sample) => sample.probability));
  const airspaces = new Set(samples.map((sample) => sample.fir)).size;
  const hours = Math.floor(data.block / 60);
  const minutes = String(data.block % 60).padStart(2, "0");
  ribbonCaption.textContent =
    `${data.departure} → ${data.arrival} · ${hours}h${minutes} · ` +
    `worst stretch ${Math.round(worst * 100)}% ATC · ${airspaces} airspace${airspaces === 1 ? "" : "s"}`;
}

// Keep the Departure control honest: while a preview window is active it should say so.
function syncWhenSelect() {
  const existing = controls.when.querySelector('option[value="at"]');
  if (suggestion) {
    if (existing) {
      existing.textContent = `preview ${formatZ(suggestion)}`;
    } else {
      const option = document.createElement("option");
      option.value = "at";
      option.textContent = `preview ${formatZ(suggestion)}`;
      controls.when.appendChild(option);
    }
    controls.when.value = "at";
    return;
  }
  if (existing) {
    const wasPreview = controls.when.value === "at";
    existing.remove();
    if (wasPreview) controls.when.value = "now";
  }
}

function renderBoard(outcome, query) {
  if (!planner.pairs.length) {
    boardEl.innerHTML = `
      <div class="empty fatal">
        <b>No candidate routes yet.</b>
        <p>The collector has seen aircraft, but no filed flight plans with a usable
        origin and destination. Publish again in a few minutes.</p>
      </div>`;
    return;
  }
  if (!outcome.results.length) {
    boardEl.innerHTML = `<div class="empty">No flights match. Widen the flight length or clear the airports.</div>`;
    return;
  }
  const rows = outcome.results
    .map((result, index) => {
      const active = selected && result.dep === selected.dep && result.arr === selected.arr;
      const pct = Math.round(result.atc * 100);
      const cls = pct >= 60 ? "good" : pct >= 35 ? "ok" : "thin";
      const evidenceDays = Number(result.days) || 0;
      const evidenceLabel = evidenceDays > 0 ? `${evidenceDays}d` : "prior";
      const evidenceClass = evidenceDays >= 14 ? "" : "thin";
      return `
      <button class="row ${active ? "active" : ""}" data-index="${index}" title="Probability is based on ${evidenceDays || 0} day(s) of history for the airspace along this route">
        <span class="rank">${index + 1}</span>
        <span class="pair"><b>${result.dep}</b> → <b>${result.arr}</b>
          <small>${result.depName} to ${result.arrName}</small></span>
        <span class="stats">
          <span class="days ${evidenceClass}">${evidenceLabel}</span>
          <span class="pct ${cls}">${pct}%</span>
          <span class="meta">${result.aircraft} · ${Math.floor(result.block / 60)}h${String(result.block % 60).padStart(2, "0")} · ${result.distance} nm</span>
        </span>
        <span class="why">${explain(result)}</span>
      </button>`;
    })
    .join("");
  const hint = suggestion
    ? `<div class="hint">Previewing <b>${formatZ(departureTime())}</b><button data-action="clear">back to now</button></div>`
    : suggestionInfo
      ? `<div class="hint">Quiet right now &mdash; best window in the next 24 h is <b>${formatZ(suggestionInfo.at)}</b> (${Math.round(suggestionInfo.atc * 100)}% ATC, ${suggestionInfo.dep}&ndash;${suggestionInfo.arr})<button data-action="use">preview</button></div>`
      : "";

  boardEl.innerHTML = `
    <div class="board-head">
      <span>${outcome.matched} flights · ${Math.max(0.1, outcome.elapsedMs).toFixed(1)} ms · departing ${formatZ(query.departAt)}</span>
      <span>${(PRESETS[outcome.preset] || PRESETS.airline).label}${taste.weights ? " · tuned" : ""}</span>
    </div>${forecastWarning()}${hint}${rows}`;

  const useHint = boardEl.querySelector('[data-action="use"]');
  if (useHint) {
    useHint.addEventListener("click", () => {
      if (suggestionInfo) suggestion = suggestionInfo.at;
      suggestionInfo = null;
      recompute();
    });
  }
  const clearHint = boardEl.querySelector('[data-action="clear"]');
  if (clearHint) {
    clearHint.addEventListener("click", () => {
      suggestion = null;
      controls.when.value = "now";
      recompute();
    });
  }
  boardEl.querySelectorAll(".row").forEach((node) => {
    node.addEventListener("click", () => {
      selected = lastResults[Number(node.dataset.index)];
      renderBoard(outcome, query);
      drawMap(query.departAt);
      drawRibbon(selected);
    });
  });
}

// ---- map ------------------------------------------------------------------

const VIEW = { lonMin: -180, latMax: 72, lonMax: 180, latMin: -58 };

function projection() {
  const dpr = window.devicePixelRatio || 1;
  const cssWidth = canvas.clientWidth || 800;
  const cssHeight = canvas.clientHeight || 520;
  canvas.width = cssWidth * dpr;
  canvas.height = cssHeight * dpr;
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);

  let bounds = VIEW;
  if (selected) {
    const dep = planner.airportByIcao.get(selected.dep);
    const arr = planner.airportByIcao.get(selected.arr);
    if (dep && arr) {
      const padLon = Math.max(12, Math.abs(dep.lon - arr.lon) * 0.25);
      const padLat = Math.max(8, Math.abs(dep.lat - arr.lat) * 0.25);
      bounds = {
        lonMin: Math.min(dep.lon, arr.lon) - padLon,
        lonMax: Math.max(dep.lon, arr.lon) + padLon,
        latMin: Math.min(dep.lat, arr.lat) - padLat,
        latMax: Math.max(dep.lat, arr.lat) + padLat,
      };
    }
  }
  const project = (lon, lat) => [
    ((lon - bounds.lonMin) / (bounds.lonMax - bounds.lonMin)) * cssWidth,
    ((bounds.latMax - lat) / (bounds.latMax - bounds.latMin)) * cssHeight,
  ];
  return { project, width: cssWidth, height: cssHeight, bounds };
}

function coverageColour(probability) {
  if (probability <= 0.02) return null;
  const hue = 205 - probability * 60; // blue -> green
  const alpha = 0.08 + probability * 0.55;
  return `hsla(${hue}, 78%, 52%, ${alpha.toFixed(3)})`;
}

function drawMap(at) {
  if (!planner) return;
  // Derive the mid-flight moment from the selected route, not from a parameter: a stray
  // redraw (resize, click) was passing nothing and showing the departure hour instead.
  const blockMinutes = selected && selected.block ? selected.block : 0;
  const { project, width, height } = projection();
  ctx.clearRect(0, 0, width, height);
  ctx.fillStyle = "#08111c";
  ctx.fillRect(0, 0, width, height);

  // Show the airspace at the middle of the flight, not at the gate. A departure-hour
  // map next to a whole-flight score reads as a contradiction.
  const midFlight = new Date(at.getTime() + Math.max(0, blockMinutes) * 30000);
  canvas.dataset.mid = midFlight.toISOString();
  const hour = hourIndex(midFlight);

  // Airspace choropleth: where will someone be talking?
  for (const polygon of planner.polygons) {
    if (!polygon.id) continue;
    const probability = planner.firProbability(polygon.id, hour);
    const fill = coverageColour(probability);
    if (!fill) continue;
    ctx.beginPath();
    polygon.pts.forEach(([lon, lat], index) => {
      const [x, y] = project(lon, lat);
      if (index === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    });
    ctx.closePath();
    ctx.fillStyle = fill;
    ctx.fill();
  }

  // FIR outlines, faint.
  ctx.strokeStyle = "rgba(120, 170, 220, 0.16)";
  ctx.lineWidth = 0.6;
  for (const polygon of planner.polygons) {
    ctx.beginPath();
    polygon.pts.forEach(([lon, lat], index) => {
      const [x, y] = project(lon, lat);
      if (index === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    });
    ctx.closePath();
    ctx.stroke();
  }

  if (selected) {
    const dep = planner.airportByIcao.get(selected.dep);
    const arr = planner.airportByIcao.get(selected.arr);
    if (dep && arr) {
      ctx.beginPath();
      for (let i = 0; i <= 64; i += 1) {
        const [lat, lon] = greatCircle([dep.lat, dep.lon], [arr.lat, arr.lon], i / 64);
        const [x, y] = project(lon, lat);
        if (i === 0) ctx.moveTo(x, y);
        else ctx.lineTo(x, y);
      }
      ctx.strokeStyle = "#7dd3fc";
      ctx.lineWidth = 2;
      ctx.stroke();

      for (const airport of [dep, arr]) {
        const [x, y] = project(airport.lon, airport.lat);
        ctx.beginPath();
        ctx.arc(x, y, 4, 0, Math.PI * 2);
        ctx.fillStyle = "#f8fafc";
        ctx.fill();
        ctx.fillStyle = "rgba(248,250,252,0.85)";
        ctx.font = "11px ui-monospace, monospace";
        ctx.fillText(airport.icao, x + 7, y + 4);
      }
    }
  }

  ctx.fillStyle = "rgba(148, 163, 184, 0.75)";
  ctx.font = "11px ui-monospace, monospace";
  const stamp = midFlight.toISOString().slice(11, 16);
  ctx.fillText(`forecast for ${stamp}Z (mid-flight)`, 12, height - 12);
  if (live) {
    ctx.fillText(`live: ${live.controllers} controllers online`, 12, height - 28);
  }
}

// ---- live network ----------------------------------------------------------

async function refreshLive() {
  try {
    const response = await fetch("https://data.vatsim.net/v3/vatsim-data.json", {
      cache: "no-store",
    });
    const payload = await response.json();
    const controllers = payload.controllers || [];
    const atc = controllers.filter((controller) => controller.facility >= 1);
    live = {
      controllers: atc.length,
      pilots: (payload.pilots || []).length,
      updated: payload.general?.update_timestamp,
    };
    // Hand the live staffing to the planner: the forecast is now anchored to who is
    // actually online, decaying over two hours.
    planner.setLive({
      at: new Date(payload.general?.update_timestamp ?? Date.now()),
      positions: atc.map((controller) => ({
        key: controller.callsign,
        facility: controller.facility,
      })),
    });
    recompute();
  } catch (error) {
    live = null;
    if (planner) planner.setLive(null);
  }
  updateStatus();
}

function updateStatus() {
  if (!planner) return;
  const meta = planner.meta;
  const generated = new Date(meta.generated_at);
  const minutes = Math.max(0, Math.round((Date.now() - generated.getTime()) / 60000));
  const age = minutes < 90 ? `${minutes} min` : `${Math.round(minutes / 60)} h`;
  const liveText = live ? ` · live: ${live.controllers} ATC / ${live.pilots} pilots` : "";
  const anchored = planner.hasLive && planner.hasLive() ? " · live staffing blended" : "";
  const days = meta.days_observed;
  const daysText = days === 1 ? "1 day" : `${days} days`;
  // Under two weeks of history the forecast is mostly the prior. Saying so is the
  // difference between a product that is trusted later and one that is impressive once.
  const caution = days < 14 ? ` · early days: thin history, treat as a rough guess` : "";
  statusEl.textContent =
    `forecast ${age} old · ${daysText} observed · ` +
    `${meta.positions_modelled} positions${liveText}${anchored}${caution}`;
}

// ---- permalinks ------------------------------------------------------------

function readHash() {
  const params = new URLSearchParams(location.hash.replace(/^#/, ""));
  if (!params.toString()) return;
  const block = params.get("block");
  if (block) controls.block.value = block;
  const from = params.get("from");
  if (from) controls.from.value = from;
  const to = params.get("to");
  if (to) controls.to.value = to;
  const aircraft = params.get("aircraft");
  if (aircraft) controls.aircraft.value = aircraft;
  const when = params.get("when");
  if (when) controls.when.value = when;
  const preset = params.get("preset");
  if (preset && PRESETS[preset]) controls.preset.value = preset;
  const fleetParam = params.get("fleet");
  if (fleetParam) {
    const codes = fleetParam.split(",").filter((code) => planner.aircraftByIcao.has(code));
    if (codes.length) {
      fleet = codes;
      updateFleetLabel();
    }
  }
  const at = params.get("at");
  if (at) {
    const parsed = new Date(at);
    if (!Number.isNaN(parsed.getTime())) suggestion = parsed;
  }
  if (params.get("pick") === "1") {
    buildFleetOptions();
    controls.fleetDialog.showModal();
  }
  if (params.get("duel") === "1") pendingDuel = true;
  const watchParam = params.get("watch");
  if (watchParam) pendingWatch = Number(watchParam) || 0;
}

function writeHash(query) {
  const params = new URLSearchParams();
  if (query.from) params.set("from", query.from);
  if (query.to) params.set("to", query.to);
  params.set("preset", query.preset);
  params.set("block", String(query.blockMinutes));
  if (fleet.length && fleet.length <= 12) params.set("fleet", fleet.join(","));
  if (suggestion) params.set("at", `${suggestion.toISOString().slice(0, 16)}Z`);
  const next = params.toString();
  try {
    history.replaceState(null, "", `${location.pathname}${location.search}#${next}`);
  } catch (error) {
    /* file:// and some embeds refuse history changes; the page still works */
  }
}

function forecastWarning() {
  if (!planner) return "";
  const generated = new Date(planner.meta.generated_at);
  const hours = (Date.now() - generated.getTime()) / 3600000;
  if (!Number.isFinite(hours) || hours < 12) return "";
  return `<div class="hint warn">This forecast is ${Math.round(hours)} h old. The publisher may be stopped, so the probabilities are stale.</div>`;
}

function showLoadError(error) {
  statusEl.textContent = "forecast unavailable";
  boardEl.innerHTML = `
    <div class="empty fatal">
      <b>Could not load the forecast.</b>
      <p>${error.message}</p>
      <p class="small">If this is a local install, run <code>skysherpa publish</code> first,
      then reload. The trace is in the browser console.</p>
      <button id="retryButton" type="button" class="primary">Retry</button>
    </div>`;
  const retry = document.getElementById("retryButton");
  if (retry) retry.addEventListener("click", () => location.reload());
  console.error(error);
}

// ---- boot ------------------------------------------------------------------

async function boot() {
  try {
    const bundle = await loadBundle();
    planner = createPlanner(bundle);
    populateSelects();
    fleet = loadFleet();
    updateFleetLabel();
    loadTaste();
    applyPresetToSliders();
    watch = loadWatch();
    controls.watch.value = String(watch ? watch.threshold : 70);
    controls.watchOut.textContent = controls.watch.value;
    updateWatchState();
    controls.when.value = "now";
    readHash();
    updateStatus();
    recompute();
    if (pendingDuel) {
      pendingDuel = false;
      openTaste();
    }
    if (pendingWatch) {
      const threshold = pendingWatch;
      pendingWatch = 0;
      armWatch(threshold);
    }
  } catch (error) {
    showLoadError(error);
    return;
  }

  controls.when.addEventListener("input", () => {
    suggestion = null;
    suggestionInfo = null;
    recompute();
  });
  for (const key of ["block", "from", "to"]) {
    controls[key].addEventListener("input", recompute);
  }
  // Moving a weight slider is an explicit opinion for this session; it overrides what
  // was learned, without deleting the choices (Tune my taste refits them).
  for (const key of ["atc", "duration", "popularity"]) {
    controls[key].addEventListener("input", () => {
      weightsTouched = true;
      taste.weights = null;
      recompute();
    });
  }
  controls.preset.addEventListener("input", () => {
    applyPresetToSliders();
    weightsTouched = false;
    if (taste.choices.length) {
      taste.weights = fitPreferenceWeights(taste.choices, presetWeights());
    }
    recompute();
  });
  controls.tasteButton.addEventListener("click", openTaste);
  controls.tasteClose.addEventListener("click", () => controls.tasteDialog.close());
  controls.tasteReset.addEventListener("click", () => {
    taste = { choices: [], weights: null };
    saveTaste();
    recompute();
    renderDuel();
  });
  controls.duelA.addEventListener("click", () => recordChoice("a"));
  controls.duelB.addEventListener("click", () => recordChoice("b"));
  controls.watchButton.addEventListener("click", toggleWatch);
  controls.watch.addEventListener("input", () => {
    controls.watchOut.textContent = controls.watch.value;
  });
  setInterval(checkWatch, 300000);
  controls.fleetButton.addEventListener("click", openFleet);
  controls.fleetApply.addEventListener("click", applyFleet);
  controls.fleetCancel.addEventListener("click", () => controls.fleetDialog.close());
  controls.fleetSearch.addEventListener("input", () => {
    const needle = controls.fleetSearch.value.trim().toLowerCase();
    for (const item of controls.fleetList.querySelectorAll(".fleet-item")) {
      item.style.display = !needle || item.dataset.search.includes(needle) ? "" : "none";
    }
  });
  controls.refresh.addEventListener("click", () => {
    refreshLive();
    recompute();
  });
  window.addEventListener("resize", () => {
    drawMap(departureTime());
    drawRibbon(selected);
  });

  refreshLive();
  setInterval(refreshLive, 120000);
  setInterval(recompute, 60000); // the clock moves; the answer should too
}

boot();
