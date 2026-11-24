// The map pane: the fibre of the recording on show, drawn from the server's inventory.
// It shares the page with app.js (its S, C, redraw, px2y ...) and adds: a pane left of the data,
// a colour for the fibre (a label, a measured column, or the mean of the data on screen), and the
// link both ways between a place on the map and a distance in the data.
// Everything said about the fibre, its notes on how well it is placed included, is the inventory's.
"use strict";
(() => {
  const MP = {
    data: null, open: false, colour: "", view: null, lastGroup: null, lastPath: null,
    fitted: null,     // "one" or "all" while the view is a fit, so a resize can fit again; null once the user moves it
    pin: null,        // {path, d, until}: the place a click on the map asked for, marked for a moment
    stay: false,      // the next change of recording came from a click on the map: its view is kept
    hover: null,      // {path, d}: the optical distance under the cursor on the map
    wf: null,         // the product distance under the cursor in the data
    layers: {},       // name -> {on, shapes, credit}
    basemap: false, imgs: new Map(), drag: null, frame: false,
    mean: null,       // the last "data on screen" row means, and what they were made for
    under: null,      // the background layers as last drawn, and for which view
  };
  const pane = $("mapPane"), mcv = $("mapCv"), mctx = mcv.getContext("2d"), tip = $("mapTip");
  const CATS = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#9a6bdc", "#d6568f", "#5f7d8c", "#8c6d3f"];
  const RAMP = ["#440154", "#3b528b", "#21918c", "#5ec962", "#fde725"];
  const ORDER = ["low", "medium", "high"];
  const EARTH = 40075016.7;
  const keep = (k, v) => { try { localStorage.setItem("dastor.map." + k, v); } catch (e) { /* private mode */ } };
  const kept = (k) => { try { return localStorage.getItem("dastor.map." + k); } catch (e) { return null; } };
  const light = () => document.documentElement.dataset.theme === "light";
  const NARROW = 560;  // a window narrower than this has no room for the map and the data side by side
  const metres = (d) => (Math.abs(d) >= 10000 ? `${fmtNum(+(d / 1000).toFixed(2))} km` : `${fmtNum(d)} m`);
  // A place on the fibre is not known to the metre: it is given to 10 m on a long fibre, and more closely on a short one.
  const grain = (p) => (!(p.length < 2000) ? 10 : p.length >= 200 ? 1 : 0.1);
  const about = (d, p) => { const g = grain(p); return `≈ ${metres(+(Math.round(d / g) * g).toFixed(1))}`; };

  // ---------------------------------------------------------------- projection (web mercator, the world is 0..1)
  const mx = (lon) => (lon + 180) / 360;
  const my = (lat) => { const s = Math.sin((clamp(lat, -85.0511, 85.0511) * Math.PI) / 180); return 0.5 - Math.log((1 + s) / (1 - s)) / (4 * Math.PI); };
  const size = () => [mcv.clientWidth, mcv.clientHeight];
  const toPx = (x, y) => { const [W, H] = size(), v = MP.view; return [W / 2 + (x - v.cx) * v.k, H / 2 + (y - v.cy) * v.k]; };
  const cosLat = (y) => Math.cos(2 * Math.atan(Math.exp((0.5 - y) * 2 * Math.PI)) - Math.PI / 2);
  // How the inventory's coordinates become the map's: longitude and latitude through web mercator, anything
  // else (a projected grid, a local x, y) as the plane it is, the second axis up. `per` is ground units per map unit,
  // and `m` the metres in one ground unit (null if the unit is not one known here), for comparing ground with fibre.
  const METRES = { m: 1, meter: 1, metre: 1, meters: 1, metres: 1, km: 1000, kilometer: 1000, kilometre: 1000, cm: 0.01, mm: 0.001, ft: 0.3048, foot: 0.3048, feet: 0.3048 };
  const GEO = { geo: true, x: mx, y: my, per: (y) => EARTH * cosLat(y), unit: "m", m: 1 };
  let PRJ = GEO;
  function frameOf(f) {
    if (!f || f.kind !== "plane") return GEO;
    const m = METRES[String(f.units || "").toLowerCase()] || null;
    return { geo: false, x: (v) => v, y: (v) => -v, per: () => 1, unit: m === 1 ? "m" : f.units || "", m, labels: f.labels || ["x", "y"] };
  }
  const metresPerPx = () => PRJ.per(MP.view.cy) / MP.view.k;  // ground units for one pixel: metres, unless the frame says otherwise
  // Straight lines between a distance map's control points; nothing outside them, as DASCore has it.
  function interp(x, xs, ys) {
    if (!xs.length) return x;
    const n = xs.length - 1, up = xs[n] >= xs[0], lo = up ? xs[0] : xs[n], hi = up ? xs[n] : xs[0];
    if (x < lo || x > hi) return null;
    for (let i = 1; i <= n; i++) if ((up ? x <= xs[i] : x >= xs[i]) || i === n) return ys[i - 1] + ((x - xs[i - 1]) * (ys[i] - ys[i - 1])) / (xs[i] - xs[i - 1] || 1);
    return null;
  }

  // ---------------------------------------------------------------- the inventory's paths
  function prepare(data) {
    PRJ = frameOf(data.frame);
    for (const p of data.paths) {
      // A point is placed only if both its coordinates are stated. Longitudes run on across the
      // antimeridian, so a fibre which crosses it is one line and not one round the world.
      const xs = p.x || [], ys = p.y || [], x = [], y = []; let last = null, turn = 0;
      for (let i = 0; i < p.d.length; i++) {
        const u = xs[i], w = ys[i];
        if (typeof u !== "number" || typeof w !== "number") { x.push(NaN); y.push(NaN); continue; }
        if (PRJ.geo && last !== null) turn += u - last > 180 ? -360 : u - last < -180 ? 360 : 0;
        last = u; x.push(PRJ.x(PRJ.geo ? u + turn : u)); y.push(PRJ.y(w));
      }
      p.x = x; p.y = y;
      p.placed = p.x.some((v) => v === v);
      p.brk = new Set(p.breaks || []);
      const ok = p.d.filter((_, i) => p.x[i] === p.x[i]);
      p.reach = ok.length ? [Math.min(...ok), Math.max(...ok)] : null;  // the stretch the inventory places
      for (const t of Object.values(p.tracks)) t.sort((a, b) => a[0] - b[0]);
    }
    return data;
  }
  // The path of the recording on show. A recording which ran on over a re-laid fibre is on two paths, each
  // for its own time: the one for the time at the middle of the screen is taken.
  function active() {
    if (!MP.data) return null;
    const on = [];
    for (const p of MP.data.paths) { const r = p.recordings.find((q) => q.key === S.group); if (r) on.push({ p, r }); }
    if (on.length < 2 || !S.xTime || !S.levels.length) return on[0] || null;
    const P = plotRect(), t = px2x(P.x + P.w / 2), within = (q) => (!q[0] || parseTime(q[0]) <= t) && (!q[1] || t < parseTime(q[1]));
    return on.find((a) => within(a.r.time || [])) || on[0];
  }
  const toOptical = (a, d) => (a.r.unmapped ? null : interp(d, a.r.from, a.r.to));
  const toProduct = (a, d) => (a.r.unmapped ? null : interp(d, a.r.to, a.r.from));
  // The stretch of the fibre a recording covers, in optical distance (null: not known from the inventory).
  // Without a distance map the recording's own distance is the optical distance: its products say how far it reaches.
  function ownSpan(key) {
    let lo = Infinity, hi = -Infinity;
    for (const q of (S.groups && S.groups.get(key)) || []) {
      const c = q.coords && q.coords[q.y]; if (!c || !isFinite(c.min) || !isFinite(c.max)) continue;
      lo = Math.min(lo, c.min); hi = Math.max(hi, c.max + (c.step || 0));
    }
    return lo <= hi ? [lo, hi] : null;
  }
  const span = (r) => (r.unmapped ? null : r.to.length ? [Math.min(...r.to), Math.max(...r.to)] : ownSpan(r.key));
  const covers = (r, d) => { const s = span(r); return !!s && d >= s[0] && d <= s[1]; };
  function coverage(a) {
    const s = span(a.r); if (s) return s;
    if (a.r.unmapped || !S.ext) return null;
    return [S.ext.y0, S.ext.y1];  // no map: the data's own distance is the optical distance
  }
  function trackValue(p, group, d) {
    const t = p.tracks[group]; if (!t) return null;
    let lo = 0, hi = t.length - 1, k = -1;
    while (lo <= hi) { const m = (lo + hi) >> 1; if (t[m][0] <= d) { k = m; lo = m + 1; } else hi = m - 1; }
    return k >= 0 && d <= t[k][1] ? t[k][2] : null;
  }
  // The two control points a distance lies between, never one from each of two segments.
  function bracket(p, d) {
    const ds = p.d, n = ds.length; if (!n || d < ds[0] - 1e-3 || d > ds[n - 1] + 1e-3) return null;
    let lo = 0, hi = n - 1;
    while (lo < hi) { const m = (lo + hi + 1) >> 1; if (ds[m] <= d) lo = m; else hi = m - 1; }
    if (lo === n - 1 || p.brk.has(lo + 1)) {
      if (d - ds[lo] > 1e-3) return null;  // a gap between segments
      return lo > 0 && !p.brk.has(lo) ? [lo - 1, lo, 1] : [lo, lo, 0];
    }
    return [lo, lo + 1, (d - ds[lo]) / (ds[lo + 1] - ds[lo] || 1)];
  }
  function columnValue(p, name, d) {
    const b = bracket(p, d), v = p.columns[name].v; if (!b || v[b[0]] === null || v[b[1]] === null) return null;
    return v[b[0]] + b[2] * (v[b[1]] - v[b[0]]);
  }
  function positionAt(p, d) {
    const b = p.placed ? bracket(p, d) : null; if (!b || p.x[b[0]] !== p.x[b[0]] || p.x[b[1]] !== p.x[b[1]]) return null;
    return [p.x[b[0]] + b[2] * (p.x[b[1]] - p.x[b[0]]), p.y[b[0]] + b[2] * (p.y[b[1]] - p.y[b[0]])];
  }
  const groundLen = (p, i) => Math.hypot(p.x[i + 1] - p.x[i], p.y[i + 1] - p.y[i]) * PRJ.per(p.y[i]) * PRJ.m;  // in metres
  const joined = (p, i) => i >= 0 && i + 1 < p.d.length && !p.brk.has(i + 1) && p.x[i] === p.x[i] && p.x[i + 1] === p.x[i + 1];
  // More fibre than line: the run of control points around i over which the inventory states much more fibre than
  // the line drawn between them is long (a coil, slack, or a route drawn with few points). It is what the
  // inventory's numbers say, not a kind of thing: [from, to, the line's length], or null.
  function gathered(p, i) {
    const slack = Math.min(1, p.length / 1000 || 1);  // what counts as more, on a fibre of this length
    const dense = (j) => j >= 0 && j + 1 < p.d.length && !p.brk.has(j + 1) && p.x[j] === p.x[j] && p.x[j + 1] === p.x[j + 1] && p.d[j + 1] - p.d[j] > 1.5 * groundLen(p, j) + slack;
    if (!PRJ.m || !dense(i)) return null;  // the fibre is in metres; so must the ground be, to compare
    let a = i, b = i, ground = 0;
    while (dense(a - 1)) a--;
    while (dense(b + 1)) b++;
    for (let j = a; j <= b; j++) ground += groundLen(p, j);
    return p.d[b + 1] - p.d[a] >= 20 * slack ? [p.d[a], p.d[b + 1], ground] : null;
  }

  // ---------------------------------------------------------------- the colour of the fibre
  const hex = (h) => [1, 3, 5].map((i) => parseInt(h.slice(i, i + 2), 16));
  function ramp(f, stops = RAMP) {
    f = clamp(f, 0, 1) * (stops.length - 1); const k = Math.min(Math.floor(f), stops.length - 2), w = f - k, a = hex(stops[k]), b = hex(stops[k + 1]);
    return `rgb(${a.map((v, i) => Math.round(v * (1 - w) + b[i] * w)).join(",")})`;
  }
  // The mean along time of the data on screen, for each row of distance: what "data on screen" paints.
  // It is the plain mean of the magnitudes the tiles hold (the level's statistic of the band on show),
  // sampled at 48 times across the screen; rows whose tiles have not come are left out.
  function viewMean() {
    const P = plotRect(), v = S.view, key = [S.group, v.x0, v.spp, v.y0, v.ypp, P.w, P.h, S.disp.stat, S.disp.scale].join(","), now = performance.now();
    if (MP.mean && MP.mean.key === key && now - MP.mean.at < 400) { if (!MP.mean.again) { MP.mean.again = true; setTimeout(soon, 450); } return MP.mean; }
    const n = Math.min(400, Math.max(20, Math.floor(P.h / 2))), cols = 48, out = new Float32Array(n).fill(NaN);
    for (let j = 0; j < n; j++) {
      const y = v.y0 + ((j + 0.5) / n) * P.h * v.ypp; let sum = 0, m = 0;
      for (let i = 0; i < cols; i++) { const q = valueAt(px2x(P.x + ((i + 0.5) / cols) * P.w), y); if (q !== null && isFinite(q)) { sum += Math.abs(q); m++; } }
      if (m >= 2) { const s = shade(sum / m); if (isFinite(s)) out[j] = s; }
    }
    const fin = Array.from(out).filter((q) => q === q).sort((a, b) => a - b);  // its own limits: the mean of rare events sits low on the data's
    const lo = fin.length ? fin[Math.floor(0.02 * (fin.length - 1))] : 0, hi = fin.length ? fin[Math.ceil(0.98 * (fin.length - 1))] : 1;
    MP.mean = { key, at: now, again: false, n, y0: v.y0, dy: (P.h * v.ypp) / n, v: out, lo, hi };
    return MP.mean;
  }
  function onShow() {  // the band on show, if every level shows the same
    const tags = new Set(S.levels.map((l) => (l.p && l.p.attrs && l.p.attrs.tag) || ""));
    return tags.size === 1 ? [...tags][0] : "";
  }
  function painter(a) {
    const mode = MP.colour, p = a.p, plain = () => THEME.blue;
    if (mode.startsWith("track:")) {
      const g = mode.slice(6), t = p.tracks[g] || [], vals = [...new Set(t.map((q) => String(q[2])))];
      if (vals.every((q) => ORDER.includes(q))) vals.sort((x, y) => ORDER.indexOf(x) - ORDER.indexOf(y));
      else if (vals.every((q) => q.trim() !== "" && isFinite(q))) vals.sort((x, y) => x - y);
      else vals.sort();
      const col = Object.fromEntries(vals.map((q, i) => [q, CATS[i % CATS.length]]));
      const edges = [...new Set(t.flatMap((q) => [q[0], q[1]]))].sort((x, y) => x - y);
      return { at: (d) => { const q = trackValue(p, g, d); return q === null ? null : col[String(q)]; }, edges,
        legend: { title: g.replace(/_/g, " "), items: vals.map((q) => [q, col[q]]), none: "not stated" } };
    }
    if (mode.startsWith("col:")) {
      const name = mode.slice(4), c = p.columns[name], vs = c ? c.v.filter((q) => q !== null) : []; if (!vs.length) return { at: plain };
      let lo = Infinity, hi = -Infinity; for (const q of vs) { lo = Math.min(lo, q); hi = Math.max(hi, q); }
      return { at: (d) => { const q = columnValue(p, name, d); return q === null ? null : ramp((q - lo) / (hi - lo || 1)); },
        legend: { title: `${name.replace(/_/g, " ")}${c.units ? " (" + c.units + ")" : ""}`, lo, hi, stops: RAMP, none: "not stated" } };
    }
    if (mode === "view") {
      const m = viewMean(), stops = CMAPS[S.disp.cmap] || CMAPS.viridis, band = onShow();
      const at = (d) => {
        const y = toProduct(a, d); if (y === null) return null;
        const j = Math.floor((y - m.y0) / m.dy); if (j < 0 || j >= m.n || m.v[j] !== m.v[j]) return null;
        return ramp((m.v[j] - m.lo) / (m.hi - m.lo || 1e-12), stops);
      };
      const title = `mean along time of the data on screen${band ? ": " + band : ""}, ${S.disp.stat} of each pixel${S.disp.scale === "log" ? ", log10" : ""}; own limits`;
      return { at, legend: { title, lo: m.lo, hi: m.hi, stops, none: "not on screen, or not loaded" } };
    }
    return { at: plain };
  }

  // ---------------------------------------------------------------- drawing
  function fit(all) {
    if (!MP.data) return;
    const a = active(), one = !all && a && a.p.placed, paths = (one ? [a.p] : MP.data.paths).filter((p) => p.placed);
    const cov = one ? coverage(a) : null;
    let x0 = Infinity, x1 = -Infinity, y0 = Infinity, y1 = -Infinity;
    const take = (x, y) => { x0 = Math.min(x0, x); x1 = Math.max(x1, x); y0 = Math.min(y0, y); y1 = Math.max(y1, y); };
    for (const p of paths) for (let i = 0; i < p.x.length; i++) if (p.x[i] === p.x[i] && (!cov || (p.d[i] >= cov[0] && p.d[i] <= cov[1]))) take(p.x[i], p.y[i]);
    if (cov) for (const d of cov) { const at = positionAt(a.p, clamp(d, a.p.reach[0], a.p.reach[1])); if (at) take(at[0], at[1]); }
    if (!isFinite(x0)) { if (!all) fit(true); return; }
    const tiny = PRJ.geo ? 2e-6 : Math.max(x1 - x0, y1 - y0) * 1e-3 || 1, [W, H] = size(), w = Math.max(x1 - x0, tiny), h = Math.max(y1 - y0, tiny), room = Math.min(90, H / 4);  // room: the legend's corner
    const k = 0.84 * Math.min(W / w, (H - room) / h);
    MP.view = { cx: (x0 + x1) / 2, cy: (y0 + y1) / 2 + room / 2 / k, k }; MP.k0 = k; MP.fitted = all ? "all" : "one";
  }
  function resizeMap() {
    const dpr = window.devicePixelRatio || 1;
    mcv.width = Math.round(mcv.clientWidth * dpr); mcv.height = Math.round(mcv.clientHeight * dpr);
    mctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  }
  function soon() { if (!MP.frame && MP.open) { MP.frame = true; requestAnimationFrame(() => { MP.frame = false; drawMap(); }); } }
  function drawBasemap(W, H) {
    const b = MP.data.basemap; if (!b || !MP.basemap || !PRJ.geo) return false;
    const url = (!light() && b.dark) || b.url, v = MP.view, dpr = window.devicePixelRatio || 1;
    const z = clamp(Math.round(Math.log2((v.k * dpr) / 256)), 0, 19), n = 2 ** z, s = v.k / n, now = Date.now();
    const tx0 = Math.floor((v.cx - W / 2 / v.k) * n), tx1 = Math.floor((v.cx + W / 2 / v.k) * n), ty0 = Math.floor((v.cy - H / 2 / v.k) * n), ty1 = Math.floor((v.cy + H / 2 / v.k) * n);
    if ((tx1 - tx0 + 1) * (ty1 - ty0 + 1) > 150) return true;
    for (let tx = tx0; tx <= tx1; tx++) for (let ty = Math.max(ty0, 0); ty <= Math.min(ty1, n - 1); ty++) {
      const src = url.replace("{s}", "abc"[(((tx + ty) % 3) + 3) % 3]).replace("{z}", z).replace("{x}", ((tx % n) + n) % n).replace("{y}", ty);
      let img = MP.imgs.get(src);
      if (img && img.failed && now - img.failed > 30000) { MP.imgs.delete(src); img = null; }  // a tile which did not come is asked for again later
      if (!img) {
        img = new Image(); img.referrerPolicy = "no-referrer";  // the tile site learns the tile, not this server's address
        img.onload = soon; img.onerror = () => { img.failed = Date.now(); };
        img.src = src; MP.imgs.set(src, img);
        if (MP.imgs.size > 200) MP.imgs.delete(MP.imgs.keys().next().value);
      }
      if (img.complete && img.naturalWidth) { const [px, py] = toPx(tx / n, ty / n); mctx.drawImage(img, px, py, s + 0.5, s + 0.5); }
    }
    if (!light() && !b.dark) { mctx.fillStyle = "rgba(0,0,0,0.5)"; mctx.fillRect(0, 0, W, H); }  // light tiles under a dark page
    return true;
  }
  // The background layers change only with the map's view, so they are drawn once for each and kept.
  function drawLayers(W, H) {
    const on = Object.entries(MP.layers).filter(([, L]) => L.on && L.shapes); if (!on.length || !PRJ.geo) return;
    const v = MP.view, dpr = window.devicePixelRatio || 1, key = [v.cx, v.cy, v.k, W, H, dpr, C.dim, ...on.map(([n]) => n)].join(",");
    if (!MP.under || MP.under.key !== key) {
      const cvs = (MP.under && MP.under.cvs) || document.createElement("canvas"), c = cvs.getContext("2d");
      cvs.width = mcv.width; cvs.height = mcv.height; c.setTransform(dpr, 0, 0, dpr, 0, 0);
      const x0 = v.cx - W / 2 / v.k, x1 = v.cx + W / 2 / v.k, y0 = v.cy - H / 2 / v.k, y1 = v.cy + H / 2 / v.k;
      for (const [, L] of on) for (const sh of L.shapes) {
        if (sh.box[2] < x0 || sh.box[0] > x1 || sh.box[3] < y0 || sh.box[1] > y1) continue;
        c.beginPath();
        for (const ring of sh.rings) for (let i = 0; i < ring.length; i += 2) { const [px, py] = toPx(ring[i], ring[i + 1]); if (i) c.lineTo(px, py); else c.moveTo(px, py); }
        if (sh.poly) { c.globalAlpha = 0.42; c.fillStyle = sh.fill || "#999"; c.fill("evenodd"); c.globalAlpha = 1; }
        else { c.strokeStyle = sh.stroke || C.dim; c.lineWidth = 1.2; c.stroke(); }
      }
      MP.under = { key, cvs };
    }
    mctx.drawImage(MP.under.cvs, 0, 0, W, H);
  }
  // Strokes the path between two distances. A colour which changes along it changes where it does:
  // each piece between control points is cut at `edges`, the distances at which a label starts or ends.
  function strokePath(p, from, to, width, colourAt, edges) {
    mctx.lineWidth = width; mctx.lineCap = "round"; mctx.lineJoin = "round";
    const fn = typeof colourAt === "function"; let run, open = false, e = 0;
    const end = () => { if (open) mctx.stroke(); open = false; };
    for (let i = 0; i + 1 < p.d.length; i++) {
      const d0 = p.d[i], d1 = p.d[i + 1];
      if (!joined(p, i) || d1 < from || d0 > to || d1 <= d0) { end(); continue; }
      const a = Math.max(d0, from), b = Math.min(d1, to), cuts = [a];
      if (edges) { while (e < edges.length && edges[e] <= a) e++; for (let j = e; j < edges.length && edges[j] < b; j++) cuts.push(edges[j]); }
      cuts.push(b);
      for (let q = 0; q + 1 < cuts.length; q++) {
        const u = cuts[q], w = cuts[q + 1], c = (fn ? colourAt((u + w) / 2) : colourAt) || C.tick;
        const at = (d) => { const f = (d - d0) / (d1 - d0); return toPx(p.x[i] + f * (p.x[i + 1] - p.x[i]), p.y[i] + f * (p.y[i + 1] - p.y[i])); };
        if (c !== run || !open || u > d0) { end(); mctx.beginPath(); mctx.strokeStyle = c; const [ax, ay] = at(u); mctx.moveTo(ax, ay); open = true; run = c; }
        const [bx, by] = at(w); mctx.lineTo(bx, by);
      }
      if (b < d1) end();
    }
    end();
  }
  function drawMap() {
    if (!MP.open || !MP.data) return;
    const [W, H] = size(); if (W < 2 || H < 2) return;
    { const dpr = window.devicePixelRatio || 1; if (mcv.width !== Math.round(W * dpr) || mcv.height !== Math.round(H * dpr)) resizeMap(); }  // the bar above can grow a line
    const a = active();
    const path = a ? a.p : null;
    if (S.group !== MP.lastGroup || !MP.view) { MP.lastGroup = S.group; MP.lastPath = path; if (!MP.stay || !MP.view) fit(false); MP.stay = false; buildControls(); }
    else if (path !== MP.lastPath) { MP.lastPath = path; buildControls(); }  // the time on screen moved onto another epoch of the fibre
    mctx.fillStyle = C.bg; mctx.fillRect(0, 0, W, H);
    if (!MP.view) {
      mctx.fillStyle = C.dim; mctx.font = "14px system-ui"; mctx.textAlign = "left";
      mctx.fillText(MP.data.status === "unreadable" ? "The inventory could not be read; the server's log says why." : "The inventory places no fibre you may see.", 16, 28);
      renderLegend(null); return;
    }
    const tiles = drawBasemap(W, H);
    drawLayers(W, H);
    for (const p of MP.data.paths) if (p.placed && (!a || p !== a.p)) strokePath(p, -Infinity, Infinity, 2, tiles ? "#5d6167" : C.dim);
    let legend = null;
    if (a && a.p.placed) {
      const P = plotRect(), cov = coverage(a) || [-Infinity, Infinity], paint = painter(a), casing = light() ? "#ffffff" : "#000000";
      const edge = (y) => toOptical(a, clamp(y, a.r.from.length ? Math.min(...a.r.from) : -Infinity, a.r.from.length ? Math.max(...a.r.from) : Infinity));
      const w0 = edge(S.view.y0), w1 = edge(S.view.y0 + P.h * S.view.ypp);
      legend = paint.legend;
      strokePath(a.p, -Infinity, Infinity, 3.5, casing);                    // the whole fibre, thin: the recording covers the thick part only
      strokePath(a.p, -Infinity, Infinity, 1.5, tiles ? "#5d6167" : C.dim);
      if (w0 !== null && w1 !== null) { mctx.globalAlpha = 0.55; strokePath(a.p, Math.min(w0, w1), Math.max(w0, w1), 19, C.win); mctx.globalAlpha = 1; }  // the stretch the data shows
      strokePath(a.p, cov[0], cov[1], 8, casing);                           // a casing, so any colour reads on any ground
      strokePath(a.p, cov[0], cov[1], 5, (d) => paint.at(d) || (MP.colour ? C.tick : THEME.blue), paint.edges);
      const mark = (d, r, fill) => { const at = d === null ? null : positionAt(a.p, d); if (!at) return; const [px, py] = toPx(at[0], at[1]); mctx.beginPath(); mctx.arc(px, py, r, 0, 6.2832); mctx.fillStyle = fill; mctx.fill(); mctx.lineWidth = 2; mctx.strokeStyle = "#000"; mctx.stroke(); };
      if (w0 !== null && w1 !== null) { mark(w0, 3.5, C.win); mark(w1, 3.5, C.win); }
      if (metresPerPx() * (PRJ.m || 1) <= 25) for (const t of a.p.tracks.coupling || []) {  // fibre gathered in one place; zoomed out, the rings would hide the line
        if (t[2] !== "coiled") continue;
        const at = positionAt(a.p, (t[0] + t[1]) / 2); if (!at) continue;
        const [px, py] = toPx(at[0], at[1]);
        mctx.beginPath(); mctx.arc(px, py, clamp(2 + Math.sqrt(t[1] - t[0]) / 5, 3, 7), 0, 6.2832);
        mctx.fillStyle = C.bg; mctx.fill(); mctx.lineWidth = 1.4; mctx.strokeStyle = C.text; mctx.stroke();
      }
      if (MP.wf !== null && !MP.hover) mark(toOptical(a, MP.wf), 6, C.win);
      if (MP.hover && MP.hover.path === a.p) mark(MP.hover.d, 5, "#fff");
      else if (pinned(a)) mark(MP.pin.d, 6, "#fff");
    } else if (a) {
      mctx.fillStyle = C.dim; mctx.font = "14px system-ui"; mctx.textAlign = "left"; mctx.fillText("The inventory states no map position for this fibre.", 16, 28);
    }
    drawScale(W, H, !!legend && W < 360);  // in a narrow pane the legend takes the bottom, so the scale goes up
    renderLegend(legend);
    const b = MP.data.basemap, credit = [tiles && b.attribution, ...Object.values(MP.layers).filter((L) => PRJ.geo && L.on && L.credit).map((L) => L.credit)].filter(Boolean);
    $("mapCredit").textContent = credit.join(" · ");
  }
  function drawScale(W, H, high) {
    const mPerPx = metresPerPx();
    const len = niceNumber(mPerPx * 90), px = len / mPerPx, x = W - px - 14, y = high ? 46 : H - 26;
    mctx.strokeStyle = C.text; mctx.lineWidth = 2; mctx.beginPath(); mctx.moveTo(x, y - 4); mctx.lineTo(x, y); mctx.lineTo(x + px, y); mctx.lineTo(x + px, y - 4); mctx.stroke();
    mctx.fillStyle = C.text; mctx.font = "12px system-ui"; mctx.textAlign = "center"; mctx.fillText(PRJ.unit !== "m" ? `${len} ${PRJ.unit}`.trim() : len >= 1000 ? `${len / 1000} km` : `${len} m`, x + px / 2, y - 7);
  }
  function renderLegend(l) {
    const el = $("mapLegend");
    if (!l) { el.hidden = true; return; }
    const key = JSON.stringify(l); if (el.dataset.key === key) { el.hidden = false; return; }
    el.dataset.key = key; el.hidden = false;
    let html = `<b>${esc(l.title)}</b>`;
    if (l.items) html += l.items.map(([v, c]) => `<span><i style="background:${c}"></i>${esc(v)}</span>`).join("");
    else html += `<span class="grad" style="background:linear-gradient(90deg,${l.stops.join(",")})"></span><span class="ends"><em>${fmtNum(+(+l.lo).toPrecision(3))}</em><em>${fmtNum(+(+l.hi).toPrecision(3))}</em></span>`;
    if (l.none) html += `<span><i style="background:${C.tick}"></i>${esc(l.none)}</span>`;
    el.innerHTML = html;
  }
  const pinned = (a) => !!MP.pin && MP.pin.path === a.p && performance.now() < MP.pin.until;
  // The same colour beside the data: a strip inside the plot's left edge, and the place the map points at.
  function overlay() {
    const a = active(); if (!MP.open || !a || !S.levels.length) return;
    const P = plotRect();
    if (MP.colour && MP.colour !== "view") {
      const paint = painter(a);
      for (let y = 0; y < P.h; y += 2) {
        const d = toOptical(a, px2y(P.y + y + 1)), c = d === null ? null : paint.at(d);
        ctx.fillStyle = c || C.tick; ctx.fillRect(P.x, P.y + y, 9, 2);
      }
      ctx.strokeStyle = "#000"; ctx.lineWidth = 1; ctx.strokeRect(P.x + 0.5, P.y + 0.5, 9, P.h - 1);
    }
    const at = MP.hover && MP.hover.path === a.p ? MP.hover.d : pinned(a) ? MP.pin.d : null;
    if (at !== null) {
      const d = toProduct(a, at), y = d === null ? -1 : y2px(d);
      if (y >= P.y && y <= P.y + P.h) {
        ctx.save(); ctx.setLineDash([6, 4]); ctx.lineWidth = 3; ctx.strokeStyle = "#000"; ctx.beginPath(); ctx.moveTo(P.x, y); ctx.lineTo(P.x + P.w, y); ctx.stroke();
        ctx.lineWidth = 1.5; ctx.strokeStyle = "#fff"; ctx.stroke(); ctx.restore();
      }
    }
  }
  // The map draws after the data, and a fault in it must not take the data's drawing with it.
  const draw0 = window.draw;
  window.draw = function () { draw0(); try { overlay(); drawMap(); } catch (e) { console.error("map:", e); } };

  // ---------------------------------------------------------------- controls
  function buildControls() {
    const a = active(), sel = $("mapColour"), was = MP.colour || kept("colour") || "";  // the choice kept from last time waits for its recording
    const opt = ([v, t]) => `<option value="${esc(v)}">${esc(t)}</option>`, nice = (s) => s.replace(/_/g, " ");
    const all = [["", "plain"]], tracks = a ? Object.keys(a.p.tracks).sort().map((g) => ["track:" + g, nice(g)]) : [], cols = a ? Object.keys(a.p.columns).sort().map((c) => ["col:" + c, nice(c)]) : [];
    let html = opt(all[0]);
    if (tracks.length) html += `<optgroup label="labels in the inventory">${tracks.map(opt).join("")}</optgroup>`;
    if (cols.length) html += `<optgroup label="measured along the fibre">${cols.map(opt).join("")}</optgroup>`;
    html += `<optgroup label="the data">${opt(["view", "data on screen (mean along time)"])}</optgroup>`;
    sel.innerHTML = html;
    MP.colour = [...all, ...tracks, ...cols, ["view"]].some(([v]) => v === was) ? was : ""; sel.value = MP.colour;
    const name = $("mapName"), note = $("mapNote");
    if (!a) { name.textContent = MP.data.paths.length ? "this recording is not in the inventory" : ""; note.textContent = ""; note.hidden = true; return; }
    const L = +(+a.p.length || 0).toFixed(1), to = a.p.reach ? +a.p.reach[1].toFixed(1) : 0;
    name.textContent = `${a.p.name} · ${a.p.reach && to < L - grain(a.p) / 10 ? `placed to ${metres(to)} of ${metres(L)}` : metres(L)}`;
    note.textContent = [a.p.about, a.r.about].filter(Boolean).join(" "); note.hidden = !note.textContent; note.title = "from the inventory; press to read it all";
    note.classList.remove("all"); note.setAttribute("aria-expanded", "false");
  }
  function buildLayerBoxes() {
    const box = $("mapLayers"), d = MP.data; let html = "";
    if (!PRJ.geo) {  // web maps and layers are placed by longitude and latitude, which this inventory does not use
      box.innerHTML = `<span title="The inventory's coordinates are not longitude and latitude, so no web map or background layer can be laid under them.">${esc(PRJ.labels.join(", "))}${PRJ.unit ? " in " + esc(PRJ.unit) : ""}${d.frame.name ? " · " + esc(d.frame.name) : ""}</span>`;
      return;
    }
    if (d.basemap) html += `<label title="Loads map tiles from the internet (${esc(d.basemap.attribution || "a web map")}): that site sees your address and which places you look at. Off until you tick it; this browser then remembers the tick."><input type="checkbox" id="mapBase" ${MP.basemap ? "checked" : ""}> web map</label>`;
    for (const name of d.layers) html += `<label><input type="checkbox" data-layer="${esc(name)}" ${kept("layer." + name) === "1" ? "checked" : ""}> ${esc(name.replace(/_/g, " "))}</label>`;
    box.innerHTML = html;
  }
  function loadTicked() { for (const el of $("mapLayers").querySelectorAll("[data-layer]")) if (el.checked && !(MP.layers[el.dataset.layer] || {}).shapes) toggleLayer(el.dataset.layer, true); }
  async function toggleLayer(name, on) {
    const L = (MP.layers[name] ||= { on: false, shapes: null, credit: "" }); L.on = on; keep("layer." + name, on ? "1" : "0"); MP.under = null;
    if (on && !L.shapes) {
      try {
        const g = await api(`/viewer/map/layer/${encodeURIComponent(name)}`), shapes = [];
        const add = (poly, lines, pr) => {
          const box = [Infinity, Infinity, -Infinity, -Infinity];
          const rings = lines.map((r) => { const o = new Float64Array(r.length * 2); r.forEach((c, i) => { const x = mx(c[0]), y = my(c[1]); o[2 * i] = x; o[2 * i + 1] = y; box[0] = Math.min(box[0], x); box[1] = Math.min(box[1], y); box[2] = Math.max(box[2], x); box[3] = Math.max(box[3], y); }); return o; });
          shapes.push({ poly, rings, box, fill: pr.fill, stroke: pr.stroke, name: pr.name });
        };
        for (const f of g.features || []) {
          const ge = f.geometry, pr = f.properties || {}; if (!ge) continue;
          if (ge.type === "Polygon") add(true, ge.coordinates, pr);
          else if (ge.type === "MultiPolygon") for (const p of ge.coordinates) add(true, p, pr);
          else if (ge.type === "LineString") add(false, [ge.coordinates], pr);
          else if (ge.type === "MultiLineString") add(false, ge.coordinates, pr);
        }
        L.shapes = shapes; L.credit = typeof g.attribution === "string" ? g.attribution : "";
      } catch (e) {
        toast(`layer ${name}: ${e.message}`); L.on = false; keep("layer." + name, "0");
        const el = $("mapLayers").querySelector(`[data-layer="${CSS.escape(name)}"]`); if (el) el.checked = false;
      }
    }
    MP.under = null; soon();
  }
  // What a background layer says of the place under the cursor: the name of its shape there.
  function groundAt(p) {
    const v = MP.view, [W, H] = size(), x = v.cx + (p.x - W / 2) / v.k, y = v.cy + (p.y - H / 2) / v.k, out = [];
    for (const [name, L] of Object.entries(MP.layers)) {
      if (!L.on || !L.shapes || !PRJ.geo) continue;
      for (const sh of L.shapes) {
        if (!sh.poly || !sh.name || x < sh.box[0] || x > sh.box[2] || y < sh.box[1] || y > sh.box[3]) continue;
        let inside = false;
        for (const r of sh.rings) for (let i = 0, j = r.length - 2; i < r.length; j = i, i += 2) if ((r[i + 1] > y) !== (r[j + 1] > y) && x < ((r[j] - r[i]) * (y - r[i + 1])) / (r[j + 1] - r[i + 1]) + r[i]) inside = !inside;
        if (inside) { out.push(`${name.replace(/_/g, " ")}: ${sh.name}`); break; }
      }
    }
    return out;
  }
  // The pane's width is kept as a share of the window, and always leaves the data room.
  function setWidth(share) {
    const w = window.innerWidth, px = clamp(share * w, Math.min(200, w / 2), Math.max(Math.min(200, w / 2), w - 320));
    pane.style.width = Math.round(px) + "px"; return px / w;
  }
  // Changing the pane's width keeps the stretch of time on screen: the data is rescaled, not cut or left blank.
  function relayout(change) {
    const had = S.levels.length ? plotRect().w * S.view.spp : 0;
    change(); resize();
    if (had) { const w = plotRect().w; if (w > 10) S.view.spp = had / w; }
    if (MP.open) { resizeMap(); if (MP.view && MP.fitted) fit(MP.fitted === "all"); }
    redraw();
  }
  // `passing`: the window's width opened or shut the pane, not the user, whose choice is kept as it was.
  function show(open, passing) {
    relayout(() => { MP.open = open; pane.hidden = !open; if (open) setWidth(+kept("share") || 0.38); });
    $("btnMap").setAttribute("aria-pressed", open); if (!passing) { keep("open", open ? "1" : "0"); MP.shut = false; }
    if (open) { loadTicked(); if (!MP.view) fit(false); }
  }

  // ---------------------------------------------------------------- mouse
  const mpos = (ev) => { const r = mcv.getBoundingClientRect(); return { x: ev.clientX - r.left, y: ev.clientY - r.top }; };
  // The fibre under the cursor: the nearest point of any drawn line, between control points too. The fibre on
  // show wins a near tie. `also` lists the other stretches of the same fibre which pass the same spot.
  function nearest(p) {
    const a = active(), TOL = 14; let best = null; const mine = [];
    for (const path of MP.data.paths) {
      if (!path.placed) continue;
      const bias = a && path === a.p ? 0 : 5;
      for (let i = 0; i + 1 < path.d.length; i++) {
        if (!joined(path, i)) continue;
        const [ax, ay] = toPx(path.x[i], path.y[i]), [bx, by] = toPx(path.x[i + 1], path.y[i + 1]), vx = bx - ax, vy = by - ay, L2 = vx * vx + vy * vy;
        const f = L2 ? clamp(((p.x - ax) * vx + (p.y - ay) * vy) / L2, 0, 1) : 0.5, r = Math.hypot(p.x - ax - f * vx, p.y - ay - f * vy);
        if (r > TOL) continue;
        const hit = { path, i, d: path.d[i] + f * (path.d[i + 1] - path.d[i]), r: r + bias };
        hit.score = hit.r - (!L2 && gathered(path, i) ? 0.5 : 0);  // on a tie, the fibre held in one spot: it is most of what lies there
        if (!best || hit.score < best.score) best = hit;
        if (!bias) mine.push(hit);
      }
    }
    if (!best) return null;
    best.gather = gathered(best.path, best.i);
    // further along the fibre than the cursor's own reach, and than a small part of the fibre: another pass, not a neighbour
    const far = Math.max(Math.min(150, (best.path.length || 3000) / 20), 3 * TOL * metresPerPx() * (PRJ.m || 0));
    const others = mine.filter((h) => best.path === h.path && Math.abs(h.d - best.d) > far && !(best.gather && h.d >= best.gather[0] && h.d <= best.gather[1])).sort((x, y) => x.d - y.d);
    best.also = [];
    for (const h of others) { const last = best.also[best.also.length - 1]; if (last && h.d - last.d1 < far) { last.d1 = h.d; if (h.r < last.r) { last.r = h.r; last.d = h.d; } } else best.also.push({ d: h.d, d1: h.d, r: h.r }); }
    return best;
  }
  function describe(hit) {
    const p = hit.path, d = hit.d, lines = [`${about(d, p)} along the fibre`];
    if (hit.gather) lines.push(`${about(hit.gather[0], p)} to ${about(hit.gather[1], p).slice(2)} of fibre lies along ${metres(+hit.gather[2].toPrecision(2))} of this line`);
    if (hit.also.length) lines.push(`the fibre also passes here at ${hit.also.slice(0, 4).map((h) => about(h.d, p)).join(", ")}${hit.also.length > 4 ? " ..." : ""}`);
    const seg = (p.breaks || []).filter((b) => b <= hit.i).length - 1, said = (p.notes || [])[seg];
    if (said) lines.push(said);
    for (const g of Object.keys(p.tracks)) { const v = trackValue(p, g, d); if (v !== null) lines.push(`${g.replace(/_/g, " ")}: ${v}`); }
    for (const [n, c] of Object.entries(p.columns)) { const v = columnValue(p, n, d); if (v !== null) lines.push(`${n.replace(/_/g, " ")}: ${fmtNum(+v.toPrecision(4))}${c.units ? " " + c.units : ""}`); }
    return lines;
  }
  // The recording of a fibre which covers a place on it: the one on show if it does, else the one reaching furthest.
  function recordingAt(path, d) {
    const fits = path.recordings.filter((r) => covers(r, d)); if (!fits.length) return null;
    const len = (r) => { const s = span(r); return s[1] - s[0]; };
    return fits.find((r) => r.key === S.group) || fits.sort((x, y) => len(y) - len(x))[0];
  }
  // Shows a place of the fibre in the data, at the zoom the data had, and marks it for a moment on both.
  function jump(hit) {
    const rec = recordingAt(hit.path, hit.d);
    if (!rec) { toast(`No recording of ${hit.path.name} covers ${about(hit.d, hit.path)}.`); return; }
    const go = (ypp) => {
      const b = active(), y = b ? toProduct(b, hit.d) : null; if (y === null) return;
      pushView(); moved();
      if (ypp) S.view.ypp = Math.min(ypp, S.view.ypp);  // a recording just opened shows all of itself: keep the zoom instead
      S.view.y0 = y - (plotRect().h * S.view.ypp) / 2; clampPos();
      MP.pin = { path: b.p, d: hit.d, until: performance.now() + 4000 }; setTimeout(redraw, 4100); redraw();
    };
    if (rec.key === S.group) { go(); return; }
    const ypp = S.view.ypp;
    MP.hover = null; tip.style.display = "none"; MP.stay = true;  // the map stays where the click was made
    setGroup(rec.key).then(() => { toast(`Opened ${rec.key}, which covers ${about(hit.d, hit.path)}.`); go(ypp); }).catch((e) => { MP.stay = false; toast(`could not open ${rec.key}: ${e.message}`); });
  }
  mcv.addEventListener("mousedown", (ev) => { if (MP.view) MP.drag = { p: mpos(ev), cx: MP.view.cx, cy: MP.view.cy, moved: false }; });
  window.addEventListener("mouseup", (ev) => {
    const d = MP.drag; MP.drag = null; if (!d || d.moved || ev.target !== mcv) return;
    const hit = nearest(mpos(ev)); if (hit) jump(hit);
  });
  window.addEventListener("mousemove", (ev) => {
    if (!MP.open || !MP.data) return;
    if (MP.drag) {
      const p = mpos(ev), d = MP.drag; if (Math.hypot(p.x - d.p.x, p.y - d.p.y) > 4) d.moved = true;
      if (d.moved) { MP.fitted = null; MP.view.cx = d.cx - (p.x - d.p.x) / MP.view.k; MP.view.cy = d.cy - (p.y - d.p.y) / MP.view.k; soon(); }
      return;
    }
    if (ev.target === mcv && MP.view) {
      const p = mpos(ev), a = active(), hit = nearest(p), was = MP.hover, lines = [];
      MP.hover = hit ? { path: hit.path, d: hit.d } : null; MP.wf = null;
      mcv.style.cursor = hit ? "pointer" : "grab";
      if (hit) {
        const mine = a && hit.path === a.p, rec = recordingAt(hit.path, hit.d);
        if (!mine) lines.push(hit.path.name);
        lines.push(...describe(hit));
        lines.push(!rec ? "no recording covers this place" : rec.key === S.group ? "click: show this place in the data" : `click: open ${rec.key}`);
      }
      lines.push(...groundAt(p));
      if (lines.length) {
        tip.textContent = lines.join("\n"); tip.style.display = "block";
        tip.style.left = clamp(p.x + 14, 0, Math.max(0, mcv.clientWidth - tip.offsetWidth - 4)) + "px";
        tip.style.top = (p.y + 14 + tip.offsetHeight > mcv.clientHeight ? Math.max(0, p.y - tip.offsetHeight - 10) : p.y + 14) + "px";
      } else tip.style.display = "none";
      if ((was && was.d) !== (MP.hover && MP.hover.d)) redraw();
    } else {
      if (MP.hover || tip.style.display !== "none") { MP.hover = null; tip.style.display = "none"; redraw(); }
      const p = evPos(ev), wf = ev.target === cv && inPlot(p) ? px2y(p.y) : null;
      if (wf !== MP.wf) { MP.wf = wf; soon(); }
    }
  });
  function zoom(f, p) {
    const [W, H] = size(), v = MP.view, wx = v.cx + (p.x - W / 2) / v.k, wy = v.cy + (p.y - H / 2) / v.k;
    MP.fitted = null;
    v.k = clamp(v.k * f, PRJ.geo ? 200 : (MP.k0 || v.k) / 1000, PRJ.geo ? 2 ** 28 : (MP.k0 || v.k) * 1e6); v.cx = wx - (p.x - W / 2) / v.k; v.cy = wy - (p.y - H / 2) / v.k; soon();
  }
  mcv.addEventListener("wheel", (ev) => { if (!MP.view) return; ev.preventDefault(); zoom(Math.exp(-ev.deltaY * 0.0015), mpos(ev)); }, { passive: false });
  mcv.addEventListener("dblclick", () => { fit(false); soon(); });
  // With the map in focus (a click on it), the keys which move the data move the map instead.
  mcv.addEventListener("keydown", (ev) => {
    if (ev.key === "Tab") { ev.stopPropagation(); return; }  // Tab leaves the map, as it leaves any control
    if (!MP.view || ev.ctrlKey || ev.metaKey || ev.altKey) return;
    const [W, H] = size(), step = (ev.shiftKey ? 0.5 : 0.1) / MP.view.k, k = ev.key;
    if (k.startsWith("Arrow")) MP.fitted = null;
    if (k === "ArrowLeft") MP.view.cx -= W * step; else if (k === "ArrowRight") MP.view.cx += W * step;
    else if (k === "ArrowUp") MP.view.cy -= H * step; else if (k === "ArrowDown") MP.view.cy += H * step;
    else if (k === "+" || k === "=") zoom(1.4, { x: W / 2, y: H / 2 }); else if (k === "-") zoom(1 / 1.4, { x: W / 2, y: H / 2 });
    else if (k === "w") fit(false); else if (k === "Escape") { cv.focus({ preventScroll: true }); return; } else return;
    ev.preventDefault(); ev.stopPropagation(); soon();
  });
  $("btnMap").addEventListener("click", () => show(!MP.open));
  $("mapFit").addEventListener("click", () => { fit(false); soon(); });
  $("mapAll").addEventListener("click", () => { fit(true); soon(); });
  const unfold = (el) => el.setAttribute("aria-expanded", el.classList.toggle("all"));
  $("mapNote").addEventListener("click", (ev) => unfold(ev.currentTarget));
  $("mapNote").addEventListener("keydown", (ev) => { if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); ev.stopPropagation(); unfold(ev.currentTarget); } });
  $("mapColour").addEventListener("change", (ev) => { MP.colour = ev.target.value; keep("colour", MP.colour); ev.target.blur(); cv.focus({ preventScroll: true }); redraw(); });
  $("mapLayers").addEventListener("change", (ev) => {
    if (ev.target.id === "mapBase") { MP.basemap = ev.target.checked; keep("base", MP.basemap ? "1" : "0"); soon(); }
    else if (ev.target.dataset.layer) toggleLayer(ev.target.dataset.layer, ev.target.checked);
  });
  // a press on a button or tick box of the map hands the keys back to the data, as the page's other bars do
  $("mapBar").addEventListener("click", (ev) => { if (ev.target.closest("button, input[type=checkbox], label") && !ev.target.closest("select")) cv.focus({ preventScroll: true }); });
  // the edge between the map and the data drags; a double-click puts it back
  $("mapEdge").addEventListener("mousedown", (ev) => {
    ev.preventDefault(); let share = null;  // a press which drags nowhere changes nothing
    const move = (e) => relayout(() => { share = setWidth(e.clientX / window.innerWidth); });
    const up = () => { window.removeEventListener("mousemove", move); window.removeEventListener("mouseup", up); if (share !== null) keep("share", share); };
    window.addEventListener("mousemove", move); window.addEventListener("mouseup", up);
  });
  $("mapEdge").addEventListener("dblclick", () => { keep("share", 0.38); relayout(() => setWidth(0.38)); });
  // A window too narrow for both gives the map's room to the data, and the map comes back when there is room again.
  window.addEventListener("resize", () => {
    const room = window.innerWidth >= NARROW;
    if (MP.open && !room) { show(false, true); MP.shut = true; return; }
    if (!MP.open && room && MP.shut) { MP.shut = false; show(true, true); return; }
    if (MP.open) relayout(() => setWidth(+kept("share") || 0.38));
  });

  (async () => {
    try { MP.data = prepare(await api("/viewer/map")); } catch (e) { MP.data = { paths: [], layers: [], basemap: null, status: "none" }; }
    const any = MP.data.paths.some((p) => p.placed);
    $("btnMap").hidden = !(MP.data.paths.length || MP.data.status === "unreadable");  // nothing to show, nothing to press
    MP.basemap = !!MP.data.basemap && kept("base") === "1";  // tiles from the internet only once the user has asked for them
    MP.colour = kept("colour") || "";
    buildLayerBoxes(); buildControls();
    const want = kept("open");
    if (any && window.innerWidth >= NARROW && (want === "1" || (want === null && window.innerWidth >= 900))) show(true);
  })();
})();
