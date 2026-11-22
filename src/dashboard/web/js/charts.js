// Charts in the notch's ink: white at stepped opacities, the current bucket in full white, no colour and no glow.

import { h, s, fmt } from "./core.js";

// Vertical pill bars. One series, one axis; the current bucket is lit, the rest are frosted white.
export function pillBars(data, opts = {}) {
  // Round the top up to a whole unit (an hour for minutes) so the three gridlines read cleanly.
  const unit = opts.unit || 1;
  const max = Math.max(unit, niceMax(Math.max(1, ...data.map((d) => d.value || 0)) / unit) * unit);
  const ticks = [0, 0.5, 1].map((f) => f * max);
  const format = opts.format || ((v) => fmt.num(v));
  return h("div", { class: "pillbars", style: { "--h": `${opts.height || 220}px` } },
    h("div", { class: "pillbars-grid" }, ticks.slice().reverse().map((t, i) => h("div", { class: "pillbars-line", key: String(i) },
      h("span", null, opts.tick ? opts.tick(t) : format(t))))),
    h("div", { class: "pillbars-plot" }, data.map((d, i) => h("div", {
      class: ["pillbar", d.current && "is-current"], key: d.key || String(i),
      "data-tip": d.tip || `${d.label}: ${format(d.value || 0)}`,
    }, h("div", { class: "pillbar-track" }, h("span", { class: "pillbar-fill", style: { "--v": `${((d.value || 0) / max) * 100}%` } })),
    h("span", { class: "pillbar-label" }, d.label)))));
}

function niceMax(v) {
  const step = Math.pow(10, Math.floor(Math.log10(v)));
  for (const m of [1, 1.2, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10]) if (m * step >= v) return m * step;
  return v;
}

// A quiet line for stat cards, ending in a dot at the latest value.
export function spark(values, opts = {}) {
  const clean = values.map((v) => v || 0);
  if (clean.length < 2) clean.push(clean[0] || 0, clean[0] || 0);
  const lo = Math.min(...clean), hi = Math.max(...clean), span = hi - lo || 1;
  const pts = clean.map((v, i) => [(i / (clean.length - 1)) * 100, 86 - ((v - lo) / span) * 72]);
  // Smooth with a simple Catmull-Rom to Bezier pass.
  let d = `M${pts[0][0]},${pts[0][1]}`;
  for (let i = 0; i < pts.length - 1; i++) {
    const p0 = pts[i - 1] || pts[i], p1 = pts[i], p2 = pts[i + 1], p3 = pts[i + 2] || p2;
    d += ` C${p1[0] + (p2[0] - p0[0]) / 6},${p1[1] + (p2[1] - p0[1]) / 6} ${p2[0] - (p3[0] - p1[0]) / 6},${p2[1] - (p3[1] - p1[1]) / 6} ${p2[0]},${p2[1]}`;
  }
  const [x, y] = pts[pts.length - 1];
  return h("div", { class: "spark", style: { height: `${opts.height || 28}px` } },
    s("svg", { viewBox: "0 0 100 100", preserveAspectRatio: "none" }, s("path", { d, class: "spark-line" })),
    h("span", { class: "spark-dot", style: { left: `${x}%`, top: `${y}%` } }));
}

export function ring(fraction, opts = {}) {
  const size = opts.size || 40, stroke = opts.stroke || 3;
  const r = (size - stroke) / 2, c = 2 * Math.PI * r;
  const f = Math.max(0, Math.min(1, fraction || 0));
  return h("div", { class: "ring", style: { width: `${size}px`, height: `${size}px` }, "data-tip": opts.tip },
    s("svg", { width: size, height: size, viewBox: `0 0 ${size} ${size}` },
      s("circle", { cx: size / 2, cy: size / 2, r, class: "ring-track", "stroke-width": stroke }),
      s("circle", { cx: size / 2, cy: size / 2, r, class: "ring-arc", "stroke-width": stroke,
        "stroke-dasharray": `${c} ${c}`, "stroke-dashoffset": c * (1 - f), transform: `rotate(-90 ${size / 2} ${size / 2})` })),
    opts.label != null && h("div", { class: "ring-label" }, opts.label));
}

export function heat(values, weeks = 12) {
  const end = new Date();
  const start = new Date(end); start.setDate(end.getDate() - ((weeks - 1) * 7 + ((end.getDay() + 6) % 7)));
  const max = Math.max(1, ...Object.values(values));
  const cells = [];
  for (const d = new Date(start); d <= end; d.setDate(d.getDate() + 1)) {
    const key = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
    const v = values[key] || 0;
    cells.push(h("span", { key, class: `heat heat-${v ? Math.min(4, Math.ceil((v / max) * 4)) : 0}`,
      "data-tip": `${fmt.date(key, { weekday: "short", month: "short", day: "numeric" })} · ${v ? fmt.minutes(v) : "nothing logged"}` }));
  }
  return h("div", { class: "heatmap" }, cells);
}
