"use strict";

/* ---------- state ---------- */

const state = {
  hours: 24,
  thresholds: { loss_pct: 20, latency_ms: 250 },
  hosts: [],
  expanded: null,      // host name whose detail panel is open
  histories: {},       // host name -> /api/history payload
  refreshTimer: null,
  hoverHold: false,    // pointer is on a chart: don't re-render under it
  dragging: null,      // host name being dragged, or null
};

// Fallback poll only; live SSE events drive updates between polls.
const REFRESH_MS = 60000;
const LIVE_DEBOUNCE_MS = 1200;

/* ---------- small helpers ---------- */

const $ = (sel, root) => (root || document).querySelector(sel);

function el(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
}

function fmtNum(v, digits) {
  if (v === null || v === undefined || Number.isNaN(v)) return "–";
  return v.toLocaleString(undefined, { maximumFractionDigits: digits, minimumFractionDigits: 0 });
}

function fmtMs(v) { return v == null ? "–" : fmtNum(v, v < 10 ? 2 : 1) + " ms"; }

function fmtMbps(v) {
  if (v == null) return "–";
  if (v >= 1000) return fmtNum(v / 1000, 2) + " Gbit/s";
  return fmtNum(v, v < 10 ? 1 : 0) + " Mbit/s";
}

function fmtPct(v) { return v == null ? "–" : fmtNum(v, v > 0 && v < 10 ? 1 : 0) + "%"; }

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

function fmtClock(ts) {
  const d = new Date(ts * 1000);
  return String(d.getHours()).padStart(2, "0") + ":" + String(d.getMinutes()).padStart(2, "0");
}

function fmtTick(ts, rangeHours) {
  const d = new Date(ts * 1000);
  if (rangeHours <= 24) return fmtClock(ts);
  return MONTHS[d.getMonth()] + " " + d.getDate() + " " + fmtClock(ts);
}

function fmtFull(ts) {
  const d = new Date(ts * 1000);
  return MONTHS[d.getMonth()] + " " + d.getDate() + ", " +
    d.toLocaleTimeString(undefined, { hour12: false });
}

/* Clean tick values for a 0-based axis: returns {max, ticks[]} */
function niceScale(maxVal, count) {
  if (!(maxVal > 0)) maxVal = 1;
  const rough = maxVal / count;
  const mag = Math.pow(10, Math.floor(Math.log10(rough)));
  let step = mag;
  for (const m of [1, 2, 2.5, 5, 10]) {
    if (rough <= m * mag) { step = m * mag; break; }
  }
  const top = Math.ceil(maxVal / step) * step;
  const ticks = [];
  for (let v = 0; v <= top + step / 1e6; v += step) ticks.push(v);
  return { max: top, ticks };
}

const svgNS = "http://www.w3.org/2000/svg";
function svgEl(tag, attrs) {
  const n = document.createElementNS(svgNS, tag);
  for (const k in attrs) n.setAttribute(k, attrs[k]);
  return n;
}

/* ---------- status metadata (icon + label, never color alone) ---------- */

const STATUS = {
  up:       { label: "Up",       color: "var(--status-good)", icon: "M7 0a7 7 0 100 14A7 7 0 007 0zm-1 10L3 7l1.2-1.2L6 7.6l3.8-3.8L11 5z" },
  degraded: { label: "Degraded", color: "var(--status-warn)", icon: "M7 .8L13.7 13H.3L7 .8zM6.3 5h1.4v4H6.3V5zm0 5h1.4v1.5H6.3V10z" },
  down:     { label: "Down",     color: "var(--status-crit)", icon: "M7 0a7 7 0 100 14A7 7 0 007 0zM9.8 8.8l-1 1L7 8 5.2 9.8l-1-1L6 7 4.2 5.2l1-1L7 6l1.8-1.8 1 1L8 7z" },
  unknown:  { label: "No data",  color: "var(--muted)",       icon: "M7 0a7 7 0 100 14A7 7 0 007 0zM6.3 10h1.4v1.5H6.3V10zm2.9-5.2c0 1.6-1.5 1.9-1.5 3H6.3c0-1.7 1.4-1.9 1.4-2.9 0-.5-.4-.8-.9-.8s-.9.4-.9 1H4.4C4.4 3.7 5.5 3 7 3s2.2.8 2.2 1.8z" },
};

function statusIcon(status) {
  const meta = STATUS[status] || STATUS.unknown;
  const svg = svgEl("svg", { viewBox: "0 0 14 14", role: "img" });
  svg.appendChild(svgEl("path", { d: meta.icon, fill: meta.color }));
  return svg;
}

/* ---------- charts ---------- */

/*
 * Time-series line chart (single series): area wash, 2px line, hairline
 * grid, crosshair + tooltip, keyboard navigation. Failed samples (v null,
 * ok false) break the line and get a critical tick at the baseline.
 *
 * opts: { points: [{t, v, ok, extra}], color, unit, seriesName,
 *         fmtV, xDomain: [t0, t1], rangeHours, height }
 */
function timeSeriesChart(container, opts) {
  container.textContent = "";
  const width = Math.max(280, container.clientWidth || 600);
  const height = opts.height || 220;
  const m = { top: 10, right: 14, bottom: 22, left: 48 };
  const iw = width - m.left - m.right;
  const ih = height - m.top - m.bottom;
  const pts = opts.points;

  const svg = svgEl("svg", { width, height, viewBox: `0 0 ${width} ${height}` });
  container.appendChild(svg);

  if (!pts.length) {
    container.appendChild(el("div", "empty-note", "No samples in this range yet."));
    return;
  }

  const [t0, t1] = opts.xDomain;
  const vMax = Math.max(...pts.map(p => (p.v == null ? 0 : p.v)));
  const scale = niceScale(vMax, 4);
  const x = t => m.left + ((t - t0) / (t1 - t0 || 1)) * iw;
  const y = v => m.top + ih - (v / scale.max) * ih;

  // gridlines + y tick labels (skip the 0 line; the baseline covers it)
  for (const tv of scale.ticks) {
    const yy = y(tv);
    if (tv > 0) {
      svg.appendChild(svgEl("line", {
        x1: m.left, x2: m.left + iw, y1: yy, y2: yy,
        stroke: "var(--grid)", "stroke-width": 1,
      }));
    }
    const lab = svgEl("text", {
      x: m.left - 8, y: yy + 4, "text-anchor": "end",
      fill: "var(--muted)", "font-size": 11,
    });
    lab.textContent = fmtNum(tv, 1);
    svg.appendChild(lab);
  }

  // baseline
  svg.appendChild(svgEl("line", {
    x1: m.left, x2: m.left + iw, y1: m.top + ih, y2: m.top + ih,
    stroke: "var(--baseline)", "stroke-width": 1,
  }));

  // x ticks: ~1 per 110px
  const nx = Math.max(2, Math.floor(iw / 110));
  for (let i = 0; i <= nx; i++) {
    const t = t0 + ((t1 - t0) * i) / nx;
    const lab = svgEl("text", {
      x: x(t), y: m.top + ih + 16,
      "text-anchor": i === 0 ? "start" : i === nx ? "end" : "middle",
      fill: "var(--muted)", "font-size": 11,
    });
    lab.textContent = fmtTick(t, opts.rangeHours);
    svg.appendChild(lab);
  }

  // area + line paths, broken at null values
  let line = "", area = "", runStart = null, prev = null;
  const flushArea = (from, to) => {
    if (from == null || to == null) return;
    area += ` L ${x(to.t).toFixed(1)} ${(m.top + ih).toFixed(1)}` +
            ` L ${x(from.t).toFixed(1)} ${(m.top + ih).toFixed(1)} Z`;
  };
  for (const p of pts) {
    if (p.v == null) {
      flushArea(runStart, prev);
      runStart = null; prev = null;
      continue;
    }
    const px = x(p.t).toFixed(1), py = y(p.v).toFixed(1);
    if (prev == null) {
      line += ` M ${px} ${py}`;
      area += ` M ${px} ${py}`;
      runStart = p;
    } else {
      line += ` L ${px} ${py}`;
      area += ` L ${px} ${py}`;
    }
    prev = p;
  }
  flushArea(runStart, prev);
  if (area) svg.appendChild(svgEl("path", { d: area, fill: opts.color, opacity: 0.1 }));
  if (line) svg.appendChild(svgEl("path", {
    d: line, fill: "none", stroke: opts.color, "stroke-width": 2,
    "stroke-linejoin": "round", "stroke-linecap": "round",
  }));

  // lone points (both neighbors missing) would be invisible as a path
  for (let i = 0; i < pts.length; i++) {
    if (pts[i].v == null) continue;
    const loneLeft = i === 0 || pts[i - 1].v == null;
    const loneRight = i === pts.length - 1 || pts[i + 1].v == null;
    if (loneLeft && loneRight) {
      svg.appendChild(svgEl("circle", {
        cx: x(pts[i].t), cy: y(pts[i].v), r: 2.5, fill: opts.color,
      }));
    }
  }

  // down ticks at the baseline
  for (const p of pts) {
    if (p.v == null) {
      svg.appendChild(svgEl("rect", {
        x: x(p.t) - 1.5, y: m.top + ih - 6, width: 3, height: 6,
        fill: "var(--status-crit)", rx: 1,
      }));
    }
  }

  /* --- crosshair + tooltip --- */
  const hair = svgEl("line", {
    y1: m.top, y2: m.top + ih, stroke: "var(--baseline)",
    "stroke-width": 1, visibility: "hidden",
  });
  svg.appendChild(hair);
  const dotRing = svgEl("circle", { r: 6, fill: "var(--surface-1)", visibility: "hidden" });
  const dot = svgEl("circle", { r: 4, fill: opts.color, visibility: "hidden" });
  svg.appendChild(dotRing);
  svg.appendChild(dot);

  const tip = el("div", "tooltip");
  tip.hidden = true;
  container.appendChild(tip);

  const overlay = el("div", "overlay");
  overlay.tabIndex = 0;
  overlay.setAttribute("role", "application");
  overlay.setAttribute("aria-label", (opts.seriesName || "chart") + "; use arrow keys to inspect values");
  overlay.addEventListener("pointerenter", () => { state.hoverHold = true; });
  overlay.addEventListener("pointerleave", () => { state.hoverHold = false; });
  container.appendChild(overlay);

  let active = -1;

  function showIndex(i) {
    active = i;
    const p = pts[i];
    const px = x(p.t);
    hair.setAttribute("x1", px);
    hair.setAttribute("x2", px);
    hair.setAttribute("visibility", "visible");
    if (p.v != null) {
      dot.setAttribute("cx", px); dot.setAttribute("cy", y(p.v));
      dotRing.setAttribute("cx", px); dotRing.setAttribute("cy", y(p.v));
      dot.setAttribute("visibility", "visible");
      dotRing.setAttribute("visibility", "visible");
    } else {
      dot.setAttribute("visibility", "hidden");
      dotRing.setAttribute("visibility", "hidden");
    }

    tip.textContent = "";
    tip.appendChild(el("div", "tt-time", fmtFull(p.t)));
    const row = el("div", "tt-row");
    const key = el("span", "tt-key");
    key.style.color = p.v == null ? "var(--status-crit)" : opts.color;
    row.appendChild(key);
    row.appendChild(el("span", "tt-val", p.v == null ? "Down" : opts.fmtV(p.v)));
    row.appendChild(el("span", "tt-name", opts.seriesName));
    tip.appendChild(row);
    if (p.extra) {
      for (const [name, val] of p.extra) {
        const r2 = el("div", "tt-row");
        r2.appendChild(el("span", "tt-key")).style.visibility = "hidden";
        r2.appendChild(el("span", "tt-val", val));
        r2.appendChild(el("span", "tt-name", name));
        tip.appendChild(r2);
      }
    }
    tip.hidden = false;
    const tw = tip.offsetWidth;
    const left = px + 14 + tw > width ? px - 14 - tw : px + 14;
    tip.style.left = left + "px";
    tip.style.top = Math.max(0, (p.v != null ? y(p.v) : m.top + ih / 2) - 24) + "px";
  }

  function hide() {
    active = -1;
    tip.hidden = true;
    hair.setAttribute("visibility", "hidden");
    dot.setAttribute("visibility", "hidden");
    dotRing.setAttribute("visibility", "hidden");
  }

  function nearest(clientX) {
    const rect = overlay.getBoundingClientRect();
    const tx = t0 + ((clientX - rect.left - m.left) / iw) * (t1 - t0);
    let best = 0, bd = Infinity;
    for (let i = 0; i < pts.length; i++) {
      const d = Math.abs(pts[i].t - tx);
      if (d < bd) { bd = d; best = i; }
    }
    return best;
  }

  overlay.addEventListener("pointermove", e => showIndex(nearest(e.clientX)));
  overlay.addEventListener("pointerleave", hide);
  overlay.addEventListener("focus", () => showIndex(active >= 0 ? active : pts.length - 1));
  overlay.addEventListener("blur", hide);
  overlay.addEventListener("keydown", e => {
    if (e.key === "ArrowLeft" && active > 0) { showIndex(active - 1); e.preventDefault(); }
    else if (e.key === "ArrowRight" && active < pts.length - 1) { showIndex(active + 1); e.preventDefault(); }
    else if (e.key === "Escape") hide();
  });
}

/* Mini sparkline for the card: line + wash + end dot, no axes. */
function sparkline(container, pts, color) {
  container.textContent = "";
  if (pts.length < 2) return;
  const width = Math.max(200, container.clientWidth || 300);
  const height = 56;
  const pad = 6;
  const t0 = pts[0].t, t1 = pts[pts.length - 1].t;
  const vals = pts.filter(p => p.v != null).map(p => p.v);
  if (!vals.length) return;
  const vMax = Math.max(...vals) || 1;
  const x = t => pad + ((t - t0) / (t1 - t0 || 1)) * (width - 2 * pad - 34);
  const y = v => height - pad - (v / vMax) * (height - 2 * pad);

  const svg = svgEl("svg", { width, height, viewBox: `0 0 ${width} ${height}` });
  let line = "", prev = null, lastPt = null;
  for (const p of pts) {
    if (p.v == null) { prev = null; continue; }
    line += (prev == null ? " M " : " L ") + x(p.t).toFixed(1) + " " + y(p.v).toFixed(1);
    prev = p; lastPt = p;
  }
  if (line) svg.appendChild(svgEl("path", {
    d: line, fill: "none", stroke: color, "stroke-width": 2,
    "stroke-linejoin": "round", "stroke-linecap": "round",
  }));
  if (lastPt) {
    svg.appendChild(svgEl("circle", { cx: x(lastPt.t), cy: y(lastPt.v), r: 5, fill: "var(--surface-1)" }));
    svg.appendChild(svgEl("circle", { cx: x(lastPt.t), cy: y(lastPt.v), r: 3.5, fill: color }));
    const lab = svgEl("text", {
      x: x(lastPt.t) + 8, y: y(lastPt.v) + 4,
      fill: "var(--ink-2)", "font-size": 11,
    });
    lab.textContent = fmtNum(lastPt.v, 1);
    svg.appendChild(lab);
  }
  container.appendChild(svg);
}

/* ---------- rendering ---------- */

function stat(label, value, unit) {
  const s = el("div", "stat");
  s.appendChild(el("div", "stat-label", label));
  const v = el("div", "stat-value", value);
  if (unit && value !== "–") v.appendChild(el("span", "unit", unit));
  s.appendChild(v);
  return s;
}

function renderCards() {
  const grid = $("#cards");
  grid.textContent = "";
  const tpl = $("#card-tpl");

  for (const h of state.hosts) {
    const card = tpl.content.firstElementChild.cloneNode(true);
    card.dataset.host = h.name;
    const meta = STATUS[h.status] || STATUS.unknown;

    const chip = $(".status-chip", card);
    chip.classList.add("status-" + h.status);
    $(".status-icon", chip).appendChild(statusIcon(h.status));
    $(".status-label", chip).textContent = meta.label;
    $(".host-name", card).textContent = h.name;
    $(".host-addr", card).textContent = h.address + (h.kind === "tcp" ? " (tcp)" : "");

    const stats = $(".stats", card);
    stats.appendChild(stat("Uptime", h.uptime_pct == null ? "–" : fmtNum(h.uptime_pct, h.uptime_pct < 100 ? 2 : 0), "%"));
    const lastRtt = h.last && h.last.ok ? h.last.rtt_avg : null;
    stats.appendChild(stat("Latency", lastRtt == null ? "–" : fmtNum(lastRtt, lastRtt < 10 ? 2 : 1), "ms"));
    const loss = h.last ? h.last.loss_pct : null;
    stats.appendChild(stat("Loss", loss == null ? "–" : fmtNum(loss, 0), "%"));
    if (h.has_throughput) {
      const tp = h.last_throughput ? h.last_throughput.mbps : null;
      stats.appendChild(stat("Throughput", tp == null ? "–" : fmtNum(tp, tp < 10 ? 1 : 0), "Mbit/s"));
    }

    $(".card-head", card).addEventListener("click", () => toggleExpand(h.name));
    makeDraggable(card);
    grid.appendChild(card);

    const hist = state.histories[h.name];
    if (hist) {
      sparkline($(".spark", card), pingPoints(hist.ping), "var(--series-lat)");
      if (state.expanded === h.name) renderDetail(card, h, hist);
    }
  }
}

function pingPoints(rows) {
  return rows.map(r => ({
    t: r.ts,
    v: r.ok && r.rtt_avg != null ? r.rtt_avg : null,
    extra: r.ok
      ? [["packet loss", fmtPct(r.loss_pct)]]
      : (r.error ? [["", r.error]] : null),
  }));
}

function renderDetail(card, h, hist) {
  card.classList.add("expanded");
  $(".card-head", card).setAttribute("aria-expanded", "true");
  const detail = $(".detail", card);
  detail.hidden = false;

  const now = Date.now() / 1000;
  const domain = [now - state.hours * 3600, now];
  const rangeLabel = "last " + ($(".range-btn.selected") || {}).textContent;

  $(".lat-chart", detail).dataset.kind = "lat";
  $(".chart-block h2 .chart-sub", detail).textContent = "· ms, " + rangeLabel;
  timeSeriesChart($(".lat-chart", detail), {
    points: pingPoints(hist.ping),
    color: "var(--series-lat)",
    seriesName: "round-trip avg",
    fmtV: fmtMs,
    xDomain: domain,
    rangeHours: state.hours,
    height: 220,
  });

  const tpBlock = $(".tp-block", detail);
  if (h.has_throughput) {
    tpBlock.hidden = false;
    $(".chart-sub", tpBlock).textContent = "· Mbit/s, " + rangeLabel;
    timeSeriesChart($(".tp-chart", tpBlock), {
      points: hist.throughput.map(r => ({
        t: r.ts,
        v: r.ok ? r.mbps : null,
        extra: r.ok ? [["method", r.method]] : (r.error ? [["", r.error]] : null),
      })),
      color: "var(--series-tp)",
      seriesName: "throughput",
      fmtV: fmtMbps,
      xDomain: domain,
      rangeHours: state.hours,
      height: 180,
    });
  } else {
    tpBlock.hidden = true;
  }

  $(".remove-btn", detail).onclick = () => removeHost(h.name);

  const tbody = $("table.samples tbody", detail);
  tbody.textContent = "";
  const recent = hist.ping.slice(-20).reverse();
  for (const r of recent) {
    const tr = el("tr");
    tr.appendChild(el("td", null, fmtFull(r.ts)));
    const st = r.ok
      ? (r.loss_pct >= state.thresholds.loss_pct ||
         (r.rtt_avg != null && r.rtt_avg >= state.thresholds.latency_ms)
         ? "degraded" : "up")
      : "down";
    const tdS = el("td");
    const cell = el("span", "cell-status");
    cell.appendChild(statusIcon(st));
    cell.appendChild(document.createTextNode(STATUS[st].label));
    tdS.appendChild(cell);
    tr.appendChild(tdS);
    tr.appendChild(el("td", null, r.ok ? fmtMs(r.rtt_avg) : "–"));
    tr.appendChild(el("td", null, fmtPct(r.loss_pct)));
    tr.appendChild(el("td", null, r.error || ""));
    tbody.appendChild(tr);
  }
  if (!recent.length) {
    const tr = el("tr");
    const td = el("td", "empty-note", "No samples yet.");
    td.colSpan = 5;
    tr.appendChild(td);
    tbody.appendChild(tr);
  }
}

function toggleExpand(name) {
  state.expanded = state.expanded === name ? null : name;
  refresh();
}

/* ---------- drag to rearrange ---------- */

function makeDraggable(card) {
  card.draggable = true;
  card.addEventListener("dragstart", e => {
    state.dragging = card.dataset.host;
    card.classList.add("dragging");
    e.dataTransfer.effectAllowed = "move";
    e.dataTransfer.setData("text/plain", card.dataset.host);
  });
  card.addEventListener("dragend", async () => {
    card.classList.remove("dragging");
    state.dragging = null;
    const order = [...$("#cards").children].map(c => c.dataset.host);
    const current = state.hosts.map(h => h.name);
    if (order.join("\n") !== current.join("\n")) {
      state.hosts.sort((a, b) => order.indexOf(a.name) - order.indexOf(b.name));
      try {
        await fetchJSON("/api/hosts/order", {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ names: order }),
        });
      } catch (err) {
        alert("Could not save the new order: " + err.message);
        refresh();
      }
    }
  });
}

$("#cards").addEventListener("dragover", e => {
  if (!state.dragging) return;
  e.preventDefault();
  e.dataTransfer.dropEffect = "move";
  const grid = $("#cards");
  const dragged = grid.querySelector(".card.dragging");
  if (!dragged) return;
  const target = e.target.closest("section.card");
  if (!target || target === dragged) return;
  const rect = target.getBoundingClientRect();
  const before = (e.clientX - rect.left) / rect.width < 0.5;
  grid.insertBefore(dragged, before ? target : target.nextSibling);
});
$("#cards").addEventListener("drop", e => e.preventDefault());

/* ---------- data flow ---------- */

async function fetchJSON(url, options) {
  const r = await fetch(url, options);
  if (!r.ok) {
    let msg = url + " -> " + r.status;
    try {
      const body = await r.json();
      if (body.detail) msg = typeof body.detail === "string"
        ? body.detail : JSON.stringify(body.detail);
    } catch (e) { /* non-JSON error body */ }
    throw new Error(msg);
  }
  return r.json();
}

/* ---------- add / remove hosts ---------- */

const addDialog = $("#add-dialog");
const addForm = $("#add-form");
const addError = $("#add-error");

$("#add-host-btn").addEventListener("click", () => {
  addForm.reset();
  addError.hidden = true;
  $("#port-row").hidden = true;
  addDialog.showModal();
});
$("#add-cancel").addEventListener("click", () => addDialog.close());
addForm.elements.check.addEventListener("change", e => {
  $("#port-row").hidden = e.target.value !== "tcp";
});

addForm.addEventListener("submit", async e => {
  e.preventDefault();
  const f = addForm.elements;
  const body = {
    name: f.name.value.trim(),
    address: f.address.value.trim(),
    check: f.check.value,
    port: f.check.value === "tcp" && f.port.value ? parseInt(f.port.value, 10) : null,
    throughput_url: f.throughput_url.value.trim() || null,
  };
  try {
    await fetchJSON("/api/hosts", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    addDialog.close();
    refresh();
  } catch (err) {
    addError.textContent = err.message;
    addError.hidden = false;
  }
});

async function removeHost(name) {
  if (!confirm('Remove "' + name + '" and its history?')) return;
  try {
    await fetchJSON("/api/hosts/" + encodeURIComponent(name), { method: "DELETE" });
    if (state.expanded === name) state.expanded = null;
    delete state.histories[name];
    refresh();
  } catch (err) {
    alert("Could not remove host: " + err.message);
  }
}

async function refresh(quiet) {
  if (state.dragging || state.hoverHold) {
    scheduleRefresh();   // try again once the interaction ends
    return;
  }
  const grid = $("#cards");
  if (!quiet) grid.classList.add("refreshing");
  try {
    const status = await fetchJSON("/api/status?hours=" + state.hours);
    state.hosts = status.hosts;
    if (status.thresholds) state.thresholds = status.thresholds;
    // sparkline history for every host; full history drives the detail view too
    await Promise.all(state.hosts.map(async h => {
      try {
        state.histories[h.name] = await fetchJSON(
          "/api/history/" + encodeURIComponent(h.name) + "?hours=" + state.hours);
      } catch (e) { /* keep the previous history on a transient failure */ }
    }));
    renderCards();
    $("#updated").textContent = "updated " + fmtClock(status.now);
  } catch (e) {
    $("#updated").textContent = "update failed: " + e.message;
  } finally {
    grid.classList.remove("refreshing");
  }
}

function setRange(hours, btn) {
  state.hours = hours;
  for (const b of document.querySelectorAll(".range-btn")) b.classList.remove("selected");
  btn.classList.add("selected");
  refresh();
}

for (const btn of document.querySelectorAll(".range-btn")) {
  btn.addEventListener("click", () => setRange(parseFloat(btn.dataset.hours), btn));
}

let resizeTimer = null;
window.addEventListener("resize", () => {
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(renderCards, 150);
});

/* ---------- live updates (server-sent events) ---------- */

let refreshQueued = null;
function scheduleRefresh() {
  clearTimeout(refreshQueued);
  refreshQueued = setTimeout(() => refresh(true), LIVE_DEBOUNCE_MS);
}

function setLive(on, label) {
  $("#live").classList.toggle("on", on);
  $("#live-label").textContent = label;
}

function connectEvents() {
  const es = new EventSource("/api/events");
  es.onopen = () => setLive(true, "live");
  es.onerror = () => setLive(false, "reconnecting");   // EventSource retries itself
  es.onmessage = e => {
    let ev = {};
    try { ev = JSON.parse(e.data); } catch (err) { return; }
    if (ev.type === "hosts_changed") refresh(true);
    else scheduleRefresh();
  };
}

refresh();
connectEvents();
state.refreshTimer = setInterval(() => refresh(true), REFRESH_MS);
