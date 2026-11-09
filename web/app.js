// SkySherpa app: load the published bundle, plan locally, render a board and a map.
// No framework, no build step. Everything below is DOM + canvas.

import { createPlanner, explain, greatCircle, hourIndex } from "./core.js";

const statusEl = document.getElementById("status");
const boardEl = document.getElementById("board");
const canvas = document.getElementById("map");
const ctx = canvas.getContext("2d");

const controls = {
  when: document.getElementById("when"),
  block: document.getElementById("block"),
  blockOut: document.getElementById("blockOut"),
  from: document.getElementById("from"),
  to: document.getElementById("to"),
  aircraft: document.getElementById("aircraft"),
  atc: document.getElementById("wAtc"),
  duration: document.getElementById("wDuration"),
  popularity: document.getElementById("wPopularity"),
  refresh: document.getElementById("refresh"),
};

let planner = null;
let selected = null;
let lastResults = [];
let live = null;
let suggestion = null; // an explicit departure time the visitor asked to preview
let suggestionInfo = null; // the best upcoming window, only when "now" is quiet

function formatZ(date) {
  return `${date.toISOString().slice(11, 16)}Z`;
}

async function loadBundle() {
  const binary = async (name) => (await fetch(`data/${name}`)).arrayBuffer();
  const json = async (name) => (await fetch(`data/${name}`)).json();

  const [meta, firs, airports, pairs, aircraft, polygons] = await Promise.all([
    json("meta.json"),
    json("firs.json"),
    json("airports.json"),
    json("pairs.json"),
    json("aircraft.json"),
    json("polygons.json"),
  ]);
  const [firCtrBuf, firFssBuf, firGridBuf, airportLayersBuf] = await Promise.all([
    binary("fir_ctr.bin"),
    binary("fir_fss.bin"),
    binary("fir_grid.bin"),
    binary("airport_layers.bin"),
  ]);

  return {
    meta,
    firs,
    airports,
    pairs,
    aircraft,
    polygons,
    firCtr: new Uint8Array(firCtrBuf),
    firFss: new Uint8Array(firFssBuf),
    firGrid: new Uint16Array(firGridBuf),
    airportLayers: new Uint8Array(airportLayersBuf),
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

  const classes = [...new Set(planner.aircraft.map((a) => a.class))].sort();
  controls.aircraft.innerHTML = classes
    .map((klass) => {
      const group = planner.aircraft
        .filter((a) => a.class === klass)
        .sort((a, b) => a.name.localeCompare(b.name))
        .map((a) => `<option value="${a.icao}">${a.icao} — ${a.name}</option>`)
        .join("");
      return `<optgroup label="${klass}">${group}</optgroup>`;
    })
    .join("");
  controls.aircraft.value = "A20N";
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
    aircraft: controls.aircraft.value || "A20N",
    limit: 18,
    weights: {
      atc: Number(controls.atc.value) / 100,
      duration: Number(controls.duration.value) / 100,
      popularity: Number(controls.popularity.value) / 100,
    },
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
  if (!outcome.results.length) {
    boardEl.innerHTML = `<div class="empty">No flights match. Widen the flight length or clear the airports.</div>`;
    return;
  }
  const rows = outcome.results
    .map((result, index) => {
      const active = selected && result.dep === selected.dep && result.arr === selected.arr;
      const pct = Math.round(result.atc * 100);
      const cls = pct >= 60 ? "good" : pct >= 35 ? "ok" : "thin";
      return `
      <button class="row ${active ? "active" : ""}" data-index="${index}">
        <span class="rank">${index + 1}</span>
        <span class="pair"><b>${result.dep}</b> → <b>${result.arr}</b>
          <small>${result.depName} to ${result.arrName}</small></span>
        <span class="stats">
          <span class="pct ${cls}">${pct}%</span>
          <span class="meta">${Math.floor(result.block / 60)}h${String(result.block % 60).padStart(2, "0")} · ${result.distance} nm</span>
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
      <span>${outcome.aircraft}</span>
    </div>${hint}${rows}`;

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
    const atc = (payload.controllers || []).filter((c) => c.facility > 0);
    live = {
      controllers: atc.length,
      pilots: (payload.pilots || []).length,
      updated: payload.general?.update_timestamp,
    };
  } catch (error) {
    live = null;
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
  statusEl.textContent =
    `forecast ${age} old · ${meta.days_observed} days observed · ` +
    `${meta.positions_modelled} positions${liveText}`;
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
  const at = params.get("at");
  if (at) {
    const parsed = new Date(at);
    if (!Number.isNaN(parsed.getTime())) suggestion = parsed;
  }
}

function writeHash(query) {
  const params = new URLSearchParams();
  if (query.from) params.set("from", query.from);
  if (query.to) params.set("to", query.to);
  params.set("aircraft", query.aircraft);
  params.set("block", String(query.blockMinutes));
  if (suggestion) params.set("at", `${suggestion.toISOString().slice(0, 16)}Z`);
  const next = params.toString();
  try {
    history.replaceState(null, "", `${location.pathname}${location.search}#${next}`);
  } catch (error) {
    /* file:// and some embeds refuse history changes; the page still works */
  }
}

// ---- boot ------------------------------------------------------------------

async function boot() {
  try {
    const bundle = await loadBundle();
    planner = createPlanner(bundle);
    populateSelects();
    controls.when.value = "now";
    readHash();
    updateStatus();
    recompute();
  } catch (error) {
    statusEl.textContent = `failed to load forecast: ${error.message}`;
    console.error(error);
    return;
  }

  controls.when.addEventListener("input", () => {
    suggestion = null;
    suggestionInfo = null;
    recompute();
  });
  for (const key of ["block", "from", "to", "aircraft", "atc", "duration", "popularity"]) {
    controls[key].addEventListener("input", recompute);
  }
  controls.refresh.addEventListener("click", () => {
    refreshLive();
    recompute();
  });
  window.addEventListener("resize", () => drawMap(departureTime()));

  refreshLive();
  setInterval(refreshLive, 120000);
  setInterval(recompute, 60000); // the clock moves; the answer should too
}

boot();
