/* FDE Audit dashboard — hash-routed views + run-analysis panel.
   Vanilla JS, no dependencies. Views: overview / activity / opportunities / runs / reports. */

const $ = (s) => document.querySelector(s);
const el = (t, cls, txt) => { const e = document.createElement(t); if (cls) e.className = cls; if (txt != null) e.textContent = txt; return e; };
const esc = (s) => (s == null ? "" : String(s)).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const fmtMin = (m) => m == null ? "—" : (m >= 60 ? (m/60).toFixed(1)+"h" : Math.round(m)+"m");
const tsDay = (ms) => ms ? new Date(ms).toISOString().slice(0,10) : "—";
const OPP = {automate:"Automate",integrate:"Integrate",consolidate:"Consolidate","build-custom":"Build custom",train:"Train",eliminate:"Eliminate"};

async function getJSON(url) { const r = await fetch(url); if (!r.ok) throw new Error(await r.text()); return r.json(); }
async function postJSON(url, body) {
  const r = await fetch(url, {method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify(body||{})});
  if (!r.ok && r.status !== 202) throw new Error(await r.text());
  return r.json();
}

// -- filter state (the topbar selects ARE the state) ------------------------
function personVal() { return $("#personSel").value; }
function deptVal() { return $("#deptSel").value; }
function periodVal() { return $("#periodSel").value; }
function scopeQS() {
  return `person=${encodeURIComponent(personVal())}&department=${encodeURIComponent(deptVal())}`;
}

// -- overview memo (Overview and Activity share one payload) ----------------
let ovMemo = { key: null, data: null };
function invalidateOverview() { ovMemo = { key: null, data: null }; }
async function fetchOverview() {
  const key = `${scopeQS()}&period=${periodVal()}`;
  if (ovMemo.key !== key) {
    const data = await getJSON(`/api/overview?${key}`);
    ovMemo = { key, data };
  }
  const d = ovMemo.data;
  const scope = d.scope.department ? `dept: ${d.scope.department}`
              : (d.scope.person_id ? "one person" : "all people");
  $("#scopeLabel").textContent = `· ${scope} · ${d.scope.period}`;
  updateBadges(d.overview.unprocessed_frames || 0, d.overview.pending_runs || 0);
  return d;
}

function updateBadges(unproc, pending) {
  for (const badge of [$("#prepBadge"), $("#panelBadge")]) {
    badge.hidden = unproc === 0;
    badge.textContent = `${unproc.toLocaleString()} new`;
  }
  // step-2 chip: packets prepared but not yet labeled+committed in Claude Code.
  // Server-derived, so it survives reloads (unlike the in-memory job log).
  const chip = $("#pendingChip");
  chip.hidden = !pending;
  chip.textContent = pending === 1 ? "1 packet waiting" : `${pending} packets waiting`;
}

// -- views -------------------------------------------------------------------
const UNPROC_TIP = "Captured work frames (screenshots) not yet analyzed — newer than the last "+
  "completed analysis. These are what “Prepare vision analysis” (step 1) will bundle up; after "+
  "Claude Code labels the packet (step 2), this returns to 0 and the frames feed the next report. "+
  "Not affected by the Period selector.";

// last-rendered tile numbers (keyed by label) + the filter scope they were
// rendered under, so a live refresh can flash only the numbers that changed.
let tileFlash = { scope: "", vals: {} };

async function renderOverview() {
  const d = await fetchOverview();
  const o = d.overview;
  const unproc = o.unprocessed_frames || 0;
  const daily = d.daily || [];
  // per-tile sparklines: the day-by-day trend of that metric over the set period.
  const work = daily.map(x => x.work_minutes);
  const idle = daily.map(x => x.idle_minutes);
  const imgs = daily.map(x => x.images);
  const captured = daily.map(x => x.work_minutes + x.idle_minutes);
  const tiles = [
    {l:"Work time", n:fmtMin(o.work_minutes), s:`${o.active_frames+o.passive_frames} work frames`, spark:work},
    {l:"Images stored", n:o.images_stored.toLocaleString(), s:`${o.dup_linked.toLocaleString()} de-duplicated`, spark:imgs},
    {l:"Frames captured", n:o.frames.toLocaleString(), s:`${o.days} day(s), ${o.apps} apps`, spark:captured},
    {l:"Awaiting analysis ⓘ", n:unproc.toLocaleString(), s: unproc ? "frames ready for step 1 → 2" : "all frames analyzed", tip:UNPROC_TIP},
    {l:"Idle time", n:fmtMin(o.idle_minutes), s:`${o.idle_frames} idle frames`, spark:idle},
    {l:"Sessions analyzed", n:o.sessions_analyzed.toLocaleString(), s:`${o.frames_analyzed} frames, ${o.low_value_sessions} low-value`},
    {l:"Reclaimable / wk", n:fmtMin(o.reclaimable_minutes_per_week), s:`across ${o.observations_total} observations`, href:"#/opportunities"},
    {l:"Open opportunities", n:o.observations_open.toLocaleString(), s:`of ${o.observations_total} total`, href:"#/opportunities"},
  ];
  // flash a number only when it changed under the SAME filters — i.e. a live
  // data update, not the user switching person/period (which changes everything).
  // Key on the STABLE filter dims only: ts_start/ts_end are `now`-relative and
  // drift every request, so including them would make sameScope always false and
  // the flash would never fire.
  const sc = d.scope || {};
  const scopeKey = JSON.stringify([sc.person_id ?? null, sc.department ?? null, sc.period ?? null]);
  const sameScope = scopeKey === tileFlash.scope;
  tileFlash.scope = scopeKey;
  const box = $("#tiles"); box.innerHTML = "";
  for (const t of tiles) {
    const node = el(t.href ? "a" : "div", "tile");
    if (t.href) node.href = t.href;
    if (t.tip) node.title = t.tip;
    const nEl = el("div", "n", t.n);
    if (sameScope && tileFlash.vals[t.l] !== undefined && tileFlash.vals[t.l] !== t.n) {
      nEl.classList.add("flash");
      nEl.addEventListener("animationend", () => nEl.classList.remove("flash"), { once: true });
    }
    tileFlash.vals[t.l] = t.n;
    node.append(el("div", "l", t.l), nEl, el("div", "s", t.s));
    if (t.spark && t.spark.some(v => v > 0)) {
      const sp = el("div", "spark"); sp.append(sparkline(t.spark)); node.append(sp);
    }
    box.append(node);
  }
  renderApps(d.apps, "#appsMini", 8);
  renderDailyChart(d.daily, "#daysMini", {height: 150});
}

async function renderActivity() {
  const d = await fetchOverview();
  renderApps(d.apps, "#appsFull");
  renderDailyChart(d.daily, "#daysFull", {height: 240});
}

function renderApps(apps, sel, limit) {
  const box = $(sel); box.innerHTML = "";
  if (!apps.length) { box.append(el("div", "empty", "No captured app time in this window.")); return; }
  const shown = limit ? apps.slice(0, limit) : apps;
  const max = Math.max(...apps.map(a => a.minutes), 1);
  for (const a of shown) {
    const row = el("div", "bar-row");
    row.append(el("div", "name", a.app || "—"));
    const track = el("div", "bar-track"); const fill = el("div", "bar-fill");
    fill.style.width = Math.max(2, (a.minutes/max)*100) + "%"; track.append(fill);
    track.title = `${a.app || "—"}: ${fmtMin(a.minutes)}`;
    row.append(track, el("div", "val", fmtMin(a.minutes)));
    box.append(row);
  }
  if (limit && apps.length > shown.length)
    box.append(el("div", "muted", `+ ${apps.length - shown.length} more — see Activity`));
}

// -- charts (SVG, no external lib) -------------------------------------------
const SVGNS = "http://www.w3.org/2000/svg";
function svgEl(tag, attrs) {
  const e = document.createElementNS(SVGNS, tag);
  for (const k in attrs) e.setAttribute(k, attrs[k]);
  return e;
}
// round a max up to a clean 1 / 2 / 5 × 10ⁿ value for a readable y-axis top.
function niceCeil(v) {
  if (v <= 0) return 1;
  const pow = Math.pow(10, Math.floor(Math.log10(v)));
  const n = v / pow;
  const step = n <= 1 ? 1 : n <= 2 ? 2 : n <= 5 ? 5 : 10;
  return step * pow;
}

// "2025-09-01" -> "Sep 01" for axis / tooltip labels.
const MON = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];
function fmtDay(day) {
  const p = (day || "").split("-");
  if (p.length < 3) return day || "";
  return (MON[parseInt(p[1], 10) - 1] || p[1]) + " " + p[2];
}

// Multi-series line time-series (work vs idle minutes/day): angled date axis,
// rotated y-axis unit title, legend, and a crosshair + tooltip hover layer.
// Fills the container width; no external chart library.
function renderDailyChart(days, sel, { height = 240 } = {}) {
  const host = $(sel); host.innerHTML = "";
  if (!days || !days.length) { host.append(el("div", "empty", "No daily data in this window.")); return; }

  const chart = el("div", "chart");
  host.append(chart);   // in the DOM first so we can measure available width
  const W = Math.max(320, Math.round(chart.clientWidth || host.clientWidth || 680));
  const padL = 54, padR = 20, padT = 14, padB = 54;
  const plotW = W - padL - padR, plotH = height - padT - padB, n = days.length;

  const series = [
    { key: "work_minutes", label: "Work", color: "var(--accent)" },
    { key: "idle_minutes", label: "Idle", color: "var(--series2)" },
  ];
  const top = niceCeil(Math.max(1, ...days.flatMap(d => series.map(s => d[s.key] || 0))));
  const xAt = i => padL + (n <= 1 ? plotW / 2 : plotW * i / (n - 1));
  const yAt = v => padT + plotH * (1 - v / top);

  const tip = el("div", "chart-tip");
  const svg = svgEl("svg", { viewBox: `0 0 ${W} ${height}`, width: W, height,
                             role: "img", "aria-label": "Daily work and idle minutes" });

  // y gridlines + labels (5 divisions) and a rotated y-axis unit title
  for (let k = 0; k <= 4; k++) {
    const gv = top * k / 4, yy = yAt(gv);
    svg.append(svgEl("line", { class: "grid-line", x1: padL, y1: yy, x2: W - padR, y2: yy }));
    const lab = svgEl("text", { class: "axis-lab", x: padL - 8, y: yy + 3, "text-anchor": "end" });
    lab.textContent = fmtMin(gv);
    svg.append(lab);
  }
  const cy = padT + plotH / 2;
  const yTitle = svgEl("text", { class: "axis-title", x: 15, y: cy, "text-anchor": "middle",
                                 transform: `rotate(-90 15 ${cy})` });
  yTitle.textContent = "Time / day";
  svg.append(yTitle);

  // x ticks — angled date labels, thinned to ~68px spacing to avoid collisions
  const labEvery = Math.max(1, Math.ceil(n / Math.max(1, Math.floor(plotW / 68))));
  days.forEach((d, i) => {
    if (i % labEvery && i !== n - 1) return;
    const x = xAt(i), yb = padT + plotH + 16;
    const t = svgEl("text", { class: "x-lab", x, y: yb, "text-anchor": "end",
                              transform: `rotate(-35 ${x} ${yb})` });
    t.textContent = fmtDay(d.day);
    svg.append(t);
  });

  // series polylines (single-point windows get a marker so they're visible)
  for (const s of series) {
    const pts = days.map((d, i) => `${xAt(i).toFixed(1)},${yAt(d[s.key] || 0).toFixed(1)}`).join(" ");
    svg.append(svgEl("polyline", { class: "series-line", points: pts, stroke: s.color }));
    if (n === 1) svg.append(svgEl("circle", { cx: xAt(0), cy: yAt(days[0][s.key] || 0), r: 3, fill: s.color }));
  }

  // hover layer: crosshair, per-series focus dots, tooltip
  const cross = svgEl("line", { class: "crosshair", x1: padL, y1: padT, x2: padL, y2: padT + plotH, opacity: 0 });
  svg.append(cross);
  const dots = series.map(s => {
    const c = svgEl("circle", { class: "focus-dot", r: 3.5, fill: s.color, opacity: 0 });
    svg.append(c); return c;
  });
  const hit = svgEl("rect", { class: "hit", x: padL, y: padT, width: plotW, height: plotH });
  svg.append(hit);

  const showAt = (i) => {
    const x = xAt(i);
    cross.setAttribute("x1", x); cross.setAttribute("x2", x); cross.setAttribute("opacity", 1);
    let rows = "";
    series.forEach((s, si) => {
      const v = days[i][s.key] || 0;
      dots[si].setAttribute("cx", x); dots[si].setAttribute("cy", yAt(v)); dots[si].setAttribute("opacity", 1);
      rows += `<div class="row"><span class="dot" style="background:${s.color}"></span>` +
              `<span class="tk">${s.label}</span> <b>${esc(fmtMin(v))}</b></div>`;
    });
    tip.innerHTML = `<div class="dt">${esc(fmtDay(days[i].day))}</div>${rows}` +
                    `<div class="row"><span class="tk">${days[i].images} images</span></div>`;
    const rect = svg.getBoundingClientRect(), scale = rect.width / W;
    tip.style.left = (x * scale) + "px";
    tip.style.top = (padT + 2) + "px";
    tip.style.opacity = 1;
  };
  const hide = () => { cross.setAttribute("opacity", 0); dots.forEach(c => c.setAttribute("opacity", 0)); tip.style.opacity = 0; };
  hit.addEventListener("mousemove", (e) => {
    const rect = svg.getBoundingClientRect(), scale = rect.width / W;
    const mx = (e.clientX - rect.left) / scale;
    let i = n <= 1 ? 0 : Math.round((mx - padL) / (plotW / (n - 1)));
    showAt(Math.max(0, Math.min(n - 1, i)));
  });
  hit.addEventListener("mouseleave", hide);

  chart.append(svg, tip);

  // live tip: a slow-pulsing ring + value pill riding the newest point of each
  // series. Always drawn but hidden by default; CSS reveals it only under
  // body.live-on (set by renderLivePill), so it appears/vanishes the instant
  // live toggles — no chart re-render needed. Purely decorative (pointer-events
  // off) so the hover layer beneath still works. Appended after the svg is in
  // the DOM so getBBox can size each pill to its text.
  {
    const li = n - 1;                 // latest day = rightmost point
    const placed = [];                // pill boxes already laid down, to dodge
    series.forEach((s, si) => {
      const v = days[li][s.key] || 0;
      const tx = xAt(li), ty = yAt(v);
      const g = svgEl("g", { class: "live-tip" });
      g.style.setProperty("--lt-color", s.color);
      g.append(svgEl("circle", { class: "lt-ring", cx: tx, cy: ty, r: 4 }));
      g.append(svgEl("circle", { class: "lt-core", cx: tx, cy: ty, r: 3 }));
      const txt = svgEl("text", { class: "lt-val", "text-anchor": "middle",
                                  "dominant-baseline": "central", x: tx, y: ty });
      txt.textContent = fmtMin(v);
      g.append(txt);
      svg.append(g);                  // in the DOM -> getBBox is reliable now
      const bw = Math.ceil(txt.getBBox().width) + 16, ph = 17;
      const pcx = Math.max(padL + bw / 2, Math.min(W - padR - bw / 2, tx));
      // Work prefers above its dot, Idle below; then try the other side, then
      // further out — first spot that's inside the plot and clear of pills
      // already placed wins (the series tips can sit very close together).
      const yFor = side => side === "above" ? ty - 16 : ty + 16;
      const fits = y => y - ph / 2 >= padT + 1 && y + ph / 2 <= padT + plotH - 1;
      const clear = y => placed.every(b =>
        Math.abs(pcx - b.x) >= (bw + b.w) / 2 + 4 ||
        Math.abs(y - b.y) >= (ph + b.h) / 2 + 4);
      const prefer = si === 0 ? "above" : "below";
      let pcy = yFor(prefer);
      for (const cand of [pcy, yFor(prefer === "above" ? "below" : "above"), ty - 32, ty + 32]) {
        if (fits(cand) && clear(cand)) { pcy = cand; break; }
      }
      placed.push({ x: pcx, y: pcy, w: bw, h: ph });
      const pill = svgEl("rect", { class: "lt-pill", x: pcx - bw / 2, y: pcy - ph / 2,
                                   width: bw, height: ph, rx: ph / 2 });
      g.insertBefore(pill, txt);      // pill behind the text
      txt.setAttribute("x", pcx); txt.setAttribute("y", pcy);
    });
  }

  // legend row (identity never color-alone — swatch + text label)
  const legend = el("div", "chart-legend");
  for (const s of series) {
    const lg = el("div", "lg");
    const sw = el("span", "sw"); sw.style.background = s.color;
    lg.append(sw, el("span", null, s.label));
    legend.append(lg);
  }
  host.append(legend);
}

// tiny area+line sparkline for a stat tile (stretches to the tile's width).
function sparkline(values) {
  const w = 100, h = 34, pad = 3, n = values.length;
  const max = Math.max(...values, 1);
  const xAt = i => n <= 1 ? w / 2 : (i / (n - 1)) * (w - 2) + 1;
  const yAt = v => pad + (h - 2 * pad) * (1 - v / max);
  let line = "";
  values.forEach((v, i) => { line += (i ? "L" : "M") + xAt(i).toFixed(1) + " " + yAt(v).toFixed(1) + " "; });
  const area = line + `L ${xAt(n - 1).toFixed(1)} ${h} L ${xAt(0).toFixed(1)} ${h} Z`;
  const svg = svgEl("svg", { viewBox: `0 0 ${w} ${h}`, preserveAspectRatio: "none" });
  svg.append(svgEl("path", { class: "spark-area", d: area }));
  svg.append(svgEl("path", { class: "spark-line", d: line, "vector-effect": "non-scaling-stroke" }));
  return svg;
}

// -- live capture / fleet ----------------------------------------------------
// Liveness polls on its own ~10s cadence, independent of the Period selector and
// the active view, so the Overview band and toasts stay current everywhere.
const LIVE_POLL_MS = 10000;
let liveState = {};      // device_id -> last-seen status, for toast diffing
let liveTimer = null;

function fmtAgo(ms) {
  if (ms == null) return "never";
  const s = Math.round(ms / 1000);
  if (s < 60) return s + "s";
  if (s < 3600) return Math.floor(s / 60) + "m";
  if (s < 86400) return Math.floor(s / 3600) + "h" + String(Math.floor((s % 3600) / 60)).padStart(2, "0") + "m";
  return Math.floor(s / 86400) + "d";
}
const fmtClock = (ms) => ms ? new Date(ms).toLocaleTimeString([], {hour: "2-digit", minute: "2-digit"}) : "—";

function deviceRow(d) {
  const up = d.status === "up";
  const row = el("div", "dev-row " + (up ? "is-up" : "is-down"));
  const dot = el("span", "dev-dot " + (up ? "up" : "down"));
  const meta = el("div", "dev-meta");
  const name = el("div", "dev-name");
  name.append(el("span", "hn", d.hostname || "—"));
  name.append(el("span", "who", d.person ? ` · ${d.person}` : ""));
  const sub = el("div", "dev-sub muted");
  sub.textContent = up
    ? `capturing${d.last_app ? " · " + d.last_app : ""} · last frame ${fmtAgo(d.silent_ms)} ago`
    : `stopped · silent ${fmtAgo(d.silent_ms)}${d.last_app ? " · last in " + d.last_app : ""}`;
  meta.append(name, sub);
  const stat = el("div", "dev-stat");
  stat.append(el("span", "badge " + (up ? "ok" : "bad"), up ? "capturing" : "stopped"));
  stat.append(el("span", "muted frames", `${(d.frames_24h || 0).toLocaleString()} / 24h`));
  row.append(dot, meta, stat);
  return row;
}

function renderLiveBand(snap) {
  const band = $("#liveBand"); if (!band) return;
  const s = snap.summary;
  band.hidden = s.total === 0;
  band.classList.toggle("all-up", s.total > 0 && s.stopped === 0);
  band.classList.toggle("has-down", s.stopped > 0);
  band.innerHTML = "";
  const head = el("div", "lb-head");
  const big = el("span", "lb-n", `${s.capturing}`);
  head.append(big, el("span", "lb-lab", ` of ${s.total} machine${s.total === 1 ? "" : "s"} capturing`));
  if (s.stopped > 0) head.append(el("span", "lb-warn", `· ${s.stopped} stopped`));
  band.append(head);
  const chips = el("div", "lb-chips");
  for (const d of snap.devices) {
    const chip = el("span", "lb-chip " + (d.status === "up" ? "up" : "down"));
    chip.append(el("span", "dev-dot " + (d.status === "up" ? "up" : "down")));
    chip.append(el("span", null, d.hostname || "—"));
    chip.title = d.status === "up"
      ? `${d.hostname}: capturing, last frame ${fmtAgo(d.silent_ms)} ago`
      : `${d.hostname}: stopped, silent ${fmtAgo(d.silent_ms)}`;
    chips.append(chip);
  }
  band.append(chips);
}

function renderFleetDevices(snap) {
  const box = $("#fleetDevices"); if (!box) return;
  box.innerHTML = "";
  if (!snap.devices.length) { box.append(el("div", "empty", "No devices registered yet. Run the recorder on a machine.")); return; }
  for (const d of snap.devices) box.append(deviceRow(d));
  const up = $("#fleetUpdated");
  if (up) up.textContent = `${snap.summary.capturing}/${snap.summary.total} up · updated ${fmtClock(snap.generated_at)}`;
}

function renderStatusLog(events) {
  const box = $("#statusLog"); if (!box) return;
  box.innerHTML = "";
  if (!events.length) { box.append(el("div", "empty", "No status changes recorded yet.")); return; }
  const tbl = el("table");
  tbl.innerHTML = `<thead><tr><th>When</th><th>Machine</th><th>Change</th><th>Detail</th></tr></thead>`;
  const tb = el("tbody");
  for (const e of events) {
    const up = e.status === "up";
    const when = e.ts ? `${tsDay(e.ts)} ${fmtClock(e.ts)}` : "—";
    const detail = up ? (e.prev_status ? "recovered" : "first seen")
                      : (e.gap_ms ? `after ${fmtAgo(e.gap_ms)} silent` : "");
    const tr = el("tr");
    tr.innerHTML = `<td class="muted">${esc(when)}</td><td>${esc(e.hostname || e.device_id?.slice(0,8) || "—")}`+
      `${e.display_name ? `<div class="muted">${esc(e.display_name)}</div>` : ""}</td>`+
      `<td class="${up ? "st-up" : "st-down"}">${up ? "▲ came up" : "▼ went down"}</td>`+
      `<td class="muted">${esc(detail)}</td>`;
    tb.append(tr);
  }
  tbl.append(tb); box.append(tbl);
}

function toast(msg, kind) {
  const box = $("#toasts"); if (!box) return;
  const t = el("div", "toast " + (kind === "up" ? "t-up" : "t-down"));
  t.append(el("span", "t-dot"), el("span", "t-msg", msg));
  const close = el("button", "t-x", "×"); close.onclick = () => t.remove();
  t.append(close);
  box.append(t);
  requestAnimationFrame(() => t.classList.add("in"));
  setTimeout(() => { t.classList.remove("in"); setTimeout(() => t.remove(), 300); }, 8000);
  // keep the stack bounded
  while (box.children.length > 4) box.firstChild.remove();
}

function applyLive(snap) {
  let anyDown = false;
  for (const d of snap.devices) {
    const was = liveState[d.device_id];
    if (was && was !== d.status) {
      toast(d.status === "up" ? `${d.hostname} started capturing`
                              : `${d.hostname} stopped capturing`, d.status);
    }
    liveState[d.device_id] = d.status;
    if (d.status === "down") anyDown = true;
  }
  const nav = $("#fleetNavDot"); if (nav) nav.hidden = !anyDown;
  renderLiveBand(snap);
  if (!$("#view-fleet").hidden) renderFleetDevices(snap);
}

async function pollLive() {
  try { applyLive(await getJSON("/api/live")); } catch (e) { /* keep the timer alive */ }
}
function startLivePolling() {
  if (liveTimer) return;
  pollLive();
  liveTimer = setInterval(pollLive, LIVE_POLL_MS);
}

// -- live DB stream (SSE): push a soft refresh when the database changes -----
// The server (`/api/events`) watches SQLite's `data_version` and emits a
// `db-changed` event on every commit by another connection — new captured
// frames, a finished analysis job, a logged status change. We coalesce bursts
// (the recorder writes a frame row every few seconds) into one in-place refresh
// of whatever view is on screen, so the numbers and charts stay live without a
// page reload. This complements the ~10s liveness poll above: SSE catches DATA
// changes; polling catches a machine going SILENT (which writes nothing).
let dbStream = null, dbRefreshTimer = null, livePulseTimer = null;
let liveConnected = false;                                   // is the SSE stream open?
let autoRefresh = localStorage.getItem("autoRefresh") !== "0";   // toggle, default on
let pendingChanges = 0;                                      // changes seen while paused

// Paint the "Live" pill from the three bits of state (connected / auto-refresh /
// pending). The pill is a toggle: green "Live" when streaming and live-updating,
// amber "Paused · N" when the user has frozen the view (N = changes waiting),
// muted "Offline" while the stream reconnects.
function renderLivePill() {
  const dot = $("#liveDot"); if (!dot) return;
  const lbl = dot.querySelector(".ld-lbl");
  dot.classList.toggle("is-live", liveConnected && autoRefresh);
  dot.classList.toggle("is-paused", liveConnected && !autoRefresh);
  // gate the chart's pulsing tip on the same live-and-refreshing condition, so
  // pausing or a dropped stream stops the pulse instantly (no re-render needed)
  document.body.classList.toggle("live-on", liveConnected && autoRefresh);
  dot.setAttribute("aria-pressed", autoRefresh ? "true" : "false");
  if (!liveConnected) {
    if (lbl) lbl.textContent = "Offline";
    dot.title = "Live updates: reconnecting…";
  } else if (autoRefresh) {
    if (lbl) lbl.textContent = "Live";
    dot.title = "Live updates on — data streams in as it lands. Click to pause.";
  } else {
    if (lbl) lbl.textContent = pendingChanges ? `Paused · ${pendingChanges}` : "Paused";
    dot.title = pendingChanges
      ? `Auto-refresh paused — ${pendingChanges} change${pendingChanges === 1 ? "" : "s"} waiting. Click to resume and catch up.`
      : "Auto-refresh paused — the view is frozen. Click to resume live updates.";
  }
}

function bumpLive() {   // a quick throb so the user SEES data land, paused or not
  const dot = $("#liveDot"); if (!dot) return;
  dot.classList.add("bump");
  clearTimeout(livePulseTimer);
  livePulseTimer = setTimeout(() => dot.classList.remove("bump"), 900);
}

async function doSoftRefresh() {
  invalidateOverview();
  // badges + scope label refresh regardless of which view is showing
  try { await fetchOverview(); } catch (e) { /* keep the stream alive */ }
  // Don't yank an open detail pane (a run's sessions, a report someone is
  // reading) out from under them — the list behind it and the tiles still
  // updated above. Every other view re-renders in place.
  const { view, id } = currentRoute();
  if (id && (view === "runs" || view === "reports")) return;
  try { await route(); } catch (e) { /* keep the stream alive */ }
}

function onDbChanged() {
  bumpLive();
  if (!autoRefresh) { pendingChanges++; renderLivePill(); return; }   // frozen: just tally
  clearTimeout(dbRefreshTimer);
  dbRefreshTimer = setTimeout(doSoftRefresh, 800);   // coalesce bursts of writes
}

function setAutoRefresh(on) {
  autoRefresh = on;
  localStorage.setItem("autoRefresh", on ? "1" : "0");
  const hadPending = pendingChanges;
  pendingChanges = 0;
  renderLivePill();
  // resuming with changes buffered up -> catch up immediately
  if (on && hadPending) { clearTimeout(dbRefreshTimer); doSoftRefresh(); }
}

function startDbStream() {
  const dot = $("#liveDot");
  if (dot) dot.onclick = () => setAutoRefresh(!autoRefresh);
  renderLivePill();
  if (dbStream || !window.EventSource) return;   // no SSE -> the 10s poll still runs
  const es = new EventSource("/api/events");
  dbStream = es;
  const up = () => { liveConnected = true; renderLivePill(); };
  es.addEventListener("hello", up);
  es.onopen = up;
  es.addEventListener("db-changed", onDbChanged);
  es.onerror = () => { liveConnected = false; renderLivePill(); };   // EventSource auto-reconnects
}

async function renderFleet() {
  try {
    const snap = await getJSON("/api/live");
    applyLive(snap);
    renderFleetDevices(snap);
  } catch (e) { $("#fleetDevices").innerHTML = `<div class="empty">${esc(String(e.message || e))}</div>`; }
  try {
    const d = await getJSON("/api/status-events?limit=50");
    renderStatusLog(d.events);
  } catch (e) { /* log stays as-is */ }
}

async function renderOpportunities() {
  const rep = await getJSON(`/api/report?${scopeQS()}`);
  const box = $("#oppTable"); box.innerHTML = "";
  const opps = rep.opportunities || [];
  $("#oppTotals").textContent = opps.length
    ? `${rep.totals.opportunity_count} opportunities · ~${fmtMin(rep.totals.total_minutes_per_week)}/wk reclaimable` : "";
  if (!opps.length) {
    box.append(el("div", "empty", "No opportunities yet. Capture some work, then follow steps 1 → 2 → 3 in the Run analysis panel (see “Full walkthrough” there for details)."));
  } else {
    const showWho = personVal() === "all";   // org / department view -> attribute per person
    const tbl = el("table");
    tbl.innerHTML = `<thead><tr><th>#</th><th>Type</th><th class="num">Weekly</th><th>Status</th>`+
      `<th class="num">Evid</th>${showWho?"<th>Who</th>":""}<th>Opportunity</th></tr></thead>`;
    const tb = el("tbody");
    for (const o of opps) {
      const tr = el("tr");
      const up = o.suggested_upgrade ? `<div class="muted">→ ${esc(o.suggested_upgrade)}</div>` : "";
      const who = showWho ? `<td>${esc(o.person||"—")}<div class="muted">${esc(o.department||"")}</div></td>` : "";
      tr.innerHTML = `<td>${o.rank}</td><td>${esc(OPP[o.opportunity_type]||o.opportunity_type||"?")}</td>`+
        `<td class="num">${o.est_minutes_per_week?fmtMin(o.est_minutes_per_week):"—"}</td>`+
        `<td class="st-${o.status}">${o.status}</td><td class="num">${o.evidence_count||0}</td>`+
        `${who}<td>${esc(o.description||o.pattern_key)}${up}</td>`;
      tb.append(tr);
    }
    tbl.append(tb); box.append(tbl);
  }
  const con = rep.consolidation || [];
  $("#consolidationCard").hidden = !con.length;
  if (con.length) {
    const cbox = $("#consolidation"); cbox.innerHTML = "";
    const tbl = el("table");
    tbl.innerHTML = `<thead><tr><th>Capability</th><th>Apps</th><th class="num">People</th><th class="num">~Hrs/wk</th></tr></thead>`;
    const tb = el("tbody");
    for (const c of con) {
      const tr = el("tr");
      tr.innerHTML = `<td>${esc(c.capability)}</td><td>${esc(c.apps.join(", "))}</td>`+
        `<td class="num">${c.people_count}</td><td class="num">${c.est_hours_per_week}</td>`;
      tb.append(tr);
    }
    tbl.append(tb); cbox.append(tbl);
  }
}

// -- runs (list + inline detail pane, deep-linked as #/runs/<id>) -------------
async function renderRuns(runId) {
  const d = await getJSON("/api/runs");
  const box = $("#runs"); box.innerHTML = "";
  if (!d.runs.length) {
    box.append(el("div", "empty", "No analysis runs yet."));
  } else {
    const tbl = el("table");
    tbl.innerHTML = `<thead><tr><th>Run</th><th>Status</th><th class="num">Summaries</th><th>Model</th><th>Started</th></tr></thead>`;
    const tb = el("tbody");
    for (const r of d.runs) {
      const tr = el("tr", "clickable" + (r.run_id === runId ? " selected" : ""));
      tr.innerHTML = `<td><code>${esc(r.run_id.slice(0,8))}</code></td>`+
        `<td class="st-${r.status}">${r.status}</td><td class="num">${r.summaries}</td>`+
        `<td>${esc(r.model||"—")}</td><td class="muted">${tsDay(r.started_at)}</td>`;
      tr.onclick = () => { location.hash = `#/runs/${r.run_id}`; };
      tb.append(tr);
    }
    tbl.append(tb); box.append(tbl);
  }
  if (runId) await showRunDetail(runId); else $("#runDetail").hidden = true;
}

async function showRunDetail(runId) {
  const pane = $("#runDetail");
  try {
    const d = await getJSON(`/api/runs/${runId}`);
    $("#runDetailTitle").textContent = `Run ${runId.slice(0,8)} — ${d.run.status}`;
    const body = $("#runDetailBody"); body.innerHTML = "";
    body.append(el("div", "muted", `The data this run's suggestions came from: ${d.summaries.length} labeled session(s).`));
    if (!d.summaries.length) {
      body.append(el("div", "empty", "No summaries — this run is prepared but not yet labeled/committed in Claude Code."));
    } else {
      const tbl = el("table");
      tbl.innerHTML = `<thead><tr><th>App</th><th>Capability</th><th>Task / activity</th><th class="num">Frames</th><th>Flags</th></tr></thead>`;
      const tb = el("tbody");
      for (const s of d.summaries) {
        const flags = [s.low_value ? "low-value" : "", s.friction || ""].filter(Boolean).join(", ");
        const tr = el("tr");
        tr.innerHTML = `<td>${esc(s.app_name||"—")}</td><td>${esc(s.capability||"—")}</td>`+
          `<td>${esc(s.task||s.activity||"—")}</td><td class="num">${s.frame_count}</td>`+
          `<td class="muted">${esc(flags||"—")}</td>`;
        tb.append(tr);
      }
      tbl.append(tb); body.append(tbl);
    }
  } catch (e) {
    $("#runDetailTitle").textContent = "Run not found";
    $("#runDetailBody").innerHTML = `<div class="empty">${esc(String(e.message || e))}</div>`;
  }
  pane.hidden = false;
  pane.scrollIntoView({block: "nearest"});
}

// -- reports / digests (list + inline reading pane, #/reports/<id>) -----------
async function renderReports(digestId) {
  const d = await getJSON(`/api/digests?${scopeQS()}`);
  const box = $("#digests"); box.innerHTML = "";
  if (!d.digests.length) {
    box.append(el("div", "empty", "No saved reports yet. Run steps 1–3 in the Run analysis panel."));
  } else {
    const tbl = el("table");
    tbl.innerHTML = `<thead><tr><th>Report</th><th>Period</th><th>Person</th><th>Generated</th></tr></thead>`;
    const tb = el("tbody");
    for (const g of d.digests) {
      const tr = el("tr", "clickable" + (g.id === digestId ? " selected" : ""));
      tr.innerHTML = `<td><code>${esc(g.short_id)}</code></td><td>${esc(g.period||"—")}</td>`+
        `<td>${esc(g.person||"—")}</td><td class="muted">${esc(g.generated_day||"—")}</td>`;
      tr.onclick = () => { location.hash = `#/reports/${g.id}`; };
      tb.append(tr);
    }
    tbl.append(tb); box.append(tbl);
  }
  if (digestId) await showDigestDetail(digestId); else $("#digestDetail").hidden = true;
}

async function showDigestDetail(id) {
  const pane = $("#digestDetail");
  try {
    const d = await getJSON(`/api/digests/${id}`);
    $("#digestDetailTitle").textContent = `Report ${(d.id||id).slice(0,8)} · ${d.period||""}`;
    $("#digestDetailBody").innerHTML = mdToHtml(d.content_md || "");
  } catch (e) {
    $("#digestDetailTitle").textContent = "Report not found";
    $("#digestDetailBody").innerHTML = `<div class="empty">${esc(String(e.message || e))}</div>`;
  }
  pane.hidden = false;
  pane.scrollIntoView({block: "nearest"});
}

// -- router --------------------------------------------------------------------
const routes = {
  overview: renderOverview,
  fleet: renderFleet,
  activity: renderActivity,
  opportunities: renderOpportunities,
  runs: renderRuns,
  reports: renderReports,
  help: async () => {},   // static content in index.html
};

function currentRoute() {
  const parts = location.hash.replace(/^#\/?/, "").split("/");
  const view = routes[parts[0]] ? parts[0] : "overview";
  return { view, id: parts[1] || null };
}

async function route() {
  const { view, id } = currentRoute();
  document.querySelectorAll(".view").forEach(s => { s.hidden = s.id !== `view-${view}`; });
  document.querySelectorAll(".nav-item").forEach(a => {
    if (a.dataset.view === view) a.setAttribute("aria-current", "page");
    else a.removeAttribute("aria-current");
  });
  try { await routes[view](id); } catch (e) { console.error(e); }
}

async function refreshActive() {
  invalidateOverview();
  // badges come from the overview payload, so refresh it even off-Overview
  try { await fetchOverview(); } catch (e) { console.error(e); }
  await route();
}

// -- actions / jobs ---------------------------------------------------------------
let runningJobs = 0;
function setPulse() { $("#panelPulse").hidden = runningJobs === 0 || !document.body.classList.contains("panel-closed"); }

// while a job of a kind is in flight, its button is disabled and shows a spinner
function setBusy(kind, busy) {
  document.querySelectorAll(`[data-action="${kind}"]`).forEach(b => {
    b.disabled = busy;
    b.classList.toggle("busy", busy);
  });
}

async function runAction(kind) {
  const body = {person: personVal(), all_frames: $("#allFrames").checked};
  if (kind === "digest" || kind === "brief" || kind === "compile") body.period = periodVal();
  setBusy(kind, true);
  try {
    const job = await postJSON(`/api/actions/${kind}`, body);
    runningJobs++; setPulse();
    pollJob(job.id);
  } catch (e) {
    setBusy(kind, false);
    const line = el("div", "jobline");
    line.innerHTML = `<span class="st-failed">failed</span> · ${esc(String(e.message || e))}`;
    $("#joblog").insertBefore(line, $("#joblog").children[1] || null);
  }
}

const jobEls = {};
function jobLine(job) {
  let line = jobEls[job.id];
  if (!line) {
    line = el("div", "jobline"); jobEls[job.id] = line;
    const log = $("#joblog");
    log.insertBefore(line, log.children[1] || null);   // children[0] is the "Job log" heading
  }
  const status = `<span class="st-${job.status}">${job.status}</span>`;
  let extra = "";
  if (job.status === "done" && job.result) {
    // a compile job carries BOTH a saved report link and the next Claude prompt,
    // so these append rather than overwrite.
    if (job.result.digest_id) extra += ` <a href="#/reports/${esc(job.result.digest_id)}">view data report</a>`;
    if (job.result.say_to_claude) extra += ` <code>“${esc(job.result.say_to_claude)}”</code>`;
  }
  line.innerHTML = `${status} · ${esc(job.message || job.kind)}${extra}`;
}

async function pollJob(id) {
  const job = await getJSON(`/api/jobs/${id}`);
  jobLine(job);
  if (job.status === "running") { setTimeout(() => pollJob(id), 700); return; }
  setBusy(job.kind, false);
  runningJobs = Math.max(0, runningJobs - 1); setPulse();
  await refreshActive();   // a finished job may have changed any view
}

// -- action panel collapse ------------------------------------------------------
function setPanel(open) {
  document.body.classList.toggle("panel-closed", !open);
  localStorage.setItem("actionPanelOpen", open ? "1" : "0");
  setPulse();
}
function initPanel() {
  const saved = localStorage.getItem("actionPanelOpen");
  const open = saved != null ? saved === "1" : window.innerWidth >= 1200;
  document.body.classList.toggle("panel-closed", !open);
  $("#panelBtn").onclick = () => setPanel(document.body.classList.contains("panel-closed"));
  $("#panelClose").onclick = () => setPanel(false);
  // click anywhere on the main content area closes the panel (not just the ×)
  $("main").addEventListener("click", () => {
    if (!document.body.classList.contains("panel-closed")) setPanel(false);
  });
}

// -- sidebar collapse (icon rail) — manual toggle, persisted ------------------
function initSidebar() {
  const collapsed = localStorage.getItem("sidebarCollapsed") === "1";
  document.body.classList.toggle("sidebar-collapsed", collapsed);
  const btn = $("#navCollapse");
  const sync = (c) => { btn.title = c ? "Expand sidebar" : "Collapse sidebar"; };
  sync(collapsed);
  btn.onclick = () => {
    const now = document.body.classList.toggle("sidebar-collapsed");
    localStorage.setItem("sidebarCollapsed", now ? "1" : "0");
    sync(now);
  };
}

// -- markdown + theme --------------------------------------------------------------
function mdToHtml(md) {
  const lines = md.split("\n"); let html = ""; let i = 0;
  const inline = (s) => esc(s).replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
    .replace(/\*([^*]+)\*/g, "<em>$1</em>")
    .replace(/`([^`]+)`/g, "<code>$1</code>").replace(/→/g, "→");
  while (i < lines.length) {
    let ln = lines[i];
    if (/^\|(.+)\|$/.test(ln.trim()) && i+1 < lines.length && /^\|[\s:|-]+\|$/.test(lines[i+1].trim())) {
      html += "<table><thead><tr>" + ln.trim().slice(1,-1).split("|").map(c=>`<th>${inline(c.trim())}</th>`).join("") + "</tr></thead><tbody>";
      i += 2;
      while (i < lines.length && /^\|(.+)\|$/.test(lines[i].trim())) {
        html += "<tr>" + lines[i].trim().slice(1,-1).split("|").map(c=>`<td>${inline(c.trim())}</td>`).join("") + "</tr>"; i++;
      }
      html += "</tbody></table>"; continue;
    }
    if (/^### /.test(ln)) html += `<h3>${inline(ln.slice(4))}</h3>`;
    else if (/^## /.test(ln)) html += `<h2>${inline(ln.slice(3))}</h2>`;
    else if (/^# /.test(ln)) html += `<h1>${inline(ln.slice(2))}</h1>`;
    else if (/^---+$/.test(ln.trim())) html += "<hr>";
    else if (/^\s*[-*] /.test(ln)) { html += `<div style="margin-left:14px">• ${inline(ln.replace(/^\s*[-*] /,""))}</div>`; }
    else if (/^\d+\. /.test(ln.trim())) { html += `<div style="margin-left:14px">${inline(ln.trim())}</div>`; }
    else if (ln.trim() === "") html += "";
    else html += `<p>${inline(ln)}</p>`;
    i++;
  }
  return html;
}

function initTheme() {
  // the saved theme was already applied pre-paint by an inline snippet in <head>
  $("#themeBtn").onclick = () => {
    const cur = document.documentElement.getAttribute("data-theme");
    const next = cur === "dark" ? "light" : (cur === "light" ? "dark" : "dark");
    document.documentElement.setAttribute("data-theme", next);
    localStorage.setItem("theme", next);
  };
}

// -- init -----------------------------------------------------------------------------
async function init() {
  initTheme();
  initPanel();
  initSidebar();

  const p = await getJSON("/api/people");
  const sel = $("#personSel"); sel.innerHTML = "";
  const optAll = el("option"); optAll.value = "all"; optAll.textContent = "All people"; sel.append(optAll);
  for (const person of p.people) {
    const o = el("option"); o.value = person.person_id;
    o.textContent = `${person.display_name} (${person.department})`; sel.append(o);
  }
  if (p.default) sel.value = p.default;

  const dsel = $("#deptSel"); dsel.innerHTML = "";
  const dAll = el("option"); dAll.value = "all"; dAll.textContent = "All departments"; dsel.append(dAll);
  for (const dep of (p.departments || [])) {
    const o = el("option"); o.value = dep; o.textContent = dep; dsel.append(o);
  }

  // A person belongs to one department, so the two scopes are mutually exclusive:
  // picking a department views the whole team (person -> all); picking a specific
  // person clears the department filter.
  dsel.onchange = () => { if (deptVal() !== "all") sel.value = "all"; refreshActive(); };
  sel.onchange = () => { if (personVal() !== "all") dsel.value = "all"; refreshActive(); };
  $("#periodSel").onchange = refreshActive;
  $("#refreshBtn").onclick = refreshActive;
  $("#runDetailClose").onclick = () => { location.hash = "#/runs"; };
  $("#digestDetailClose").onclick = () => { location.hash = "#/reports"; };
  document.querySelectorAll("[data-action]").forEach(b => b.onclick = () => runAction(b.dataset.action));

  document.querySelectorAll(".copy-btn").forEach(b => b.onclick = () => copyCmd(b));

  window.addEventListener("hashchange", route);
  await refreshActive();
  startLivePolling();   // real-time capture liveness, independent of the Period filter
  startDbStream();      // push-based refresh whenever the database is written
}

async function copyCmd(btn) {
  const text = btn.dataset.copy;
  try {
    await navigator.clipboard.writeText(text);
  } catch (e) {
    // clipboard API needs a secure context; fall back to a temporary textarea
    const ta = el("textarea"); ta.value = text; ta.style.position = "fixed"; ta.style.opacity = "0";
    document.body.append(ta); ta.select();
    try { document.execCommand("copy"); } catch (_) {}
    ta.remove();
  }
  btn.classList.add("copied");
  setTimeout(() => btn.classList.remove("copied"), 1400);
}

init();
