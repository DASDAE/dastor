/* dastor viewer. Plain JavaScript, no dependencies.
 * World coordinates: x is the product's x dim (seconds since the epoch, UTC, when it is a time),
 * y its other dim. Screen: x to the right, y up.
 */
"use strict";

// ------------------------------------------------------------------ constants
const API = "../v1";
const TILE = 256;
const M = { l: 80, r: 78, t: 8, b: 66 };  // plot margins (CSS px): the colour bar on the right, the overview strip below
const BAR = { gap: 12, w: 14 };            // the colour bar, right of the plot
const STRIP = { top: 46, h: 10 };          // the overview strip, below the x axis
const MAX_INFLIGHT = 6;
const TILE_BYTE_CAP = 320 * 2 ** 20;
// Annotation colours, each drawn over a dark edge. None is the gold of a selection, and none
// sits near the bright end of the colour maps, where the events are.
const PALETTE = ["#ff5d73", "#ffffff", "#ff9d42", "#6ef0c2", "#9fd4ff", "#d9a6ff"];
const THEME = { blue: "#3978d6", gold: "#ffc934", red: "#d0002a", steel: "#c6c7cb" };
const STAMPS = ["created_time", "modified_time", "modified_by", "derived_from", "derived_revision"];
const CMAPS = {
  viridis: ["#440154", "#482878", "#3e4989", "#31688e", "#26828e", "#1f9e89", "#35b779", "#6ece58", "#b5de2b", "#fde725"],
  magma: ["#000004", "#180f3d", "#440f76", "#721f81", "#9e2f7f", "#cd4071", "#f1605d", "#fd9668", "#feca8d", "#fcfdbf"],
  gray: ["#000000", "#ffffff"],
  "gray (inverted)": ["#ffffff", "#000000"],
  "blue-white-red": ["#053061", "#2166ac", "#4393c3", "#d1e5f0", "#f7f7f7", "#fddbc7", "#d6604d", "#b2182b", "#67001f"],
};

// ------------------------------------------------------------------ state
const S = {
  products: [], groups: new Map(), group: null,
  limits: { max_tile_bytes: Infinity, max_view_bytes: Infinity },
  sync: false,                    // whether selected areas can be synced into a local data directory
  levels: [], pin: null,          // the ladder, coarsest first: [{p, sx, sy}]
  x: "", y: "", xTime: false, xUnits: "", yUnits: "", ext: null,
  view: { x0: 0, spp: 1, y0: 0, ypp: 1 },
  segs: [], gapD: 0,
  disp: { stat: "max", scale: "linear", vmin: 0, vmax: 1, cmap: "viridis", smooth: false },
  lut: null, lutVer: 0, autoPending: true, autoFor: null, manual: false,
  me: null, annotations: false, sets: [], active: null,
  sel: null,                      // {set, id}: the one annotation being edited
  many: [],                       // [{set, id}]: several picked together, by a selection window
  mode: "select", draft: null, area: null, space: false,
  undo: [], redo: [],
  lastLabel: "",                  // the label last given, offered for the next annotation
  history: [],                    // views left by a jump (fit, a crowd, the strip, a pin), for "back"
  saveError: null, conflict: null, listError: null, annError: null,  // what went wrong, shown until it is put right
  mouse: null, drag: null, blocked: null,
  refused: null,                  // a product the server refused a tile of, at this view
  clusters: [],                   // aggregated features of the last frame
};
const tiles = new Map();          // key -> {st, data, cv, ver, key, ...}
let queue = [], inflight = 0, tileBytes = 0, frameNo = 0;

const $ = (id) => document.getElementById(id);
const cv = $("cv"), ctx = cv.getContext("2d");

// ------------------------------------------------------------------ utilities
function clamp(v, a, b) { return Math.max(a, Math.min(b, v)); }
function esc(s) { return String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c])); }
function toast(msg, ms = Math.max(2500, 60 * String(msg).length)) {  // long enough to read
  const el = $("toast"); el.textContent = msg; el.style.display = "block";
  clearTimeout(toast.timer); toast.timer = setTimeout(() => (el.style.display = "none"), ms);
}
// a list as a value repeats its key
function query(o) { return Object.entries(o).flatMap(([k, v]) => [].concat(v).map((x) => `${encodeURIComponent(k)}=${encodeURIComponent(x)}`)).join("&"); }
async function api(path, opt) {
  const r = await fetch(API + path, opt);
  if (r.status === 204) return null;
  const j = await r.json().catch(() => ({}));
  if (!r.ok) { const e = new Error(typeof j.detail === "string" ? j.detail : `HTTP ${r.status}`); e.status = r.status; e.body = j; throw e; }
  return j;
}
// ISO 8601 text (no zone, UTC) <-> seconds since the epoch
function parseTime(s) {
  const [head, tail] = String(s).replace(/Z$/, "").split(".");
  return Date.parse(head + "Z") / 1000 + (tail ? parseFloat("0." + tail) : 0);
}
function isoOf(t) {
  let sec = Math.floor(t), micro = Math.round((t - sec) * 1e6);
  if (micro >= 1e6) { sec += 1; micro = 0; }
  return new Date(sec * 1000).toISOString().slice(0, 19) + "." + String(micro).padStart(6, "0");
}
function xWire(v) { return S.xTime ? isoOf(v) : v; }
function xRead(v) { return S.xTime ? parseTime(v) : +v; }
function fmtX(v) { return S.xTime ? isoOf(v).replace("T", " ").slice(0, 23) : fmtNum(v); }
function fmtNum(v) { return Math.abs(v) >= 1e5 || (v !== 0 && Math.abs(v) < 1e-3) ? v.toExponential(3) : String(+v.toPrecision(6)); }
function fmtStep(v, timelike) {
  if (!timelike) return fmtNum(v);
  if (v >= 3600) return fmtNum(v / 3600) + " h";
  if (v >= 60) return fmtNum(v / 60) + " min";
  if (v >= 1) return fmtNum(v) + " s";
  return fmtNum(v * 1000) + " ms";
}
function fmtBytes(n) {
  const units = ["B", "kB", "MB", "GB", "TB"]; let i = 0;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
  return `${n.toFixed(n >= 100 || i === 0 ? 0 : 1)} ${units[i]}`;
}
function newId() { return "f" + Math.random().toString(36).slice(2, 10); }

// ------------------------------------------------------------------ geometry
let PLOT = null;  // measured on resize, since reading the layout for every vertex is slow
function plotRect() {
  if (!PLOT) PLOT = { x: M.l, y: M.t, w: Math.max(cv.clientWidth - M.l - M.r, 10), h: Math.max(cv.clientHeight - M.t - M.b, 10) };
  return PLOT;
}
// x axis: display x = real x minus a per-segment offset; the gaps between recorded segments are
// squeezed to S.gapD display units, so display x is continuous and monotone. With no segments it is x.
function segIndex(t) { let lo = 0, hi = S.segs.length - 1, k = -1; while (lo <= hi) { const m = (lo + hi) >> 1; if (S.segs[m].a <= t) { k = m; lo = m + 1; } else hi = m - 1; } return k; }
function tau(t) {
  const sg = S.segs; if (!sg.length) return t;
  const k = segIndex(t);
  if (k < 0) return t - sg[0].off;
  const s = sg[k];
  if (t <= s.b || k + 1 >= sg.length) return t - s.off;
  return s.b - s.off + ((t - s.b) / (sg[k + 1].a - s.b)) * S.gapD;
}
function untau(u) {
  const sg = S.segs; if (!sg.length) return u;
  let k = -1; for (let i = 0; i < sg.length; i++) if (sg[i].a - sg[i].off <= u) k = i; else break;
  if (k < 0) return u + sg[0].off;
  const s = sg[k], u1 = s.b - s.off;
  if (u <= u1 || k + 1 >= sg.length) return u + s.off;
  return s.b + (Math.min(u - u1, S.gapD) / S.gapD) * (sg[k + 1].a - s.b);
}
function x2px(t) { return M.l + (tau(t) - S.view.x0) / S.view.spp; }
function x2pxSeg(t, off) { return M.l + (t - off - S.view.x0) / S.view.spp; }
function y2px(d) { const P = plotRect(); return P.y + P.h - (d - S.view.y0) / S.view.ypp; }
function px2x(px) { return untau(S.view.x0 + (px - M.l) * S.view.spp); }
function px2y(py) { const P = plotRect(); return S.view.y0 + (P.y + P.h - py) * S.view.ypp; }
function viewEnd() { const P = plotRect(); return { x1: S.view.x0 + P.w * S.view.spp, y1: S.view.y0 + P.h * S.view.ypp }; }
// recorded pieces in view: [{off, a, b}] in real x
function pieces() {
  const u0 = S.view.x0, u1 = viewEnd().x1;
  if (!S.segs.length) return S.ext ? [{ off: 0, a: Math.max(u0, S.ext.x0), b: Math.min(u1, S.ext.x1) }].filter((p) => p.b > p.a) : [];
  const out = [];
  for (const s of S.segs) { const a = Math.max(s.a, u0 + s.off), b = Math.min(s.b, u1 + s.off); if (b > a) out.push({ off: s.off, a, b }); }
  return out;
}
function displaySpan() { return S.segs.length ? tau(S.segs[S.segs.length - 1].b) - tau(S.segs[0].a) : S.ext.x1 - S.ext.x0; }
function clampView() {
  const v = S.view, fine = S.levels[S.levels.length - 1];
  if (!fine || !S.ext) return;
  v.spp = clamp(v.spp, fine.sx / 16, Math.max(displaySpan() / 50, fine.sx));
  v.ypp = clamp(v.ypp, fine.sy / 16, Math.max((S.ext.y1 - S.ext.y0) / 50, fine.sy));
}
// Keeps a tenth of the plot over the data, so paging or dragging can't lose it. True if it moved the view.
function clampPos() {
  if (!S.ext) return false;
  const v = S.view, P = plotRect(), a = tau(S.ext.x0), b = a + displaySpan(), W = P.w * v.spp, H = P.h * v.ypp;
  const x = clamp(v.x0, a - 0.9 * W, b - 0.1 * W), y = clamp(v.y0, S.ext.y0 - 0.9 * H, S.ext.y1 - 0.1 * H);
  const hit = Math.abs(x - v.x0) > W * 1e-9 || Math.abs(y - v.y0) > H * 1e-9;
  v.x0 = x; v.y0 = y;
  return hit;
}
// The views a jump left behind, so there is a way back from fit, a crowd, the strip or a pin.
function pushView() { S.history.push({ ...S.view }); if (S.history.length > 30) S.history.shift(); }
function backView() {
  const v = S.history.pop();
  if (!v) { toast("no earlier view"); return; }
  moved(); Object.assign(S.view, v); redraw();
}
function wholeView() {
  if (!S.ext) return;
  const P = plotRect(), a = tau(S.ext.x0), span = displaySpan(), h = S.ext.y1 - S.ext.y0;
  S.view.spp = (span * 1.02) / P.w; S.view.x0 = a - span * 0.01;
  S.view.ypp = (h * 1.02) / P.h; S.view.y0 = S.ext.y0 - h * 0.01;
  clampView(); redraw();
}
function moved() { lastMove = performance.now(); S.refused = null; }
function zoomAt(px, py, fx, fy) {
  moved();
  const u = S.view.x0 + (px - M.l) * S.view.spp, d = px2y(py), P = plotRect();
  S.view.spp *= fx; S.view.ypp *= fy; clampView();
  S.view.x0 = u - (px - M.l) * S.view.spp;
  S.view.y0 = d - (P.y + P.h - py) * S.view.ypp;
  redraw();
}

// ------------------------------------------------------------------ products and levels
function groupKey(p) { return p.attrs.acquisition_key || ""; }
function levelName(p) { return Object.entries(p.attrs).filter(([k]) => k !== "acquisition_key").map(([, v]) => v).join(" ") || "(untagged)"; }
function buildGroups() {
  S.groups = new Map();
  for (const p of S.products) {
    if (!p.viewable) continue;
    const k = groupKey(p);
    if (!S.groups.has(k)) S.groups.set(k, []);
    S.groups.get(k).push(p);
  }
  const sel = $("selGroup"); sel.innerHTML = ""; sel.hidden = S.groups.size < 2;  // nothing to choose between
  for (const k of S.groups.keys()) sel.add(new Option(k || "(no acquisition key)", k));
}
function groupProducts() {
  const all = S.groups.get(S.group) || [];
  return all.filter((p) => p.x === all[0].x && p.y === all[0].y);
}
function ladderKey() { return "dastor.ladder." + S.group; }
function chosenIds() {
  try { const ids = JSON.parse(localStorage.getItem(ladderKey())); if (Array.isArray(ids)) return new Set(ids); } catch (e) { /* default below */ }
  const seen = new Set(), ids = new Set();  // by default one product for each resolution
  for (const p of groupProducts()) {
    const k = `${p.coords[p.x].step}|${p.coords[p.y].step}`;
    if (!seen.has(k)) { seen.add(k); ids.add(p.id); }
  }
  return ids;
}
async function setGroup(key) {
  await save();  // edits of the recording being left
  S.group = key; $("selGroup").value = key;
  const all = groupProducts();
  S.x = all[0].x; S.y = all[0].y;
  S.xTime = all[0].coords[S.x].timelike;
  S.xUnits = all[0].coords[S.x].units; S.yUnits = all[0].coords[S.y].units;
  S.pin = null; S.sel = null; S.many = []; S.area = null; S.areaProducts = null; S.draft = null;
  buildLadder();
  wholeView();
  await loadSets();
}
function buildLadder() {
  const ids = chosenIds();
  let chosen = groupProducts().filter((p) => ids.has(p.id));
  if (!chosen.length) chosen = groupProducts().slice(0, 1);
  S.levels = chosen.map((p) => ({ p, sx: p.coords[S.x].step, sy: p.coords[S.y].step }))
    .sort((a, b) => b.sx - a.sx || b.sy - a.sy);
  if (S.pin && !S.levels.some((l) => l.p.id === S.pin)) S.pin = null;
  // extent and recorded segments: the union over the ladder
  const rd = (v) => xRead(v);
  let x0 = Infinity, x1 = -Infinity, y0 = Infinity, y1 = -Infinity; const spans = [];
  for (const l of S.levels) {
    const cx = l.p.coords[S.x], cy = l.p.coords[S.y];
    x0 = Math.min(x0, rd(cx.min)); x1 = Math.max(x1, rd(cx.max) + l.sx);
    y0 = Math.min(y0, cy.min); y1 = Math.max(y1, cy.max + l.sy);
    for (const [a, b] of cx.spans) spans.push([rd(a), rd(b) + l.sx]);
  }
  S.ext = { x0, x1, y0, y1 };
  spans.sort((a, b) => a[0] - b[0]);
  const merged = [];
  for (const [a, b] of spans) { const last = merged[merged.length - 1]; if (last && a <= last[1]) last[1] = Math.max(last[1], b); else merged.push([a, b]); }
  const recorded = merged.reduce((n, s) => n + s[1] - s[0], 0);
  S.gapD = recorded * 0.04;
  // gaps no wider than their squeezed width stay as they are
  const segs = [];
  for (const [a, b] of merged) { const last = segs[segs.length - 1]; if (last && a - last.b <= S.gapD) last.b = b; else segs.push({ a, b, off: 0 }); }
  for (let k = 1; k < segs.length; k++) segs[k].off = segs[k - 1].off + (segs[k].a - segs[k - 1].b - S.gapD);
  S.segs = S.xTime && segs.length > 1 ? segs : [];
  S.autoPending = true;
  renderLevels();
}
function zooms(l) {
  return [Math.max(0, Math.round(Math.log2(S.view.spp / l.sx))), Math.max(0, Math.round(Math.log2(S.view.ypp / l.sy)))];
}
function tileSpan(l, zx, zy) { return [TILE * l.sx * 2 ** zx, TILE * l.sy * 2 ** zy]; }
// The array bytes the server reads for one tile and for the tiles of this view at a level.
function levelCost(l) {
  const [zx, zy] = zooms(l), item = (parseInt((l.p.dtype.match(/\d+$/) || ["64"])[0], 10) || 64) / 8;
  const tile = (TILE * 2 ** zx + 1) * (TILE * 2 ** zy + 1) * item;  // the server counts both ends
  return { tile, view: tile * visibleTiles(l, zx, zy).length };
}
function overLimits(l) { const c = levelCost(l); return c.tile > S.limits.max_tile_bytes || c.view > S.limits.max_view_bytes; }
// The level drawn: the pinned one, else the coarsest with at least one sample for each screen
// pixel along x (the finest if none has). If the view would read more of it than the limits
// allow, the nearest coarser level within them stands in; with none, S.blocked is the level
// and only an outline of its data is drawn.
let levelMemo = { key: "", level: null, blocked: null };
function activeLevel() {
  const v = S.view, P = plotRect();
  const key = [v.x0, v.spp, v.y0, v.ypp, P.w, P.h, S.pin, S.refused, ...S.levels.map((l) => l.p.id)].join("|");
  if (key !== levelMemo.key) levelMemo = { key, level: pickLevel(), blocked: S.blocked };
  S.blocked = levelMemo.blocked;
  return levelMemo.level;
}
function pickLevel() {
  S.blocked = null;
  if (!S.levels.length) return null;
  let i = S.levels.findIndex((l) => l.sx <= S.view.spp * 1.0001);
  if (i < 0) i = S.levels.length - 1;
  const pinned = S.pin ? S.levels.findIndex((l) => l.p.id === S.pin) : -1;
  if (pinned >= 0) i = pinned;
  else for (let k = i; k >= 0; k--) if (!overLimits(S.levels[k])) return S.levels[k];
  if (overLimits(S.levels[i]) || S.refused === S.levels[i].p.id) S.blocked = S.levels[i];
  return S.levels[i];
}
function stepText(p) { return `${fmtStep(p.coords[S.x].step, S.xTime)} × ${fmtNum(p.coords[S.y].step)} ${S.yUnits || S.y}`; }
function renderLevels() {
  const ids = new Set(S.levels.map((l) => l.p.id));
  const all = groupProducts().slice().sort((a, b) => b.coords[S.x].step - a.coords[S.x].step || levelName(a).localeCompare(levelName(b)));
  $("levels").innerHTML = all.map((p) =>
    `<label class="level"><input type="checkbox" data-id="${p.id}" ${ids.has(p.id) ? "checked" : ""}>` +
    `<span class="name" title="${esc(JSON.stringify(p.attrs))}">${esc(levelName(p))}</span><span class="step">${esc(stepText(p))}</span></label>`).join("");
  // the status bar's control: auto follows the zoom, a level holds the view on it
  $("levelBar").innerHTML = '<button data-level="" title="follow the zoom">auto</button>' +
    S.levels.map((l) => `<button data-level="${l.p.id}" title="hold the view on ${esc(levelName(l.p))} (${esc(stepText(l.p))})">${esc(levelName(l.p))}</button>`).join("");
  updateLevelHighlight();
}
function setPin(id) {
  if (id && !S.levels.some((l) => l.p.id === id)) return;
  S.pin = S.pin === id ? null : id || null; S.autoPending = true;
  updateLevelHighlight(); redraw();
}
function updateLevelHighlight() {
  const shown = S.levels.length ? activeLevel() : null;
  for (const el of $("levels").children) el.classList.toggle("shown", !!shown && el.querySelector("input").dataset.id === shown.p.id);
  for (const b of $("levelBar").children) {
    const id = b.dataset.level;
    b.classList.toggle("on", !id && !S.pin);                          // auto: following the zoom
    b.classList.toggle("shown", !!id && !!shown && shown.p.id === id);  // the level on screen now
    b.classList.toggle("pinned", !!id && S.pin === id);
    b.setAttribute("aria-pressed", String(id ? S.pin === id : !S.pin));
  }
}

// ------------------------------------------------------------------ tiles
function tileKey(l, zx, zy, ix, iy) { return `${l.p.id}|${S.disp.stat}|${zx}|${zy}|${ix}|${iy}`; }
function visibleTiles(l, zx, zy, pc) {
  if (!pc) { const all = []; for (const p of pieces()) for (const t of visibleTiles(l, zx, zy, p)) all.push(t); return all; }
  const [tx, ty] = tileSpan(l, zx, zy), e = viewEnd(), out = [];
  const ya = Math.max(S.view.y0, S.ext.y0), yb = Math.min(e.y1, S.ext.y1);
  if (pc.b <= pc.a || yb <= ya) return out;
  for (let ix = Math.floor(pc.a / tx); ix * tx < pc.b; ix++)
    for (let iy = Math.floor(ya / ty); iy * ty < yb; iy++) out.push([ix, iy]);
  return out;
}
// While the view is moving (a wheel turn, a drag) requests wait a moment, so a zoom through
// ten scales does not start ten screens of tiles the server would still finish.
const SETTLE_MS = 120;
let lastMove = 0, settleTimer = 0, settling = false;
function requestTiles() {
  const l = activeLevel(); if (!l) return;
  if (S.blocked) { queue = []; settling = false; return; }
  const wait = lastMove + SETTLE_MS - performance.now();
  settling = wait > 0;
  if (settling) { clearTimeout(settleTimer); settleTimer = setTimeout(redraw, wait); return; }
  const [zx, zy] = zooms(l), [tx, ty] = tileSpan(l, zx, zy);
  const vis = visibleTiles(l, zx, zy), e = viewEnd();
  const xc = untau((S.view.x0 + e.x1) / 2), yc = (S.view.y0 + e.y1) / 2;
  const want = [], keys = new Set();
  for (const [ix, iy] of vis) {
    const k = tileKey(l, zx, zy, ix, iy); keys.add(k);
    const had = tiles.get(k);
    if (had && !(had.st === "error" && had.retryAt <= Date.now())) continue;
    if (had) tiles.delete(k);
    want.push({ k, l, zx, zy, ix, iy, stat: S.disp.stat, tries: had ? had.tries : 0, pri: Math.abs((ix + 0.5) * tx - xc) / tx + Math.abs((iy + 0.5) * ty - yc) / ty });
  }
  want.sort((a, b) => a.pri - b.pri);
  queue = want;  // requests for tiles which left the view are dropped
  for (const t of tiles.values()) if (t.st === "loading" && t.ctrl && !keys.has(t.key)) { t.aborted = true; t.ctrl.abort(); }
  pump();
}
function pump() {
  while (inflight < MAX_INFLIGHT && queue.length) {
    const r = queue.shift();
    if (tiles.has(r.k)) continue;
    const [tx, ty] = tileSpan(r.l, r.zx, r.zy);
    const tile = { st: "loading", key: r.k, pid: r.l.p.id, stat: r.stat, tries: r.tries, zx: r.zx, zy: r.zy, ix: r.ix, iy: r.iy, tx, ty, used: frameNo };
    tiles.set(r.k, tile); inflight++;
    const url = `${API}/viewer/tile?` + query({
      product: r.l.p.id, stat: r.stat, width: TILE, height: TILE,
      [`${S.x}_min`]: xWire(r.ix * tx), [`${S.x}_max`]: xWire((r.ix + 1) * tx),
      [`${S.y}_min`]: r.iy * ty, [`${S.y}_max`]: (r.iy + 1) * ty,
    });
    tile.ctrl = new AbortController();
    fetch(url, { signal: tile.ctrl.signal }).then(async (resp) => {
      if (resp.status === 204) { tile.st = "empty"; return; }
      if (resp.status === 413) { tile.st = "empty"; S.refused = tile.pid; return; }  // more than the estimate
      if (!resp.ok) { tile.st = "error"; tile.err = await resp.text(); return; }
      tile.data = new Float32Array(await resp.arrayBuffer());
      tile.st = tile.data.length === TILE * TILE ? "ok" : "error";
      tile.bytes = tile.data.byteLength + 200; tileBytes += tile.bytes;
    }).catch((e) => { tile.st = "error"; tile.err = String(e); })
      .finally(() => {
        inflight--; tile.ctrl = null;
        if (tile.aborted) tiles.delete(r.k);  // it only left the view: asked for again when it returns
        else if (tile.st === "error") {       // kept, and asked for again after a growing wait
          tile.tries += 1;
          const wait = Math.min(60000, 2000 * 2 ** (tile.tries - 1));
          tile.retryAt = Date.now() + wait; setTimeout(redraw, wait + 50);
        }
        redraw(); pump();
      });
  }
  if (tileBytes > TILE_BYTE_CAP) {  // least recently drawn first, never the tiles of this frame
    const old = [...tiles.values()].filter((t) => t.st !== "loading" && t.used !== frameNo).sort((a, b) => a.used - b.used);
    for (const t of old) { if (tileBytes <= TILE_BYTE_CAP * 0.85) break; tiles.delete(t.key); tileBytes -= t.bytes || 0; }
  }
}
function shade(v) { return S.disp.scale === "log" ? Math.log10(Math.abs(v)) : v; }
function buildLut() {
  const stops = (CMAPS[S.disp.cmap] || CMAPS.viridis).map((h) => [1, 3, 5].map((i) => parseInt(h.slice(i, i + 2), 16)));
  const lut = new Uint32Array(256), le = new Uint8Array(new Uint32Array([1]).buffer)[0] === 1;
  for (let i = 0; i < 256; i++) {
    const f = (i / 255) * (stops.length - 1), k = Math.min(Math.floor(f), stops.length - 2), w = f - k;
    const [r, g, b] = [0, 1, 2].map((c) => Math.round(stops[k][c] * (1 - w) + stops[k + 1][c] * w));
    lut[i] = le ? (255 << 24) | (b << 16) | (g << 8) | r : (r << 24) | (g << 16) | (b << 8) | 255;
  }
  S.lut = lut; S.lutVer++;
}
// The one place values become colours, so a GPU renderer can replace it.
function tileCanvas(tile) {
  if (tile.cv && tile.ver === S.lutVer) return tile.cv;
  if (!tile.cv) { tile.cv = document.createElement("canvas"); tile.cv.width = TILE; tile.cv.height = TILE; tile.bytes += TILE * TILE * 4; tileBytes += TILE * TILE * 4; }
  const c2 = tile.cv.getContext("2d"), img = c2.createImageData(TILE, TILE);
  const px = new Uint32Array(img.data.buffer), d = tile.data, lut = S.lut;
  const { vmin, vmax } = S.disp, k = 255.999 / (vmax - vmin || 1e-12), log = S.disp.scale === "log";
  for (let row = 0; row < TILE; row++) {
    const src = (TILE - 1 - row) * TILE, dst = row * TILE;  // the lowest y is the bottom row
    for (let col = 0; col < TILE; col++) {
      let v = d[src + col];
      if (v !== v) { px[dst + col] = 0; continue; }
      if (log) v = Math.log10(Math.abs(v));
      const s = (v - vmin) * k;
      px[dst + col] = lut[s < 0 ? 0 : s > 255 ? 255 : s | 0];
    }
  }
  c2.putImageData(img, 0, 0);
  tile.ver = S.lutVer;
  return tile.cv;
}
function drawTile(tile, off) {
  const xa = x2pxSeg(tile.ix * tile.tx, off), xb = x2pxSeg((tile.ix + 1) * tile.tx, off);
  const yt = y2px((tile.iy + 1) * tile.ty), yb = y2px(tile.iy * tile.ty);
  ctx.drawImage(tileCanvas(tile), xa, yt, xb - xa, yb - yt);
  tile.used = frameNo;
}
// Returns the tiles of the level still to come. While any are, cached tiles of other levels and
// zooms are drawn under them, coarse first, so the view is never blank.
function drawTiles(P) {
  const l = activeLevel(), [zx, zy] = zooms(l), ps = pieces();
  let pending = 0, failed = 0;
  const missing = !!S.blocked || visibleTiles(l, zx, zy).some(([ix, iy]) => { const t = tiles.get(tileKey(l, zx, zy, ix, iy)); return !t || t.st === "loading"; });
  let others = [];
  if (missing) {
    const ids = new Set(S.levels.map((x) => x.p.id));
    others = [...tiles.values()].filter((t) => t.st === "ok" && t.stat === S.disp.stat && ids.has(t.pid) &&
      !(t.pid === l.p.id && t.zx === zx && t.zy === zy) && t.tx / S.view.spp > 6 && t.ty / S.view.ypp > 6)
      .sort((a, b) => b.tx - a.tx);
  }
  for (const pc of ps) {
    const xa = x2pxSeg(pc.a, pc.off), xb = x2pxSeg(pc.b, pc.off);
    ctx.save(); ctx.beginPath(); ctx.rect(xa, P.y, xb - xa, P.h); ctx.clip();
    for (const t of others) if ((t.ix + 1) * t.tx > pc.a && t.ix * t.tx < pc.b) drawTile(t, pc.off);
    for (const [ix, iy] of S.blocked ? [] : visibleTiles(l, zx, zy, pc)) {
      const t = tiles.get(tileKey(l, zx, zy, ix, iy));
      if (!t || t.st === "loading") { pending++; continue; }
      t.used = frameNo;
      if (t.st === "error") failed++;
      if (t.st === "ok") drawTile(t, pc.off);
    }
    ctx.restore();
  }
  if (S.blocked) drawOutline(l, P);
  S.failed = failed;
  return pending;
}
// Where a level has data, for a view too large to load it: an outline of each recorded span.
function drawOutline(l, P) {
  const cx = l.p.coords[S.x], cy = l.p.coords[S.y];
  const ya = y2px(cy.max + l.sy), yb = y2px(cy.min);
  ctx.setLineDash([2, 4]); ctx.lineWidth = 1.5; ctx.strokeStyle = THEME.steel; ctx.fillStyle = "rgba(198,199,203,0.06)";
  for (const [a, b] of cx.spans) {
    const xa = x2px(xRead(a)), xb = x2px(xRead(b) + l.sx);
    if (xb < P.x || xa > P.x + P.w) continue;
    ctx.fillRect(xa, ya, xb - xa, yb - ya); ctx.strokeRect(xa, ya, Math.max(xb - xa, 1), yb - ya);
  }
  ctx.setLineDash([]);
}
function drawGaps(P) {
  const sg = S.segs;
  for (let k = 0; k + 1 < sg.length; k++) {
    const xa = x2px(sg[k].b), xb = x2px(sg[k + 1].a);
    if (xb < P.x || xa > P.x + P.w) continue;
    ctx.save(); ctx.beginPath(); ctx.rect(xa, P.y, xb - xa, P.h); ctx.clip();
    ctx.fillStyle = "#1b1d22"; ctx.fillRect(xa, P.y, xb - xa, P.h);
    ctx.strokeStyle = "rgba(255,200,80,0.25)"; ctx.lineWidth = 1;
    for (let x = xa - P.h; x < xb; x += 10) { ctx.beginPath(); ctx.moveTo(x, P.y + P.h); ctx.lineTo(x + P.h, P.y); ctx.stroke(); }
    ctx.restore();
    if (xb - xa > 14) {
      ctx.save(); ctx.translate((xa + xb) / 2, P.y + P.h / 2); ctx.rotate(-Math.PI / 2);
      ctx.fillStyle = "rgba(255,210,120,0.95)"; ctx.font = "14px system-ui"; ctx.textAlign = "center"; ctx.textBaseline = "middle";
      ctx.fillText(`no data ${fmtStep(sg[k + 1].a - sg[k].b, true)}`, 0, 0); ctx.restore();
    }
  }
}
function valueAt(x, y) {
  const l = activeLevel(); if (!l) return null;
  const [zx, zy] = zooms(l), [tx, ty] = tileSpan(l, zx, zy);
  const ix = Math.floor(x / tx), iy = Math.floor(y / ty), t = tiles.get(tileKey(l, zx, zy, ix, iy));
  if (!t || t.st !== "ok") return null;
  const col = clamp(Math.floor(((x - ix * tx) / tx) * TILE), 0, TILE - 1), row = clamp(Math.floor(((y - iy * ty) / ty) * TILE), 0, TILE - 1);
  const v = t.data[row * TILE + col];
  return v === v ? v : null;
}
function autoContrast(quiet) {
  const l = activeLevel(); if (!l) return false;
  const [zx, zy] = zooms(l), vals = [];
  for (const [ix, iy] of visibleTiles(l, zx, zy)) {
    const t = tiles.get(tileKey(l, zx, zy, ix, iy));
    if (!t || t.st !== "ok") continue;
    for (let i = 0; i < t.data.length; i += 7) { const v = shade(t.data[i]); if (Number.isFinite(v)) vals.push(v); }
  }
  if (vals.length < 10) { if (!quiet) toast("no data on screen yet"); return false; }
  vals.sort((a, b) => a - b);
  let lo = vals[Math.floor(vals.length * 0.01)], hi = vals[Math.floor(vals.length * 0.995)];
  if (S.disp.scale === "linear" && lo < 0) { hi = Math.max(-lo, hi); lo = -hi; }  // signed data: centred on zero
  if (hi <= lo) hi = lo + 1e-9;
  S.disp.vmin = +lo.toPrecision(3); S.disp.vmax = +hi.toPrecision(3);
  $("inpVmin").value = S.disp.vmin; $("inpVmax").value = S.disp.vmax;
  S.lutVer++; redraw();
  return true;
}

// ------------------------------------------------------------------ drawing
let rafPending = false;
function redraw() { if (!rafPending) { rafPending = true; requestAnimationFrame(draw); } }
function resize() {
  $("main").style.top = $("top").offsetHeight + "px";
  const dpr = window.devicePixelRatio || 1;
  PLOT = null;
  cv.width = Math.round(cv.clientWidth * dpr); cv.height = Math.round(cv.clientHeight * dpr);
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  redraw();
}
const T_STEPS = [0.001, 0.002, 0.005, 0.01, 0.02, 0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800,
  3600, 7200, 10800, 21600, 43200, 86400, 172800, 604800, 2592000];
function niceStep(raw, steps) { for (const s of steps) if (s >= raw) return s; return steps[steps.length - 1]; }
function niceNumber(raw) { const e = 10 ** Math.floor(Math.log10(raw)); for (const m of [1, 2, 5, 10]) if (m * e >= raw) return m * e; return 10 * e; }
function xTicks(step) {
  const out = [];
  for (const pc of pieces()) for (let t = Math.ceil(pc.a / step) * step; t < pc.b && out.length < 500; t += step) out.push(t);
  return out;
}
function fmtTick(t, step) {
  if (!S.xTime) return fmtNum(t);
  const s = new Date(Math.round(t * 1000)).toISOString();
  if (step >= 86400) return s.slice(0, 10);
  if (step >= 3600) return s.slice(5, 10) + " " + s.slice(11, 16);
  if (step >= 60) return s.slice(11, 16);
  return step < 1 ? s.slice(11, 23) : s.slice(11, 19);
}
function draw() {
  rafPending = false;
  const W = cv.clientWidth, H = cv.clientHeight, P = plotRect();
  ctx.fillStyle = "#101214"; ctx.fillRect(0, 0, W, H);
  if (!S.levels.length || !S.lut) {
    const text = S.listError ? `Could not reach the server: ${S.listError}. Click to try again.`
      : S.ready ? "No data to show: nothing here of two dims that you may read." : "loading...";
    ctx.fillStyle = S.listError ? "#ff8b96" : "#9aa0a6"; ctx.font = "15px system-ui"; ctx.textAlign = "left"; ctx.textBaseline = "alphabetic";
    ctx.fillText(text, 80, 40);
    return;
  }
  clampPos();
  requestTiles();
  frameNo++;
  ctx.save();
  ctx.beginPath(); ctx.rect(P.x, P.y, P.w, P.h); ctx.clip();
  ctx.fillStyle = "#000"; ctx.fillRect(P.x, P.y, P.w, P.h);
  ctx.imageSmoothingEnabled = S.disp.smooth;
  const shown = activeLevel().p.id;
  if (shown !== S.autoFor && !S.manual) S.autoPending = true;  // each product has its own range
  S.autoFor = shown;
  const pending = drawTiles(P);
  if (!pending && S.autoPending && autoContrast(true)) S.autoPending = false;
  drawGaps(P);
  drawAnnotations(P);
  if (S.blocked) drawBlocked(P);
  // notes, top right: what is wrong stays until it is put right; the rest is passing
  const notes = [];
  if (S.saveError) notes.push([`not saved (${S.saveError}); trying again`, true]);
  if (S.conflict) notes.push([`${S.conflict} was changed elsewhere and reloaded; your unsaved edits to it were dropped (Esc)`, true]);
  if (S.draft) notes.push([`${S.draft.kind}: Enter, double-click or right-click ends it · Esc cancels`, false]);
  if (S.failed) notes.push([`${S.failed} tile${S.failed > 1 ? "s" : ""} failed; trying again`, true]);
  ctx.font = "13px system-ui"; ctx.textAlign = "left"; ctx.textBaseline = "middle";
  notes.forEach(([txt, bad], i) => {
    const w = ctx.measureText(txt).width + 12;
    ctx.fillStyle = "rgba(20,22,26,0.9)"; ctx.fillRect(P.x + P.w - w - 6, P.y + 6 + i * 26, w, 22);
    ctx.fillStyle = bad ? "#ff8b96" : THEME.gold; ctx.fillText(txt, P.x + P.w - w, P.y + 17 + i * 26);
  });
  if (pending) { ctx.fillStyle = THEME.gold; ctx.fillRect(P.x, P.y, P.w * 0.25, 2); }  // a quiet sign that tiles are coming
  ctx.restore();
  drawAxes(P);
  drawColorBar(P);
  drawOverview(P);
  updateLevelHighlight();
  scheduleHash();
}
// A level the view is too large for: said in the middle of the plot, where the data would be.
function drawBlocked(P) {
  const lines = [`${levelName(S.blocked.p)} is too much to load at this zoom`,
    S.pin ? "Zoom in, or set the level (bottom right) to auto." : "Zoom in, or tick a coarser product under Levels."];
  ctx.font = "15px system-ui"; ctx.textAlign = "center"; ctx.textBaseline = "middle";
  const w = Math.max(...lines.map((t) => ctx.measureText(t).width)) + 28, x = P.x + P.w / 2, y = P.y + P.h / 2;
  ctx.fillStyle = "rgba(20,22,26,0.92)"; ctx.fillRect(x - w / 2, y - 30, w, 60);
  ctx.strokeStyle = THEME.gold; ctx.lineWidth = 1; ctx.strokeRect(x - w / 2 + 0.5, y - 29.5, w - 1, 59);
  ctx.fillStyle = THEME.gold; ctx.fillText(lines[0], x, y - 11);
  ctx.fillStyle = "#cfd3d8"; ctx.fillText(lines[1], x, y + 11);
}
// What the colours mean: the map from the high limit (top) to the low (bottom). A click on it
// sets the limits from what is on screen.
function barRect(P) { return { x: P.x + P.w + BAR.gap, y: P.y, w: BAR.w, h: P.h }; }
function drawColorBar(P) {
  const b = barRect(P), stops = CMAPS[S.disp.cmap] || CMAPS.viridis, g = ctx.createLinearGradient(0, b.y + b.h, 0, b.y);
  stops.forEach((c, i) => g.addColorStop(i / (stops.length - 1), c));
  ctx.fillStyle = g; ctx.fillRect(b.x, b.y, b.w, b.h);
  ctx.strokeStyle = "#777"; ctx.lineWidth = 1; ctx.strokeRect(b.x + 0.5, b.y + 0.5, b.w, b.h);
  const { vmin, vmax } = S.disp, n = 4;
  ctx.fillStyle = "#cfd3d8"; ctx.font = "12px system-ui"; ctx.textAlign = "left";
  for (let i = 0; i <= n; i++) {
    const y = b.y + b.h - (i / n) * b.h, v = vmin + (i / n) * (vmax - vmin);
    ctx.beginPath(); ctx.moveTo(b.x + b.w, y); ctx.lineTo(b.x + b.w + 3, y); ctx.stroke();
    ctx.textBaseline = i === 0 ? "bottom" : i === n ? "top" : "middle";
    ctx.fillText(fmtNum(+v.toPrecision(3)), b.x + b.w + 5, y);
  }
  ctx.textBaseline = "top"; ctx.fillStyle = "#9aa0a6";
  ctx.fillText(`${S.disp.scale === "log" ? "log10 " : ""}${S.disp.stat}`, b.x - 2, b.y + b.h + 6);
  if (S.manual) ctx.fillText("held", b.x - 2, b.y + b.h + 22);
}
// The whole recording in one strip under the axis: where there is data, where the annotations
// are, and where the window on screen sits. A press or drag on it moves the window.
function stripRect(P) { return { x: P.x, y: P.y + P.h + STRIP.top, w: P.w, h: STRIP.h }; }
function stripX(P, u) { return P.x + ((u - tau(S.ext.x0)) / displaySpan()) * P.w; }
function featureX(f) {
  if (f.pts && f.pts.length) return f.pts[0][0];
  if (f.box) return f.box.x0 === null || f.box.x1 === null ? null : (f.box.x0 + f.box.x1) / 2;
  const sh = f.rows ? rawShapes(f) : null;
  return sh && sh.pts.length ? sh.pts[0][0] : null;
}
function drawOverview(P) {
  const r = stripRect(P);
  ctx.fillStyle = "#3a3d42"; ctx.fillRect(r.x, r.y, r.w, r.h);
  ctx.fillStyle = "#15161a";
  for (let k = 0; k + 1 < S.segs.length; k++) {  // the squeezed gaps
    const xa = stripX(P, tau(S.segs[k].b)), xb = stripX(P, tau(S.segs[k + 1].a));
    ctx.fillRect(xa, r.y, Math.max(xb - xa, 1), r.h);
  }
  for (const s of S.sets) {
    if (!s.visible || !s.feats) continue;
    ctx.fillStyle = s.color;
    const every = Math.ceil(s.feats.length / 4000);  // enough marks to show where the work is
    for (let i = 0; i < s.feats.length; i += every) {
      const x = featureX(s.feats[i]);
      if (x !== null) ctx.fillRect(stripX(P, tau(x)) - 0.75, r.y + 1, 1.5, r.h - 2);
    }
  }
  const xa = clamp(stripX(P, S.view.x0), r.x, r.x + r.w), xb = clamp(stripX(P, viewEnd().x1), r.x, r.x + r.w);
  ctx.fillStyle = "rgba(255,201,52,0.28)"; ctx.fillRect(xa, r.y - 2, Math.max(xb - xa, 3), r.h + 4);
  ctx.strokeStyle = THEME.gold; ctx.lineWidth = 1.5; ctx.strokeRect(xa, r.y - 2, Math.max(xb - xa, 3), r.h + 4);
}
function drawAxes(P) {
  const e = viewEnd();
  ctx.fillStyle = "#cfd3d8"; ctx.strokeStyle = "#777"; ctx.lineWidth = 1; ctx.font = "13px system-ui, sans-serif";
  ctx.strokeRect(P.x + 0.5, P.y + 0.5, P.w, P.h);
  const xs = S.xTime ? niceStep(S.view.spp * 110, T_STEPS) : niceNumber(S.view.spp * 90);
  ctx.textAlign = "center"; ctx.textBaseline = "top";
  let lastX = -1e9;
  for (const t of xTicks(xs)) {
    const x = x2px(t);
    ctx.beginPath(); ctx.moveTo(x, P.y + P.h); ctx.lineTo(x, P.y + P.h + 4); ctx.stroke();
    if (x - lastX > 82) { ctx.fillText(fmtTick(t, xs), x, P.y + P.h + 6); lastX = x; }
  }
  const ps = pieces();
  ctx.textAlign = "left";
  if (S.xTime && ps.length) ctx.fillText(isoOf(ps[0].a).slice(0, 10) + " UTC", P.x, P.y + P.h + 24);
  ctx.textAlign = "right";
  ctx.fillText(S.x + (!S.xTime && S.xUnits ? ` (${S.xUnits})` : ""), P.x + P.w, P.y + P.h + 24);
  const ys = niceNumber(S.view.ypp * 50);
  ctx.textBaseline = "middle";
  for (let d = Math.ceil(S.view.y0 / ys) * ys; d < e.y1; d += ys) {
    const y = y2px(d);
    ctx.beginPath(); ctx.moveTo(P.x - 4, y); ctx.lineTo(P.x, y); ctx.stroke();
    ctx.fillText(fmtNum(+d.toPrecision(12)), P.x - 6, y);
  }
  ctx.save(); ctx.translate(12, P.y + P.h / 2); ctx.rotate(-Math.PI / 2); ctx.textAlign = "center";
  ctx.fillText(S.y + (S.yUnits ? ` (${S.yUnits})` : ""), 0, 0); ctx.restore();
}

// ------------------------------------------------------------------ annotation model
// A feature here: {id, kind, cols, pts | box | rows}.
//   point, group: pts [[x, y, extra]]   path, polygon: pts in order
//   box: {x0, x1, y0, y1}, a bound null where the feature spans that dim
//   raw: rows as stored, for anything else (drawn where possible, not reshaped)
function setKey(s) { return `${s.owner}/${s.name}`; }
function findSet(key) { return S.sets.find((s) => setKey(s) === key); }
function activeSet() { return S.active ? findSet(S.active) : null; }
function selected() {
  if (!S.sel) return null;
  const set = findSet(S.sel.set), f = set && set.feats && set.feats.find((x) => x.id === S.sel.id);
  return f ? { set, f } : null;
}
function featsFromDoc(doc) {
  const names = [S.x, S.y, `${S.x}_min`, `${S.x}_max`, `${S.y}_min`, `${S.y}_max`];
  const known = new Set(["feature_id", "seq", "part", "ring", ...names]);
  const has = (r, n) => r[n] !== null && r[n] !== undefined;
  const byId = new Map();
  for (const r of doc.annotations) { if (!byId.has(r.feature_id)) byId.set(r.feature_id, []); byId.get(r.feature_id).push(r); }
  const extra = (r) => Object.fromEntries(Object.entries(r).filter(([k, v]) => !known.has(k) && v !== null));
  const point = (r) => [xRead(r[S.x]), +r[S.y], extra(r)];
  return doc.features.map((rec) => {
    const rows = byId.get(rec.id) || [];
    const cols = Object.fromEntries(Object.entries(rec).filter(([k, v]) => k !== "id" && k !== "geometry" && v !== null));
    const f = { id: rec.id, cols };
    const points = rows.length > 0 && rows.every((r) => has(r, S.x) && has(r, S.y) && !names.slice(2).some((n) => has(r, n)));
    const simple = rows.every((r) => !(r.ring > 0) && !(r.part > 0));
    if ((rec.geometry === "path" || rec.geometry === "polygon") && points && simple) {
      f.kind = rec.geometry;
      f.pts = rows.slice().sort((a, b) => (a.seq ?? 0) - (b.seq ?? 0)).map(point);
    } else if (!rec.geometry && points) {
      f.kind = rows.length === 1 ? "point" : "group"; f.pts = rows.map(point);
    } else if (!rec.geometry && rows.length === 1 && !has(rows[0], S.x) && !has(rows[0], S.y) && names.slice(2).some((n) => has(rows[0], n))) {
      const r = rows[0], get = (n, read) => (has(r, n) ? read(r[n]) : null);
      f.kind = "box"; f.extra = extra(r);
      f.box = { x0: get(names[2], xRead), x1: get(names[3], xRead), y0: get(names[4], Number), y1: get(names[5], Number) };
    } else { f.kind = "raw"; f.geometry = rec.geometry; f.rows = rows; }
    return f;
  });
}
function docFromFeats(set) {
  const annotations = [], features = [];
  for (const f of set.feats) {
    const rec = { id: f.id, ...f.cols };
    if (f.kind === "path" || f.kind === "polygon") rec.geometry = f.kind;
    if (f.kind === "raw") { if (f.geometry) rec.geometry = f.geometry; for (const r of f.rows) annotations.push(r); }
    else if (f.kind === "box") {
      const r = { feature_id: f.id, ...(f.extra || {}) }, b = f.box;
      if (b.x0 !== null) r[`${S.x}_min`] = xWire(b.x0);
      if (b.x1 !== null) r[`${S.x}_max`] = xWire(b.x1);
      if (b.y0 !== null) r[`${S.y}_min`] = b.y0;
      if (b.y1 !== null) r[`${S.y}_max`] = b.y1;
      annotations.push(r);
    } else {
      const ordered = f.kind === "path" || f.kind === "polygon";
      f.pts.forEach((p, i) => annotations.push({ feature_id: f.id, ...(ordered ? { seq: i, part: 0, ring: 0 } : {}), [S.x]: xWire(p[0]), [S.y]: p[1], ...(p[2] || {}) }));
    }
    features.push(rec);
  }
  return { dims: set.dims, acquisition_key: set.acquisition_key || "", base_revision: set.revision, annotations, features };
}
async function loadSets() {
  let listing = null;
  S.annError = null;
  try { listing = await api("/annotations"); } catch (e) { if (e.status !== 404) S.annError = e.message; }  // 404: this server keeps none
  S.annotations = !!listing || !!S.annError; S.me = listing ? listing.owner : null;
  $("annBox").hidden = !S.annotations;
  const dims = new Set([S.x, S.y]), old = new Map(S.sets.map((s) => [setKey(s), s]));
  S.sets = (listing ? listing.sets : []).filter((s) => s.dims.length === 2 && s.dims.every((d) => dims.has(d)) &&
    (!s.acquisition_key || s.acquisition_key === S.group));
  S.sets = S.sets.map((s) => { const was = old.get(setKey(s)); return was && (was.dirty || was.saving) ? was : s; });  // unsaved edits stay
  S.sets.forEach((s) => {
    const was = old.get(setKey(s));
    if (was === s) return;
    let h = 0; for (const c of setKey(s)) h = (h * 31 + c.charCodeAt(0)) >>> 0;  // the same colour every visit,
    const used = new Set(S.sets.filter((o) => o !== s && o.color).map((o) => o.color));  // unless a set shown has it
    for (let i = 0; i < PALETTE.length; i++) { s.color = PALETTE[(h + i) % PALETTE.length]; if (!used.has(s.color)) break; }
    s.visible = was ? was.visible : true;
    if (was && was.revision === s.revision && was.feats) s.feats = was.feats;
  });
  await Promise.all(S.sets.filter((s) => s.visible && !s.feats).map(loadSet));
  if (!activeSet() || !activeSet().editable) { const mine = S.sets.find((s) => s.editable && s.feats); S.active = mine ? setKey(mine) : null; S.undo = []; S.redo = []; }
  renderSets(); renderSelected(); redraw();
}
async function loadSet(s) {
  try {
    const doc = await api(`/annotations/${s.owner}/${s.name}`);
    s.revision = doc.revision; s.dims = doc.dims; s.acquisition_key = doc.acquisition_key;
    s.feats = featsFromDoc(doc); s.failed = false;
  } catch (e) {  // unread, so never saved over: an empty list would replace what is stored
    s.feats = null; s.failed = true; s.visible = false;
    if (S.active === setKey(s)) S.active = null;
    toast(`could not read ${setKey(s)}: ${e.message}`, 5000);
  }
}
function renderSets() {
  $("sets").innerHTML = S.sets.map((s) => {
    const k = setKey(s), state = s.failed ? "could not be read" : k === S.active ? "being edited" : s.editable ? "click to edit" : "read only: copy from it into your own set";
    return `<div class="set ${k === S.active ? "active" : ""} ${s.editable ? "" : "locked"}"><input type="checkbox" data-show="${esc(k)}" ${s.visible ? "checked" : ""} title="show">` +
      `<span class="swatch" style="background:${s.color}"></span>` +
      `<span class="name" data-edit="${esc(k)}" title="${state}">${esc(k)}</span>` +
      `<span class="tag">${s.failed ? "unread" : s.editable ? "" : "\u{1F512}"}</span></div>`;
  }).join("");
  $("annNote").textContent = S.annError ? `Could not list the sets: ${S.annError}` : S.me ? "" : "Read only here: visitors have no name to save under.";
  const can = !!S.me;
  $("btnNewSet").disabled = !can; $("btnLoadSet").disabled = !can;
  $("btnDeleteSet").disabled = !activeSet(); $("btnSaveSet").disabled = !activeSet(); $("btnDownloadSet").disabled = !activeSet();
  for (const b of $("tools").children) {  // with nowhere to save, the drawing tools are off, and say why
    if (b.dataset.mode === "select" || b.dataset.mode === "pan") continue;
    b.disabled = !can; if (!can) b.title = `${b.getAttribute("aria-label")}: annotations can't be saved here`;
  }
  renderChip();
}
// The top bar's chip: the set being edited, and a dot which is hollow while edits are unsaved,
// filled once they are, and red while a save is failing.
function renderChip() {
  const s = activeSet(), busy = S.sets.some((x) => x.dirty || x.saving);
  $("setChip").hidden = !S.annotations;
  $("chipSwatch").style.background = s ? s.color : "transparent";
  $("chipName").textContent = s ? setKey(s) : "no set";
  $("chipDot").className = "dot " + (S.saveError ? "bad" : busy ? "busy" : "ok");
  $("setChip").title = S.saveError ? `not saved: ${S.saveError}` : busy ? "saving" : s ? "everything is saved" : "no annotation set is being edited";
}

// ------------------------------------------------------------------ editing and saving
function snapshot() { const s = activeSet(); return s ? JSON.stringify(s.feats) : "[]"; }
// Every edit of the active set goes through here: `fn` changes it, then it is saved.
function change(fn) {
  const s = activeSet(); if (!s) return;
  S.undo.push(snapshot()); if (S.undo.length > 100) S.undo.shift();
  S.redo = [];
  fn(s); touch();
}
function touch() { const s = activeSet(); s.dirty = true; renderChip(); clearTimeout(touch.timer); touch.timer = setTimeout(save, 600); renderSelected(); redraw(); }
function restore(from, to) {
  const s = activeSet(); if (!s || !from.length) return;
  to.push(snapshot()); s.feats = JSON.parse(from.pop()); touch();
}
// Saves every set with unsaved edits, whichever is being edited now, so an edit made just
// before switching set or recording is not lost with the switch.
async function save() {
  clearTimeout(touch.timer);
  await Promise.all(S.sets.filter((s) => s.dirty && s.feats).map(saveSet));
}
async function saveNow() {
  const s = activeSet(); if (!s) return;
  await s.saving;  // one under way counts: its edits are no longer marked unsaved
  await save();
  toast(S.saveError ? `not saved: ${S.saveError}` : `saved ${setKey(s)}`);
}
// The times and name the server stamped, written onto the annotations in place: the objects
// on screen (one may be mid-drag, or have a field being typed in) are never swapped.
function mergeStamps(s, doc) {
  const by = new Map(doc.features.map((r) => [r.id, r]));
  for (const f of s.feats) {
    const r = by.get(f.id); if (!r) continue;
    for (const k of ["created_time", "modified_time", "modified_by"]) if (r[k] !== null && r[k] !== undefined) f.cols[k] = r[k];
  }
}
async function saveSet(s) {
  if (s.saving) { await s.saving; if (!s.dirty) return; }
  s.dirty = false; renderChip();
  s.saving = (async () => {
    try {
      const out = await api(`/annotations/${s.owner}/${s.name}`, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(docFromFeats(s)) });
      s.revision = out.revision; S.saveError = null;
      if (!s.dirty) {
        const doc = await api(`/annotations/${s.owner}/${s.name}`);
        if (!s.dirty && doc.revision === s.revision) mergeStamps(s, doc);
      }
    } catch (e) {
      if (e.status === 409) {  // changed elsewhere: what is stored wins, and the plot says so until dismissed
        S.conflict = setKey(s); S.saveError = null; S.undo = []; S.redo = []; await loadSet(s);
      } else { s.dirty = true; S.saveError = e.message; touch.timer = setTimeout(save, 5000); }
    }
  })();
  await s.saving; s.saving = null;
  renderSets(); renderSelected(); redraw();
}
// A new, empty set of the caller's on this recording. Returns its key, or null if it exists.
async function createSet(name) {
  const first = groupProducts()[0];
  try {
    await api(`/annotations/${S.me}/${name}`, { method: "PUT", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ dims: first.dims, acquisition_key: S.group, base_revision: 0, annotations: [], features: [] }) });
  } catch (e) { if (e.status === 409) return null; throw e; }
  return `${S.me}/${name}`;
}
// "new set": with a name it is made; without, the name field opens for one.
async function newSet(name) {
  const row = $("newSetRow"), box = $("inpNewSet");
  if (!name) { row.hidden = false; box.value = ""; box.focus(); return; }
  let key;
  try { key = await createSet(name); } catch (e) { toast(e.message); return; }
  if (!key) { toast(`${S.me}/${name} exists already`); return; }
  row.hidden = true;
  await save();
  S.active = key; S.undo = []; S.redo = [];
  await loadSets();
}
// Something to draw into: the set being edited, else a new one made without asking.
async function ensureSet() {
  if (activeSet()) return true;
  if (!S.me) { toast("Annotations can't be saved here."); return false; }
  try {
    for (let n = 1; n < 50; n++) {
      const key = await createSet(n === 1 ? "annotations" : `annotations_${n}`);
      if (!key) continue;
      S.active = key; S.undo = []; S.redo = [];
      await loadSets();
      toast(`new set: ${key}`);
      return !!activeSet();
    }
  } catch (e) { toast(`no set to draw into: ${e.message}`); }
  return false;
}
// A set from a file on this computer: a zip of a DASCore set's folder, or one bare table.
async function loadSetFile(file) {
  const stem = file.name.replace(/\.[^.]+$/, "").replace(/[^A-Za-z0-9_.-]+/g, "_").replace(/^[_.-]+/, "").slice(0, 56) || "annotations";
  const first = groupProducts()[0];
  const q = query({ filename: file.name, dims: first.dims.join(","), acquisition_key: S.group });
  let name = null;
  for (let n = 1; n < 50 && !name; n++) {  // named after the file; numbered if that name is taken
    const tryName = n === 1 ? stem : `${stem}_${n}`;
    try {
      await api(`/annotations/${S.me}/${tryName}/import?${q}`, { method: "POST", headers: { "Content-Type": "application/octet-stream" }, body: file });
      name = tryName;
    } catch (e) { if (e.status !== 409) { toast(`not loaded: ${e.message}`); return; } }
  }
  if (!name) return;
  await save();
  S.active = `${S.me}/${name}`; S.undo = []; S.redo = [];
  await loadSets();
  toast(findSet(S.active) ? `loaded as ${S.me}/${name}` : `loaded as ${S.me}/${name}, but it is on other dims or another recording, so it is not shown here`);
}
// The set being edited, to a file: its DASCore folder, zipped.
async function saveSetFile() {
  const s = activeSet(); if (!s) return;
  await save();  // what is on disk is what is on screen
  const link = document.createElement("a");
  link.href = `${API}/annotations/${s.owner}/${s.name}/export`; link.download = `${s.name}.zip`;
  document.body.appendChild(link); link.click(); link.remove();
}
// "delete set" asks once more on the button itself, for a few seconds.
async function deleteSet() {
  const s = activeSet(), b = $("btnDeleteSet"); if (!s) return;
  if (b.dataset.sure !== setKey(s)) {
    b.dataset.sure = setKey(s); b.textContent = `delete ${s.name}?`; b.classList.add("danger");
    clearTimeout(deleteSet.timer);
    deleteSet.timer = setTimeout(() => { delete b.dataset.sure; b.textContent = "delete set"; b.classList.remove("danger"); }, 4000);
    return;
  }
  clearTimeout(deleteSet.timer); delete b.dataset.sure; b.textContent = "delete set"; b.classList.remove("danger");
  try { await api(`/annotations/${s.owner}/${s.name}`, { method: "DELETE" }); } catch (e) { toast(e.message); return; }
  S.active = null; S.sel = null; S.many = []; await loadSets();
}
// A new annotation: drawn ones (`ask`) open the label box where they are.
function addFeature(f, ask) {
  change((s) => s.feats.push(f)); S.sel = { set: S.active, id: f.id }; S.many = []; renderSelected();
  if (ask) openLabel(f);
}
// The label of a new annotation is typed where it was drawn. The last label is offered, so
// repeating it is one key (Enter) or none (draw the next): typing replaces it, Esc leaves none.
function openLabel(f) {
  const box = $("labelBox"), at = labelAnchor(f, plotRect()), s = activeSet(); if (!at || !s) return;
  $("labelList").innerHTML = [...new Set(s.feats.map((x) => x.cols.label).filter(Boolean))].slice(0, 50).map((v) => `<option value="${esc(v)}">`).join("");
  box.dataset.id = f.id; box.value = f.cols.label ?? S.lastLabel;
  box.style.left = `${clamp(at[0] + 8, 4, cv.clientWidth - 190)}px`; box.style.top = `${clamp(at[1] - 34, 4, cv.clientHeight - 34)}px`;
  box.hidden = false; box.focus(); box.select();
}
function closeLabel(commit) {
  const box = $("labelBox"); if (box.hidden) return;
  box.hidden = true;
  const s = activeSet(), f = s && s.feats.find((x) => x.id === box.dataset.id), v = box.value.trim();
  if (commit && v) S.lastLabel = v;
  if (commit && f && (f.cols.label ?? "") !== v) change(() => { if (v) f.cols.label = v; else delete f.cols.label; });
}
function cloneInto(set, f) {  // a copy for the set being edited, which says where it came from
  const c = JSON.parse(JSON.stringify(f));
  for (const k of STAMPS) delete c.cols[k];
  c.cols.derived_from = `${setKey(set)}/${f.id}`; c.cols.derived_revision = set.revision;
  c.id = newId();
  if (c.rows) c.rows.forEach((r) => (r.feature_id = c.id));
  return c;
}
// Copies what is selected in other sets into the set being edited, as one step to undo.
async function copySelected() {
  const from = picked().filter((x) => setKey(x.set) !== S.active);
  if (!from.length || !(await ensureSet())) return;
  const copies = from.map((x) => cloneInto(x.set, x.f));
  change((s) => s.feats.push(...copies));
  setPicked(copies.map((c) => ({ set: S.active, id: c.id })));
  toast(`${copies.length === 1 ? "copied" : `${copies.length} copied`} into ${S.active}`);
}
// Every picked annotation: the one being edited, or those a selection window caught.
function picked() {
  const refs = S.many.length ? S.many : S.sel ? [S.sel] : [], out = [];
  for (const r of refs) {
    const set = findSet(r.set), f = set && set.feats && set.feats.find((x) => x.id === r.id);
    if (f) out.push({ set, f });
  }
  return out;
}
function setPicked(refs) {
  S.sel = refs.length === 1 ? refs[0] : null; S.many = refs.length > 1 ? refs : [];
  renderSelected(); redraw();
}
// Removes what is picked from the set being edited; other sets are not this user's to change.
function deleteSelected() {
  const all = picked(), ids = new Set(all.filter((x) => setKey(x.set) === S.active).map((x) => x.f.id));
  if (ids.size) change((s) => (s.feats = s.feats.filter((x) => !ids.has(x.id))));
  if (all.length > ids.size) toast(`${all.length - ids.size} in sets you can't edit were kept`);
  if (ids.size) setPicked([]);
}
function minPoints(kind) { return kind === "polygon" ? 3 : kind === "path" ? 2 : 1; }
function finishDraft() {
  const d = S.draft; S.draft = null;
  if (d && d.pts.length >= minPoints(d.kind)) addFeature({ id: newId(), kind: d.kind, cols: {}, pts: d.pts }, true);
  redraw();
}

// ------------------------------------------------------------------ annotation drawing and hit testing
function corners(b) { return [[b.x0, b.y0], [b.x1, b.y0], [b.x1, b.y1], [b.x0, b.y1]]; }
function boxPx(b, P) {
  const xa = b.x0 === null ? P.x : x2px(b.x0), xb = b.x1 === null ? P.x + P.w : x2px(b.x1);
  const ya = b.y1 === null ? P.y : y2px(b.y1), yb = b.y0 === null ? P.y + P.h : y2px(b.y0);
  return [xa, ya, xb - xa, yb - ya];
}
function vertices(f) {
  if (f.pts) return f.pts;
  if (f.kind === "box" && Object.values(f.box).every((v) => v !== null)) return corners(f.box);
  return [];
}
function rawShapes(f) {  // what a raw feature's rows can show: points and boxes
  const has = (r, n) => r[n] !== null && r[n] !== undefined, out = { pts: [], boxes: [] };
  for (const r of f.rows) {
    if (has(r, S.x) && has(r, S.y)) out.pts.push([xRead(r[S.x]), +r[S.y]]);
    else {
      const get = (n, read) => (has(r, n) ? read(r[n]) : null);
      const x = has(r, S.x) ? xRead(r[S.x]) : null, y = has(r, S.y) ? +r[S.y] : null;
      out.boxes.push({ x0: x ?? get(`${S.x}_min`, xRead), x1: x ?? get(`${S.x}_max`, xRead), y0: y ?? get(`${S.y}_min`, Number), y1: y ?? get(`${S.y}_max`, Number) });
    }
  }
  return out;
}
function strokePts(pts, close) {
  ctx.beginPath();
  pts.forEach((p, i) => { const x = x2px(p[0]), y = y2px(p[1]); if (i) ctx.lineTo(x, y); else ctx.moveTo(x, y); });
  if (close) ctx.closePath();
}
function dot(p, r) { ctx.beginPath(); ctx.arc(x2px(p[0]), y2px(p[1]), r, 0, 6.2832); ctx.fill(); ctx.stroke(); }
// `sel`: 1 the one being edited (with handles), 2 one of several selected. `own`: of the set
// being edited; the others are dashed, so what can be changed is told apart without colour.
function drawFeature(f, color, sel, P, own) {
  const dash = own ? [] : [6, 4];
  ctx.strokeStyle = color; ctx.fillStyle = color; ctx.lineWidth = sel ? 2.5 : 1.5; ctx.setLineDash([]);
  const fill = () => { ctx.globalAlpha = sel ? 0.3 : 0.12; ctx.fill(); ctx.globalAlpha = 1; };
  // each line over a wider one, so it reads on any colour of the image: dark, or gold when picked
  const edge = sel ? THEME.gold : "rgba(0,0,0,0.75)";
  const line = () => { const w = ctx.lineWidth; ctx.strokeStyle = edge; ctx.lineWidth = w + (sel ? 4 : 2.5); ctx.stroke(); ctx.strokeStyle = color; ctx.lineWidth = w; ctx.setLineDash(dash); ctx.stroke(); ctx.setLineDash([]); };
  if (f.kind === "path") { strokePts(f.pts); line(); }
  else if (f.kind === "polygon") { strokePts(f.pts, true); fill(); line(); }
  else if (f.kind === "box") { const r = boxPx(f.box, P); ctx.beginPath(); ctx.rect(...r); fill(); line(); }
  else if (f.kind === "raw") {
    const sh = rawShapes(f);
    for (const b of sh.boxes) { const r = boxPx(b, P); ctx.beginPath(); ctx.rect(r[0], r[1], Math.max(r[2], 1), Math.max(r[3], 1)); fill(); line(); }
    ctx.strokeStyle = "#000"; ctx.lineWidth = 1; for (const p of sh.pts) dot(p, 4);
  }
  if (f.kind === "point" || f.kind === "group") { ctx.strokeStyle = sel ? THEME.gold : "#000"; ctx.lineWidth = sel ? 2.5 : 1; for (const p of f.pts) dot(p, sel ? 6 : 4.5); }
  if (sel === 1 && f.kind !== "point" && f.kind !== "group") { ctx.fillStyle = "#fff"; ctx.strokeStyle = "#000"; ctx.lineWidth = 1; for (const p of vertices(f)) dot(p, 3.5); }
  const label = f.cols.label, at = labelAnchor(f, P);
  if (label && at) {
    ctx.font = "13px system-ui"; ctx.textAlign = "left"; ctx.textBaseline = "bottom";
    const w = ctx.measureText(label).width, x = clamp(at[0] + 5, P.x + 3, P.x + P.w - w - 3), y = clamp(at[1] - 4, P.y + 16, P.y + P.h - 3);  // kept inside the plot
    ctx.lineWidth = 3; ctx.strokeStyle = "rgba(0,0,0,0.8)"; ctx.strokeText(label, x, y);
    ctx.fillStyle = color; ctx.fillText(label, x, y);
  }
}
function labelAnchor(f, P) {
  if (f.kind === "box") { const r = boxPx(f.box, P); return [r[0], r[1] + 14]; }
  const pts = f.pts || (f.rows ? rawShapes(f).pts : []);
  return pts.length ? [x2px(pts[0][0]), y2px(pts[0][1])] : null;
}
// The screen box of a feature, or null if it can't be placed.
function featureBounds(f, P) {
  let pts = f.pts, boxes = [];
  if (f.kind === "box") boxes = [f.box];
  else if (f.kind === "raw") { const sh = rawShapes(f); pts = sh.pts; boxes = sh.boxes; }
  let x0 = Infinity, x1 = -Infinity, y0 = Infinity, y1 = -Infinity;
  for (const p of pts || []) { const x = x2px(p[0]), y = y2px(p[1]); x0 = Math.min(x0, x); x1 = Math.max(x1, x); y0 = Math.min(y0, y); y1 = Math.max(y1, y); }
  for (const b of boxes) { const r = boxPx(b, P); x0 = Math.min(x0, r[0]); x1 = Math.max(x1, r[0] + r[2]); y0 = Math.min(y0, r[1]); y1 = Math.max(y1, r[1] + r[3]); }
  return x0 <= x1 ? { x0, x1, y0, y1 } : null;
}
// Features too small to tell apart at this zoom are drawn as one marker for each crowded patch
// of screen, with their number; hovering it says what it holds and clicking it zooms to them.
const CROWD_SIZE = 14, CROWD_CELL = 30;
function drawAnnotations(P) {
  const many = new Set(S.many.map((r) => `${r.set}|${r.id}`));
  S.clusters = [];
  for (const s of S.sets) {
    if (!s.visible || !s.feats) continue;
    const key = setKey(s), cells = new Map(), plain = [];
    for (const f of s.feats) {
      const b = featureBounds(f, P), picked = S.sel && S.sel.set === key && S.sel.id === f.id ? 1 : many.has(`${key}|${f.id}`) ? 2 : 0;
      if (!b || b.x1 < P.x || b.x0 > P.x + P.w || b.y1 < P.y || b.y0 > P.y + P.h) continue;
      if (picked || Math.max(b.x1 - b.x0, b.y1 - b.y0) > CROWD_SIZE) { plain.push([f, picked]); continue; }
      const cx = (b.x0 + b.x1) / 2, cy = (b.y0 + b.y1) / 2, cell = `${Math.floor(cx / CROWD_CELL)},${Math.floor(cy / CROWD_CELL)}`;
      if (!cells.has(cell)) cells.set(cell, { set: key, color: s.color, feats: [], x: 0, y: 0 });
      const c = cells.get(cell); c.feats.push(f); c.x += cx; c.y += cy;
    }
    const crowds = [];
    for (const c of cells.values()) {
      if (c.feats.length === 1) { plain.push([c.feats[0], 0]); continue; }
      c.x /= c.feats.length; c.y /= c.feats.length;
      // a crowd split by the grid is one crowd
      const near = crowds.find((o) => Math.hypot(o.x - c.x, o.y - c.y) < CROWD_CELL);
      if (!near) { crowds.push(c); continue; }
      const n = near.feats.length, m = c.feats.length;
      near.x = (near.x * n + c.x * m) / (n + m); near.y = (near.y * n + c.y * m) / (n + m);
      near.feats.push(...c.feats);
    }
    for (const c of crowds) { c.r = 9 + Math.min(8, Math.log2(c.feats.length) * 1.5); S.clusters.push(c); }
    for (const [f, picked] of plain) drawFeature(f, s.color, picked, P, key === S.active);
  }
  for (const c of S.clusters) {
    ctx.beginPath(); ctx.roundRect(c.x - c.r, c.y - c.r * 0.8, c.r * 2, c.r * 1.6, 5);  // a tag, not a point
    ctx.fillStyle = c.color; ctx.globalAlpha = 0.9; ctx.fill(); ctx.globalAlpha = 1;
    ctx.strokeStyle = "#000"; ctx.lineWidth = 1; ctx.setLineDash([]); ctx.stroke();
    ctx.fillStyle = "#000"; ctx.font = "bold 13px system-ui"; ctx.textAlign = "center"; ctx.textBaseline = "middle";
    ctx.fillText(c.feats.length > 999 ? "999+" : String(c.feats.length), c.x, c.y + 0.5);
  }
  if (S.draft) {
    ctx.strokeStyle = "#fff"; ctx.fillStyle = "#fff"; ctx.lineWidth = 1.5; ctx.setLineDash([5, 4]);
    const pts = S.mouse ? [...S.draft.pts, [px2x(S.mouse.x), px2y(S.mouse.y)]] : S.draft.pts;
    strokePts(pts, S.draft.kind === "polygon"); ctx.stroke(); ctx.setLineDash([]);
    ctx.strokeStyle = "#000"; for (const p of S.draft.pts) dot(p, 3.5);
  }
  const rubber = S.drag && (S.drag.kind === "rect" || S.drag.kind === "band") ? S.drag.box : null;
  // the download area and the selection window: light dashes over a dark line, so they read on any colour
  for (const [b, color, tag] of [[S.area, "#ffffff", "download"], [rubber, "#cfe0ff", ""]]) {
    if (!b) continue;
    const r = boxPx(b, P);
    ctx.fillStyle = color; ctx.globalAlpha = 0.1; ctx.fillRect(...r); ctx.globalAlpha = 1;
    ctx.setLineDash([]); ctx.strokeStyle = "rgba(0,0,0,0.8)"; ctx.lineWidth = 4; ctx.strokeRect(...r);
    ctx.setLineDash([7, 5]); ctx.strokeStyle = color; ctx.lineWidth = 2; ctx.strokeRect(...r); ctx.setLineDash([]);
    if (tag && r[2] > 70) {
      ctx.font = "12px system-ui"; ctx.textAlign = "left"; ctx.textBaseline = "top";
      ctx.lineWidth = 3; ctx.strokeStyle = "rgba(0,0,0,0.8)"; ctx.strokeText(tag, r[0] + 5, r[1] + 4);
      ctx.fillStyle = color; ctx.fillText(tag, r[0] + 5, r[1] + 4);
    }
  }
}
function distToSegment(px, py, ax, ay, bx, by) {
  const dx = bx - ax, dy = by - ay, n = dx * dx + dy * dy;
  const t = n ? clamp(((px - ax) * dx + (py - ay) * dy) / n, 0, 1) : 0;
  return Math.hypot(px - ax - t * dx, py - ay - t * dy);
}
function featureDistance(f, px, py, P) {
  const near = (pts, close) => {
    let best = Infinity; const n = pts.length;
    for (let i = 0; i < n; i++) {
      const a = pts[i]; best = Math.min(best, Math.hypot(px - a[0], py - a[1]));
      const b = pts[i + 1] || (close ? pts[0] : null);
      if (b) best = Math.min(best, distToSegment(px, py, a[0], a[1], b[0], b[1]));
    }
    return best;
  };
  const screen = (pts) => pts.map((p) => [x2px(p[0]), y2px(p[1])]);
  const rect = (b) => { const r = boxPx(b, P); return near([[r[0], r[1]], [r[0] + r[2], r[1]], [r[0] + r[2], r[1] + r[3]], [r[0], r[1] + r[3]]], true); };
  if (f.kind === "box") return rect(f.box);
  if (f.kind === "raw") { const sh = rawShapes(f); return Math.min(near(screen(sh.pts), false) - 3, ...sh.boxes.map(rect), Infinity); }
  if (f.kind === "path" || f.kind === "polygon") return near(screen(f.pts), f.kind === "polygon");
  return Math.min(...screen(f.pts).map((p) => Math.hypot(px - p[0], py - p[1]))) - 3;
}
function hitFeature(px, py) {
  const P = plotRect(); let best = null;
  for (const s of S.sets) {
    if (!s.visible || !s.feats) continue;
    const own = setKey(s) === S.active ? 1 : 0;  // a copy lies on its original: the one that can be edited wins
    for (const f of s.feats) { const d = featureDistance(f, px, py, P) - own; if (d < 7 && (!best || d < best.d)) best = { d, set: setKey(s), id: f.id }; }
  }
  return best;
}
// The box around a feature, in data coordinates, as an area to download. A feature with no
// extent along a dim (a point, a level line) takes a twentieth of the view either side.
function worldBounds(f) {
  let pts = f.pts || [], boxes = f.kind === "box" ? [f.box] : [];
  if (f.kind === "raw") { const sh = rawShapes(f); pts = sh.pts; boxes = sh.boxes; }
  const lo = (a, b) => (a === null || b === null ? null : Math.min(a, b)), hi = (a, b) => (a === null || b === null ? null : Math.max(a, b));
  let b = null;
  const add = (o) => { b = b ? { x0: lo(b.x0, o.x0), x1: hi(b.x1, o.x1), y0: lo(b.y0, o.y0), y1: hi(b.y1, o.y1) } : { ...o }; };
  for (const p of pts) add({ x0: p[0], x1: p[0], y0: p[1], y1: p[1] });
  for (const o of boxes) add(o);
  const P = plotRect(), padX = (P.w * S.view.spp) / 20, padY = (P.h * S.view.ypp) / 20;
  if (b.x0 !== null && b.x1 - b.x0 <= 0) { b.x0 -= padX; b.x1 += padX; }
  if (b.y0 !== null && b.y1 - b.y0 <= 0) { b.y0 -= padY; b.y1 += padY; }
  return b;
}
function hitCluster(px, py) { return S.clusters.find((c) => Math.hypot(px - c.x, py - c.y) <= c.r + 2) || null; }
function clusterText(c) {
  const counts = new Map();
  for (const f of c.feats) { const k = f.cols.label || f.kind; counts.set(k, (counts.get(k) || 0) + 1); }
  const parts = [...counts.entries()].sort((a, b) => b[1] - a[1]).slice(0, 4).map(([k, n]) => `${n} ${k}`);
  return `${c.feats.length} annotations in ${c.set}: ${parts.join(", ")}${counts.size > 4 ? ", ..." : ""}`;
}
function zoomToCluster(c) {
  pushView();
  const P = plotRect(); let x0 = Infinity, x1 = -Infinity, y0 = Infinity, y1 = -Infinity;
  for (const f of c.feats) { const b = featureBounds(f, P); x0 = Math.min(x0, b.x0); x1 = Math.max(x1, b.x1); y0 = Math.min(y0, b.y0); y1 = Math.max(y1, b.y1); }
  const fx = Math.max((x1 - x0) * 1.6, 12) / P.w, fy = Math.max((y1 - y0) * 1.6, 12) / P.h;
  zoomAt((x0 + x1) / 2, (y0 + y1) / 2, fx, fy);
  // centre what was under the cluster
  S.view.x0 += ((x0 + x1) / 2 - (P.x + P.w / 2)) * S.view.spp; S.view.y0 -= ((y0 + y1) / 2 - (P.y + P.h / 2)) * S.view.ypp;
  redraw();
}
function hitVertex(px, py) {
  const cur = selected(); if (!cur || setKey(cur.set) !== S.active) return -1;
  const vs = vertices(cur.f);
  for (let i = 0; i < vs.length; i++) if (Math.hypot(px - x2px(vs[i][0]), py - y2px(vs[i][1])) < 7) return i;
  return -1;
}
function moveVertex(f, i, x, y) {
  if (f.pts) { f.pts[i][0] = x; f.pts[i][1] = y; return; }
  const b = f.box, o = corners(b)[(i + 2) % 4];  // the opposite corner stays
  b.x0 = Math.min(x, o[0]); b.x1 = Math.max(x, o[0]); b.y0 = Math.min(y, o[1]); b.y1 = Math.max(y, o[1]);
}

// ------------------------------------------------------------------ side panels
// The Selected section. It is rebuilt only when what it shows changes shape (another
// annotation, a new column); otherwise its fields are updated in place, so a field being
// typed in keeps its text and the focus stays where the user put it.
const HIDDEN_COLS = new Set([...STAMPS]);
function shortTime(v) { return typeof v === "string" ? v.slice(0, 16).replace("T", " ") : ""; }
function metaLine(f) {
  const parts = [];
  if (f.cols.modified_time) parts.push(`edited ${shortTime(f.cols.modified_time)}${f.cols.modified_by ? ` by ${f.cols.modified_by}` : ""}`);
  if (f.cols.derived_from) parts.push(`copied from ${f.cols.derived_from}`);
  return parts.join(" · ");
}
function renderSelected() {
  const cur = selected(), el = $("selected"), all = picked();
  $("selBox").hidden = !all.length;  // shown only while there is something to show
  if ($("btnAreaOf")) $("btnAreaOf").disabled = !all.length;  // the Download section's "selection"
  if (all.length > 1) {
    const mine = all.filter((x) => setKey(x.set) === S.active).length, others = all.length - mine;
    el.dataset.sig = "";
    el.innerHTML = `<div class="hint">${all.length} annotations${others && mine ? ` (${mine} in the set being edited)` : ""}</div>` +
      (mine ? '<div class="row"><input type="text" id="inpBulkLabel" placeholder="label for all" list="labelList"></div>' : "") +
      '<div class="row">' +
      (others && S.me ? `<button class="small" id="btnCopy" title="copy into the set being edited (c)">copy ${others}</button>` : "") +
      (mine ? `<button class="small" id="btnDelete" title="Delete">delete ${mine}</button>` : "") + "</div>";
    return;
  }
  if (!cur) { el.textContent = ""; el.dataset.sig = ""; return; }
  const { set, f } = cur, mine = setKey(set) === S.active;
  const names = ["label", "note", ...Object.keys(f.cols).filter((k) => k !== "label" && k !== "note" && !HIDDEN_COLS.has(k))];
  const sig = `${setKey(set)}|${f.id}|${mine}|${names.join(",")}`;
  if (el.dataset.sig === sig) {  // the same fields: only their values may have changed
    for (const box of el.querySelectorAll("input[data-col]")) if (box !== document.activeElement) box.value = f.cols[box.dataset.col] ?? "";
    for (const cell of el.querySelectorAll("td[data-col]")) cell.textContent = f.cols[cell.dataset.col] ?? "";
    el.querySelector(".meta").textContent = metaLine(f);
    return;
  }
  const rows = names.map((k) => {
    const v = f.cols[k] ?? "";
    return `<tr><td>${esc(k)}</td>${mine ? `<td><input type="text" data-col="${esc(k)}" value="${esc(v)}"${k === "label" ? ' list="labelList"' : ""}></td>` : `<td data-col="${esc(k)}">${esc(v)}</td>`}</tr>`;
  }).join("") + (mine ? '<tr><td></td><td><input type="text" data-newcol placeholder="add a column"></td></tr>' : "");
  el.dataset.sig = sig;
  el.innerHTML = `<div class="hint">${esc(f.kind)} · ${esc(setKey(set))}</div><table class="cols">${rows}</table>` +
    `<div class="hint meta">${esc(metaLine(f))}</div><div class="row">` +
    (mine ? '<button class="small" id="btnDelete" title="Delete">delete</button>' : "") +
    (!mine && S.me ? '<button class="small" id="btnCopy" title="copy into the set being edited (c)">copy to my set</button>' : "") +
    "</div>";
}
function areaQuery(ids) {
  const a = S.area, q = { product: ids };
  if (a.x0 !== null) q[`${S.x}_min`] = xWire(a.x0);
  if (a.x1 !== null) q[`${S.x}_max`] = xWire(a.x1);
  if (a.y0 !== null) q[`${S.y}_min`] = a.y0;
  if (a.y1 !== null) q[`${S.y}_max`] = a.y1;
  return q;
}
// The Download section. An area comes from the selected annotation, from the window on
// screen, or from a box drawn with the area tool; it is then downloaded through the browser
// or, when the viewer was started with a local data directory, synced into it.
function windowBounds() {
  const P = plotRect(), e = viewEnd();
  return { x0: Math.max(px2x(P.x), S.ext.x0), x1: Math.min(px2x(P.x + P.w), S.ext.x1), y0: Math.max(S.view.y0, S.ext.y0), y1: Math.min(e.y1, S.ext.y1) };
}
function fmtDuration(s) {
  if (s < 60) return `${+s.toPrecision(3)} s`;
  if (s < 3600) return `${Math.floor(s / 60)} min ${Math.round(s % 60)} s`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ${Math.round((s % 3600) / 60)} min`;
  return `${Math.floor(s / 86400)} d ${Math.round((s % 86400) / 3600)} h`;
}
// The two ranges of an area, said once each: the date is not repeated, and each has its size.
function areaText(a) {
  const open = (lo, hi, f) => `${lo === null ? "start" : f(lo)} – ${hi === null ? "end" : f(hi)}`;
  let x;
  if (S.xTime && a.x0 !== null && a.x1 !== null) {
    const day = (v) => isoOf(v).slice(0, 10), clock = (v) => isoOf(v).slice(11, 21);
    x = `${day(a.x0)} ${clock(a.x0)} – ${day(a.x1) === day(a.x0) ? "" : day(a.x1) + " "}${clock(a.x1)} (${fmtDuration(a.x1 - a.x0)})`;
  } else x = `${S.x} ${open(a.x0, a.x1, fmtX)}`;
  const num = (v) => fmtNum(+v.toPrecision(4)), unit = S.yUnits || S.y;
  const y = `${open(a.y0, a.y1, num)} ${unit}` + (a.y0 !== null && a.y1 !== null ? ` (${num(a.y1 - a.y0)})` : "");
  return `${esc(x)}<br>${esc(y)}`;
}
const BIG_DOWNLOAD = 2 * 2 ** 30;  // past this the button warns by its colour
async function renderArea() {
  const el = $("area"), a = S.area;
  const stamp = (renderArea.stamp = (renderArea.stamp || 0) + 1);
  const pick = '<div class="row"><span class="hint">area from</span>' +
    `<button class="small" id="btnAreaOf" ${picked().length ? "" : "disabled"} title="the box around what is selected">selection</button>` +
    '<button class="small" id="btnAreaWindow" title="what is on screen now">window</button>' +
    `<button class="small ${S.mode === "area" ? "on" : ""}" id="btnAreaDraw" title="drag a box over the data (d)">a drawn box</button></div>`;
  if (!a || !S.levels.length) { el.innerHTML = pick; return; }
  // every product of the recording can be downloaded, in the ladder or not; the finest by default
  const all = groupProducts().slice().sort((p, q) => p.coords[S.x].step - q.coords[S.x].step || p.coords[S.y].step - q.coords[S.y].step);
  if (!S.areaProducts) S.areaProducts = new Set([S.levels[S.levels.length - 1].p.id]);
  const ids = all.map((p) => p.id).filter((id) => S.areaProducts.has(id));
  const syncTip = S.sync ? "copy this area into the local data directory" : "start the viewer with a local data directory to sync into it";
  el.innerHTML = pick + `<div class="row"><span class="hint">${areaText(a)}</span>` +
    '<button class="small" id="btnAreaClear" title="forget this area">clear</button></div>' +
    `<div id="areaProducts" title="the products to download">${all.map((p) => `<label><input type="checkbox" value="${p.id}" ${S.areaProducts.has(p.id) ? "checked" : ""}> ${esc(levelName(p))} <span class="hint">${fmtStep(p.coords[S.x].step, S.xTime)}</span></label>`).join("")}</div>` +
    `<div class="row"><span id="areaSize">${ids.length ? "…" : "choose a product"}</span><button id="btnSync" ${S.sync && ids.length ? "" : "disabled"} title="${syncTip}">Sync</button></div>`;
  redraw();
  if (!ids.length) return;
  const q = areaQuery(ids);
  try {
    const sum = await api("/summary?" + query(q));
    if (stamp !== renderArea.stamp) return;
    const start = a.x0 === null ? "" : `_${S.xTime ? isoOf(a.x0).slice(0, 19) : fmtNum(a.x0)}`;
    const what = ids.length === 1 ? levelName(all.find((p) => p.id === ids[0])) : `${ids.length}_products`;
    const file = `${S.group || "data"}_${what}${start}`.replace(/[^A-Za-z0-9_.-]+/g, "_");
    const big = sum.bytes > BIG_DOWNLOAD;
    $("areaSize").innerHTML = sum.patches
      ? `<a id="lnkArea" class="btn ${big ? "big" : ""}" href="${API}/data?${query({ ...q, format: "dasdae" })}" download="${esc(file)}.h5" title="${big ? "a large download" : "as a DASDAE file"}">Download ${fmtBytes(sum.bytes)}</a>`
      : "no data in this area";
  } catch (e) { if (stamp === renderArea.stamp) $("areaSize").textContent = e.message; }
}

// ------------------------------------------------------------------ mouse and keyboard
function evPos(ev) { const r = cv.getBoundingClientRect(); return { x: ev.clientX - r.left, y: ev.clientY - r.top }; }
function inPlot(p) { const P = plotRect(); return p.x >= P.x && p.x <= P.x + P.w && p.y >= P.y && p.y <= P.y + P.h; }
function toolCursor() { return S.mode === "pan" ? "grab" : S.mode === "select" ? "default" : "crosshair"; }
async function setMode(mode) {
  if (!["pan", "select", "area"].includes(mode) && !(await ensureSet())) mode = "select";  // drawing needs a set; one is made
  closeLabel(true);
  S.mode = mode; S.draft = null;
  cv.style.cursor = toolCursor();
  for (const b of $("tools").children) { const on = b.dataset.mode === mode; b.classList.toggle("on", on); b.setAttribute("aria-pressed", String(on)); }
  renderArea();
  redraw();
}
// The mouse. The middle button (or Space with the left) drags the view, in every tool. The
// left button belongs to the tool: pan (the tool to begin with) drags the view too, and a
// double-click turns it into select; select picks an annotation and drags it or one of its
// vertices; the drawing tools place points and drag boxes.
function shiftFeature(f, dx, dy) {
  if (f.pts) for (const p of f.pts) { p[0] += dx; p[1] += dy; }
  else if (f.box) { const b = f.box; if (b.x0 !== null) { b.x0 += dx; b.x1 += dx; } if (b.y0 !== null) { b.y0 += dy; b.y1 += dy; } }
}
function within(p, r, pad = 0) { return p.x >= r.x - pad && p.x <= r.x + r.w + pad && p.y >= r.y - pad && p.y <= r.y + r.h + pad; }
function stripTo(px) {  // the window centred where the strip was pressed
  const P = plotRect(), u = tau(S.ext.x0) + ((px - P.x) / P.w) * displaySpan();
  moved(); S.view.x0 = u - (P.w * S.view.spp) / 2; clampPos(); redraw();
}
cv.addEventListener("mousedown", (ev) => {
  if (S.listError) { location.reload(); return; }
  if (!S.levels.length) return;
  const p = evPos(ev), P = plotRect();
  cv.focus();
  if (ev.button === 0 && within(p, barRect(P), 4)) {  // the colour bar: dragged to move its limits, clicked to take them from the screen
    const b = barRect(P), f = clamp((b.y + b.h - p.y) / b.h, 0, 1);
    S.drag = { kind: "bar", p, moved: false, part: f > 0.75 ? "max" : f < 0.25 ? "min" : "both", f, vmin: S.disp.vmin, vmax: S.disp.vmax };
    return;
  }
  if (ev.button === 0 && within(p, stripRect(P), 5)) { pushView(); S.drag = { kind: "strip" }; stripTo(p.x); return; }
  if (!inPlot(p)) return;
  if (ev.button === 1 || (ev.button === 0 && (S.space || S.mode === "pan"))) {
    ev.preventDefault();  // no autoscroll
    // with the pan tool a press which does not move is a click: it selects
    S.drag = { kind: "pan", p, x0: S.view.x0, y0: S.view.y0, moved: false, click: ev.button === 0 && !S.space, shift: ev.shiftKey };
    cv.style.cursor = "grabbing";
    return;
  }
  if (ev.button !== 0) return;
  if (S.mode === "box" || S.mode === "area") { S.drag = { kind: "rect", from: [px2x(p.x), px2y(p.y)], box: null }; return; }
  S.drag = { kind: "click", p, moved: false, shift: ev.shiftKey };
  if (S.mode !== "select") return;
  const i = hitVertex(p.x, p.y);
  if (i >= 0) { S.drag = { kind: "vertex", i, before: snapshot(), moved: false }; return; }
  const hit = hitCluster(p.x, p.y) ? null : hitFeature(p.x, p.y), f = hit && hit.set === S.active && findSet(hit.set).feats.find((x) => x.id === hit.id);
  if (f && (f.pts || f.box)) {  // one of the set being edited: selected now, moved if dragged
    if (!ev.shiftKey) {
      S.sel = { set: hit.set, id: hit.id }; S.many = []; renderSelected();
      S.drag = { kind: "move", p, id: f.id, at: [px2x(p.x), px2y(p.y)], before: snapshot(), moved: false };
    }
  } else if (!hit && !hitCluster(p.x, p.y)) {  // on nothing: a window which selects what it catches
    S.drag = { kind: "band", p, from: [px2x(p.x), px2y(p.y)], box: null, moved: false, shift: ev.shiftKey };
  }
});
cv.addEventListener("auxclick", (ev) => ev.preventDefault());
cv.addEventListener("contextmenu", (ev) => { if (S.draft) { ev.preventDefault(); finishDraft(); } });  // right-click ends a shape
const MOVE_SLOP = 8;  // a press which wobbles less than this selects; it does not nudge the annotation
// What a press would take hold of, shown by the cursor before it is made.
function hoverCursor(p) {
  if (S.space) return "grab";
  if (S.mode === "pan") return hitCluster(p.x, p.y) ? "zoom-in" : hitFeature(p.x, p.y) ? "pointer" : "grab";
  if (S.mode !== "select") return toolCursor();
  if (hitVertex(p.x, p.y) >= 0) return "grab";
  if (hitCluster(p.x, p.y)) return "zoom-in";
  const hit = hitFeature(p.x, p.y);
  return hit ? (hit.set === S.active ? "move" : "pointer") : "default";
}
window.addEventListener("mousemove", (ev) => {
  const p = evPos(ev), P = plotRect(); S.mouse = inPlot(p) ? p : null;
  const d = S.drag, off = d && d.p ? Math.hypot(p.x - d.p.x, p.y - d.p.y) : 0;
  if (d && d.kind === "bar") {
    if (off > 3) d.moved = true;
    if (d.moved) dragBar(d, p);
  } else if (d && d.kind === "pan") {
    if (off > 4) d.moved = true;
    if (d.click && !d.moved) return;
    moved(); S.view.x0 = d.x0 - (p.x - d.p.x) * S.view.spp; S.view.y0 = d.y0 + (p.y - d.p.y) * S.view.ypp;
  } else if (d && d.kind === "strip") {
    stripTo(p.x);
  } else if (d && d.kind === "click") {
    if (off > 4) d.moved = true;
  } else if (d && d.kind === "band") {
    if (off > 4) d.moved = true;
    const x = px2x(p.x), y = px2y(p.y);
    if (d.moved) d.box = { x0: Math.min(d.from[0], x), x1: Math.max(d.from[0], x), y0: Math.min(d.from[1], y), y1: Math.max(d.from[1], y) };
  } else if (d && d.kind === "move") {
    if (off > MOVE_SLOP) d.moved = true;
    const s = activeSet(), f = s && s.feats.find((x) => x.id === d.id);  // by id: the list may have been replaced
    if (d.moved && f) { const x = px2x(p.x), y = px2y(p.y); shiftFeature(f, x - d.at[0], y - d.at[1]); d.at = [x, y]; }
  } else if (d && d.kind === "vertex") {
    const cur = selected();
    if (cur) { d.moved = true; moveVertex(cur.f, d.i, px2x(p.x), px2y(p.y)); }
  } else if (d && d.kind === "rect") {
    const x = px2x(p.x), y = px2y(p.y);
    d.box = { x0: Math.min(d.from[0], x), x1: Math.max(d.from[0], x), y0: Math.min(d.from[1], y), y1: Math.max(d.from[1], y) };
  }
  if (S.mouse && S.levels.length) {  // as fine as the level drawn, and no finer
    const x = px2x(p.x), y = px2y(p.y), v = valueAt(x, y), l = activeLevel();
    const when = S.xTime ? isoOf(x).replace("T", " ").slice(0, l.sx >= 1 ? 19 : 23) : fmtNum(+x.toPrecision(6));
    $("stPos").textContent = `${when}   ${fmtNum(+y.toPrecision(5))} ${S.yUnits || S.y}` + (v === null ? "" : `   ${fmtNum(+v.toPrecision(4))}`);
  } else if (!d) $("stPos").textContent = "";
  if (!d && S.levels.length) {
    const onBar = within(p, barRect(P), 4), onStrip = within(p, stripRect(P), 5);
    cv.style.cursor = onBar ? "ns-resize" : onStrip ? "pointer" : S.mouse ? hoverCursor(p) : "default";
    cv.title = onBar ? "drag an end to move that colour limit, the middle to move both; click to take them from what is on screen (a)" : onStrip ? "the whole recording: click or drag to move the window" : "";
  }
  const crowd = S.mouse && !d ? hitCluster(p.x, p.y) : null, tip = $("tip");
  tip.style.display = crowd ? "block" : "none";
  if (crowd) { tip.textContent = clusterText(crowd); tip.style.left = `${p.x + 14}px`; tip.style.top = `${p.y + 14}px`; }
  if (d || S.draft) redraw();
});
window.addEventListener("mouseup", (ev) => {
  const d = S.drag; if (!d) return;
  S.drag = null;
  const p = evPos(ev);
  if (d.kind === "bar") { if (!d.moved) { S.manual = false; autoContrast(); } }
  else if (d.kind === "pan") { cv.style.cursor = toolCursor(); if (d.click && !d.moved) click(p, d.shift); }
  else if (d.kind === "vertex" || d.kind === "move") {
    if (d.moved) { S.undo.push(d.before); S.redo = []; touch(); }
  } else if (d.kind === "rect") {
    const b = d.box;
    if (b && x2px(b.x1) - x2px(b.x0) > 3 && y2px(b.y0) - y2px(b.y1) > 3) {
      if (S.mode === "area") { S.area = b; setMode("pan"); }  // one area, then back to the tool to begin with
      else addFeature({ id: newId(), kind: "box", cols: {}, box: b }, true);
    }
  } else if (d.kind === "band" && d.moved) {
    const caught = inWindow(d.box);
    setPicked(d.shift ? mergePicked(caught) : caught);
  } else if ((d.kind === "click" || d.kind === "band") && !d.moved) click(p, d.shift);
  redraw();
});
// The colour bar dragged: the value which was under the cursor stays under it. An end moves
// its own limit, the middle moves both.
function dragBar(d, p) {
  const b = barRect(plotRect()), span = d.vmax - d.vmin, v = d.vmin + d.f * span;
  const f = (b.y + b.h - p.y) / b.h;
  let { vmin, vmax } = d;
  if (d.part === "max") vmax = vmin + (v - vmin) / clamp(f, 0.05, 1);
  else if (d.part === "min") vmin = vmax - (vmax - v) / (1 - clamp(f, 0, 0.95));
  else { vmin -= (f - d.f) * span; vmax -= (f - d.f) * span; }
  if (!(vmax > vmin)) return;
  S.disp.vmin = vmin; S.disp.vmax = vmax;
  $("inpVmin").value = +vmin.toPrecision(6); $("inpVmax").value = +vmax.toPrecision(6);
  S.autoPending = false; S.manual = true; S.lutVer++;
}
// The annotations of the visible sets a window catches: any vertex inside it, or a box overlapping it.
function inWindow(b) {
  const P = plotRect(), r = boxPx(b, P), x0 = r[0], x1 = r[0] + r[2], y0 = r[1], y1 = r[1] + r[3], out = [];
  for (const s of S.sets) {
    if (!s.visible || !s.feats) continue;
    for (const f of s.feats) {
      const fb = featureBounds(f, P);
      if (!fb || fb.x1 < x0 || fb.x0 > x1 || fb.y1 < y0 || fb.y0 > y1) continue;
      const pts = f.pts || (f.kind === "raw" ? rawShapes(f).pts : null);
      const inside = pts && pts.length ? pts.some((q) => { const x = x2px(q[0]), y = y2px(q[1]); return x >= x0 && x <= x1 && y >= y0 && y <= y1; }) : true;
      if (inside) out.push({ set: setKey(s), id: f.id });
    }
  }
  return out;
}
// What is picked with `refs` added; one already picked is dropped instead (Shift toggles).
function mergePicked(refs) {
  const key = (r) => `${r.set}|${r.id}`, now = new Map(picked().map((x) => [`${setKey(x.set)}|${x.f.id}`, { set: setKey(x.set), id: x.f.id }]));
  for (const r of refs) { if (now.has(key(r))) now.delete(key(r)); else now.set(key(r), r); }
  return [...now.values()];
}
function click(p, shift) {
  const pt = [px2x(p.x), px2y(p.y)];
  if (S.mode === "point") { addFeature({ id: newId(), kind: "point", cols: {}, pts: [pt] }, true); return; }
  if (S.mode === "path" || S.mode === "polygon") {
    if (!S.draft) S.draft = { kind: S.mode, pts: [] };
    S.draft.pts.push(pt); return;
  }
  const crowd = hitCluster(p.x, p.y);
  if (crowd) { $("tip").style.display = "none"; zoomToCluster(crowd); return; }
  const hit = hitFeature(p.x, p.y), ref = hit && { set: hit.set, id: hit.id };
  if (shift) { if (ref) setPicked(mergePicked([ref])); }
  else setPicked(ref ? [ref] : []);
}
// The side of the selected path or polygon under a point, as the index a new vertex takes.
function hitSide(px, py) {
  const cur = selected();
  if (!cur || setKey(cur.set) !== S.active || (cur.f.kind !== "path" && cur.f.kind !== "polygon")) return -1;
  const pts = cur.f.pts.map((p) => [x2px(p[0]), y2px(p[1])]), n = pts.length, last = cur.f.kind === "polygon" ? n : n - 1;
  for (let i = 0; i < last; i++) {
    const a = pts[i], b = pts[(i + 1) % n];
    if (distToSegment(px, py, a[0], a[1], b[0], b[1]) < 6) return i + 1;
  }
  return -1;
}
cv.addEventListener("dblclick", (ev) => {
  ev.preventDefault();
  if (S.draft) {
    S.draft.pts.pop();  // the second click of the pair added the same point twice
    finishDraft();
    return;
  }
  if (S.mode === "pan" && inPlot(evPos(ev))) { setMode("select"); toast("select tool: drag a window, move annotations (Esc returns to pan)"); return; }
  if (S.mode !== "select") return;
  const p = evPos(ev), at = hitVertex(p.x, p.y) >= 0 ? -1 : hitSide(p.x, p.y), cur = selected();
  if (at >= 0) change(() => cur.f.pts.splice(at, 0, [px2x(p.x), px2y(p.y), {}]));  // a new vertex on that side
});
cv.addEventListener("wheel", (ev) => {
  ev.preventDefault();
  if (!S.levels.length) return;
  const p = evPos(ev), f = Math.exp(clamp(ev.deltaY || ev.deltaX, -300, 300) * 0.0015);  // Shift can turn the wheel sideways
  zoomAt(p.x, p.y, ev.ctrlKey ? 1 : f, ev.shiftKey ? 1 : f);
}, { passive: false });
const PAGE_OVERLAP = 0.03;  // of a screen, shared by the view before and after Tab
window.addEventListener("keyup", (ev) => {
  if (ev.key === " ") { S.space = false; if (!S.drag) cv.style.cursor = toolCursor(); }
});
// Keys act wherever the focus is, except while text is being typed, while the help is open,
// and Space or Enter on a control, which press it.
window.addEventListener("keydown", (ev) => {
  const t = ev.target, k = ev.key;
  if (t.matches && t.matches("input:not([type=checkbox]), select, textarea")) return;
  if ($("help").open) { if (k === "?") { $("help").close(); ev.preventDefault(); } return; }
  if ((k === " " || k === "Enter") && t.closest && t.closest("button, summary, a")) return;
  if ((ev.ctrlKey || ev.metaKey) && k.toLowerCase() === "z") { ev.preventDefault(); if (ev.shiftKey) restore(S.redo, S.undo); else restore(S.undo, S.redo); return; }
  if ((ev.ctrlKey || ev.metaKey) && k.toLowerCase() === "y") { ev.preventDefault(); restore(S.redo, S.undo); return; }
  if (ev.ctrlKey || ev.metaKey || ev.altKey) return;
  const cur = S.many.length ? null : selected(), mine = cur && setKey(cur.set) === S.active;
  if (k === " ") { S.space = true; cv.style.cursor = "grab"; }  // hold to drag the view with the left button
  else if (k === "Tab") {  // a screen on in x, or back with Shift, keeping a little in common
    moved(); S.view.x0 += (ev.shiftKey ? -1 : 1) * plotRect().w * S.view.spp * (1 - PAGE_OVERLAP);
    if (clampPos()) toast(ev.shiftKey ? "the start of the recording" : "the end of the recording");
    redraw();
  }
  else if (k === "Escape") {
    if (S.draft) S.draft = null;
    else if (S.conflict) S.conflict = null;
    else if (S.mode === "pan") setPicked([]);
    else if (S.mode === "select") { if (picked().length) setPicked([]); else setMode("pan"); }  // first the selection, then the tool
    else setMode("select");
    redraw();
  }
  else if (k === "Enter") { if (S.draft) finishDraft(); else if (mine) openLabel(cur.f); else return; }
  else if (k === "Delete" || k === "Backspace") {
    const i = S.mouse ? hitVertex(S.mouse.x, S.mouse.y) : -1;
    if (cur && i >= 0 && cur.f.pts && cur.f.pts.length > minPoints(cur.f.kind)) change(() => cur.f.pts.splice(i, 1));
    else if (picked().length) deleteSelected();
    else if (k === "Backspace") backView();
  }
  else if (k >= "1" && k <= "4") setMode(["point", "path", "polygon", "box"][+k - 1]);
  else if (k === "d") setMode("area");
  else if (k === "v") setMode("select");
  else if (k === "h") setMode("pan");
  else if (k === "c") copySelected();
  else if (k === "b") backView();
  else if (k === "?") $("help").showModal();
  else if (k === "p") showSide($("side").hidden);
  else if (k === "w") { pushView(); wholeView(); }
  else if (k === "a") { S.manual = false; autoContrast(); }
  else if (k.startsWith("Arrow")) {  // a tenth of the view; half with Shift
    const P = plotRect(), f = ev.shiftKey ? 0.5 : 0.1;
    moved();
    if (k === "ArrowLeft") S.view.x0 -= P.w * S.view.spp * f;
    else if (k === "ArrowRight") S.view.x0 += P.w * S.view.spp * f;
    else if (k === "ArrowUp") S.view.y0 += P.h * S.view.ypp * f;
    else S.view.y0 -= P.h * S.view.ypp * f;
    redraw();
  }
  else if (k === "+" || k === "=" || k === "-") {
    const P = plotRect(), f = k === "-" ? 1.25 : 0.8;
    zoomAt(P.x + P.w / 2, P.y + P.h / 2, f, f);
  }
  else return;
  ev.preventDefault();
});

// ------------------------------------------------------------------ controls
// Folding the panel gives its width to the plot: the same span of the recording, drawn wider.
function showSide(show) {
  const handle = $("sideHandle"), what = `${show ? "Hide" : "Show"} the side panel (p)`, was = plotRect().w;
  $("side").hidden = !show; handle.textContent = show ? "\u203a" : "\u2039"; handle.title = what; handle.setAttribute("aria-label", what);
  try { localStorage.setItem("dastor.side", show ? "shown" : "hidden"); } catch (e) { /* private mode */ }
  resize();
  if (S.levels.length && was > 10) { S.view.spp *= was / plotRect().w; clampView(); }
}
function saveDisp() { try { localStorage.setItem("dastor.display", JSON.stringify(S.disp)); } catch (e) { /* private mode */ } }
function initControls() {
  try { Object.assign(S.disp, JSON.parse(localStorage.getItem("dastor.display")) || {}); } catch (e) { /* defaults */ }
  for (const name of Object.keys(CMAPS)) $("selCmap").add(new Option(name, name));
  $("selCmap").value = S.disp.cmap; $("selStat").value = S.disp.stat; $("selScale").value = S.disp.scale; $("chkSmooth").checked = S.disp.smooth;
  $("selGroup").onchange = (ev) => setGroup(ev.target.value);
  $("btnWhole").onclick = () => { pushView(); wholeView(); };
  $("tools").onclick = (ev) => { const b = ev.target.closest("button"); if (b && b.dataset.mode) setMode(b.dataset.mode); };
  $("setChip").onclick = () => showSide(true);
  $("selStat").onchange = (ev) => { S.disp.stat = ev.target.value; S.autoPending = true; saveDisp(); redraw(); };
  $("selScale").onchange = (ev) => { S.disp.scale = ev.target.value; S.autoPending = true; saveDisp(); redraw(); };
  $("selCmap").onchange = (ev) => { S.disp.cmap = ev.target.value; buildLut(); saveDisp(); redraw(); };
  $("chkSmooth").onchange = (ev) => { S.disp.smooth = ev.target.checked; saveDisp(); redraw(); };
  for (const [id, key] of [["inpVmin", "vmin"], ["inpVmax", "vmax"]]) {
    $(id).onchange = (ev) => { const v = parseFloat(ev.target.value); if (Number.isFinite(v)) { S.disp[key] = v; S.autoPending = false; S.manual = true; S.lutVer++; redraw(); } };
  }
  $("btnAuto").onclick = () => { S.manual = false; autoContrast(); };
  $("levelBar").onclick = (ev) => { const b = ev.target.closest("button"); if (b) { pushView(); setPin(b.dataset.level || null); } };
  $("levels").onchange = (ev) => {
    const t = ev.target; if (!t.dataset.id) return;
    const ids = [...$("levels").querySelectorAll("input:checked")].map((x) => x.dataset.id);
    if (!ids.length) { t.checked = true; return; }  // a ladder has at least one level
    try { localStorage.setItem(ladderKey(), JSON.stringify(ids)); } catch (e) { /* private mode */ }
    buildLadder(); clampView(); redraw();
  };
  $("sets").onclick = async (ev) => {
    const t = ev.target;
    if (t.dataset.show) {
      const s = findSet(t.dataset.show); s.visible = t.checked;
      if (s.visible && !s.feats) await loadSet(s);
      redraw();
    } else if (t.dataset.edit) {
      const s = findSet(t.dataset.edit);
      if (!s.editable) { toast("That set is read only. Select annotations in it and copy them into your own (c)."); return; }
      await save();
      S.active = setKey(s); S.undo = []; S.redo = []; renderSets(); renderSelected(); redraw();
    }
  };
  $("btnHelp").onclick = () => $("help").showModal();
  $("btnHelpClose").onclick = () => $("help").close();
  $("help").onclick = (ev) => { if (ev.target === $("help")) $("help").close(); };  // a click outside it
  $("sideHandle").onclick = () => showSide($("side").hidden);
  try { showSide(localStorage.getItem("dastor.side") !== "hidden"); } catch (e) { /* private mode */ }
  $("btnNewSet").onclick = () => newSet(); $("btnDeleteSet").onclick = deleteSet;
  $("inpNewSet").onkeydown = (ev) => {
    if (ev.key === "Enter" && ev.target.value.trim()) newSet(ev.target.value.trim());
    else if (ev.key === "Escape") { $("newSetRow").hidden = true; cv.focus(); }
  };
  $("btnSaveSet").onclick = saveNow;
  $("btnDownloadSet").onclick = saveSetFile;
  $("btnLoadSet").onclick = () => $("fileSet").click();
  $("fileSet").onchange = async (ev) => { const file = ev.target.files[0]; ev.target.value = ""; if (file) await loadSetFile(file); };
  // the label box at a new annotation
  const label = $("labelBox");
  label.onkeydown = (ev) => {
    ev.stopPropagation();
    if (ev.key === "Enter") { closeLabel(true); cv.focus(); }
    else if (ev.key === "Escape") { closeLabel(false); cv.focus(); }
    else if ((ev.ctrlKey || ev.metaKey) && ev.key.toLowerCase() === "z") { ev.preventDefault(); closeLabel(false); cv.focus(); restore(S.undo, S.redo); }
  };
  label.onblur = () => closeLabel(true);
  $("selected").addEventListener("change", (ev) => {
    const t = ev.target, cur = selected();
    if (t.id === "inpBulkLabel") {  // one label for everything selected in the set being edited
      const ids = new Set(picked().filter((x) => setKey(x.set) === S.active).map((x) => x.f.id)), v = t.value.trim();
      if (ids.size) change((s) => { for (const f of s.feats) if (ids.has(f.id)) { if (v) f.cols.label = v; else delete f.cols.label; } });
      if (v) S.lastLabel = v;
      return;
    }
    if (!cur) return;
    if (t.dataset.newcol !== undefined) {
      const name = t.value.trim();
      if (!name) return;
      if (["id", "geometry", ...STAMPS].includes(name) || name in cur.f.cols) { toast(`"${name}" is taken; choose another name`); return; }
      change(() => { cur.f.cols[name] = ""; });
      const box = $("selected").querySelector(`input[data-col="${CSS.escape(name)}"]`); if (box) box.focus();
    } else if (t.dataset.col) {
      const col = t.dataset.col, keep = col === "label" || col === "note";
      if (col === "label" && t.value.trim()) S.lastLabel = t.value.trim();
      change(() => { if (t.value === "" && keep) delete cur.f.cols[col]; else cur.f.cols[col] = t.value; });
    }
  });
  $("selected").addEventListener("click", (ev) => {
    if (ev.target.id === "btnDelete") deleteSelected();
    else if (ev.target.id === "btnCopy") copySelected();
  });
  $("area").addEventListener("change", (ev) => {
    if (!ev.target.closest("#areaProducts")) return;
    S.areaProducts[ev.target.checked ? "add" : "delete"](ev.target.value);
    renderArea();
  });
  $("area").addEventListener("click", (ev) => {
    const all = picked();
    if (ev.target.id === "btnAreaOf" && all.length) {  // one box round everything selected
      const lo = (a, b) => (a === null || b === null ? null : Math.min(a, b)), hi = (a, b) => (a === null || b === null ? null : Math.max(a, b));
      S.area = all.map((x) => worldBounds(x.f)).reduce((a, b) => ({ x0: lo(a.x0, b.x0), x1: hi(a.x1, b.x1), y0: lo(a.y0, b.y0), y1: hi(a.y1, b.y1) }));
      renderArea(); redraw();
    }
    else if (ev.target.id === "btnAreaWindow") { S.area = windowBounds(); renderArea(); redraw(); }
    else if (ev.target.id === "btnAreaDraw") setMode(S.mode === "area" ? "pan" : "area");
    else if (ev.target.id === "btnAreaClear") { S.area = null; renderArea(); redraw(); }
  });
  // a press on a button or tick box hands the keys back to the plot (a text field keeps them)
  for (const el of [$("top"), $("side"), $("status")]) {
    el.addEventListener("click", (ev) => {
      const text = document.activeElement && document.activeElement.matches("input:not([type=checkbox]), select");
      if (!text && ev.target.closest("button, input[type=checkbox], label")) cv.focus({ preventScroll: true });
    });
  }
  window.addEventListener("resize", resize);
  window.addEventListener("beforeunload", (ev) => { if (S.sets.some((s) => s.dirty || s.saving)) ev.preventDefault(); });
}
function scheduleHash() {
  clearTimeout(scheduleHash.timer);
  scheduleHash.timer = setTimeout(() => {
    const v = S.view, hash = "#" + query({ g: S.group, v: [v.x0, v.spp, v.y0, v.ypp].join(",") });
    if (location.hash !== hash) history.replaceState(null, "", hash);
  }, 400);
}
function readHash() {
  const out = {};
  for (const part of location.hash.slice(1).split("&")) { const [k, v] = part.split("="); if (k) out[decodeURIComponent(k)] = decodeURIComponent(v || ""); }
  return out;
}

// ------------------------------------------------------------------ start
async function init() {
  initControls(); buildLut(); resize();
  try {
    const listing = await api("/viewer/products");
    S.products = listing.products; S.limits = listing.limits; S.sync = !!listing.sync;
  } catch (e) { S.listError = e.message; }
  S.ready = true;
  buildGroups();
  if (!S.groups.size) { window.dastorReady = true; redraw(); return; }
  const hash = readHash(), key = S.groups.has(hash.g) ? hash.g : S.groups.keys().next().value;
  await setGroup(key);
  const v = (hash.v || "").split(",").map(Number);
  if (hash.g === key && v.length === 4 && v.every(Number.isFinite) && v[1] > 0 && v[3] > 0) {
    Object.assign(S.view, { x0: v[0], spp: v[1], y0: v[2], ypp: v[3] }); clampView();
  }
  setMode("pan");
  // for tests: nothing is loading, waiting to load, or unsaved
  window.dastorIdle = () => !settling && inflight === 0 && queue.length === 0 && !rafPending && S.sets.every((s) => !s.dirty && !s.saving);
  window.dastorReady = true;
  redraw();
}
init();
