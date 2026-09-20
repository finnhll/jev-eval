// Chart primitives. No dependencies, no canvas: every chart is SVG sized to its
// container in real pixels, so labels stay legible instead of being scaled by a
// viewBox. Each chart re-renders on resize and ships a hover layer.

const TIP = (() => {
  const el = document.createElement("div");
  el.className = "tip";
  document.body.appendChild(el);
  return {
    show(html, x, y) {
      el.innerHTML = html;
      el.classList.add("on");
      const r = el.getBoundingClientRect();
      const left = Math.min(Math.max(8, x - r.width / 2), innerWidth - r.width - 8);
      const top = y - r.height - 12 < 8 ? y + 18 : y - r.height - 12;
      el.style.left = left + "px";
      el.style.top = top + "px";
    },
    hide() { el.classList.remove("on"); },
  };
})();

const SVG_NS = "http://www.w3.org/2000/svg";
const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
export const SERIES = () => [css("--series-1"), css("--series-2"), css("--series-3")];

function el(name, attrs = {}, text) {
  const node = document.createElementNS(SVG_NS, name);
  for (const [k, v] of Object.entries(attrs)) {
    if (v !== null && v !== undefined) node.setAttribute(k, v);
  }
  if (text !== undefined) node.textContent = text;
  return node;
}

/** Trim a tick label to the pixels available to it, so labels never collide. */
function fitLabel(text, px) {
  const max = Math.max(3, Math.floor(px / 6.4));
  return text.length > max ? text.slice(0, max - 1) + "\u2026" : text;
}

function fmt(v, d = 2) {
  if (v === null || v === undefined || Number.isNaN(v)) return "-";
  return Number(v).toFixed(d);
}

// Re-render on container resize so text is never scaled by a viewBox.
function responsive(host, draw) {
  const run = () => {
    const width = host.clientWidth || 640;
    host.replaceChildren(draw(width));
  };
  run();
  if (typeof ResizeObserver !== "undefined") {
    let last = host.clientWidth;
    new ResizeObserver(() => {
      if (Math.abs(host.clientWidth - last) > 8) { last = host.clientWidth; run(); }
    }).observe(host);
  }
  window.addEventListener("themechange", run);
}

function axes(svg, { x0, x1, y0, y1, ticks, labels, yFmt }) {
  const line = css("--line");
  const muted = css("--text-muted");
  for (const t of ticks) {
    const y = t.y;
    svg.appendChild(el("line", { x1: x0, x2: x1, y1: y, y2: y, stroke: line, "stroke-width": 1 }));
    svg.appendChild(el("text", {
      x: x0 - 8, y: y + 4, "text-anchor": "end", fill: muted, "font-size": 11,
      "font-family": css("--mono"),
    }, yFmt ? yFmt(t.v) : t.v));
  }
  for (const l of labels || []) {
    svg.appendChild(el("text", {
      x: l.x, y: y1 + 18, "text-anchor": "middle", fill: muted, "font-size": 11,
    }, l.text));
  }
}

function niceTicks(min, max, count = 4) {
  if (min === max) { min -= 0.5; max += 0.5; }
  const span = max - min;
  const raw = span / count;
  const mag = Math.pow(10, Math.floor(Math.log10(raw)));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= raw) || mag * 10;
  const start = Math.floor(min / step) * step;
  const out = [];
  for (let v = start; v <= max + step / 2; v += step) out.push(Number(v.toFixed(6)));
  return out;
}

/** A value that should rise along an ordered sequence of states. */
export function ladder(host, { points, yLabel, max, series = 0, band }) {
  responsive(host, (width) => {
    const h = 240, padL = 46, padR = 16, padT = 14, padB = 44;
    const svg = el("svg", { width, height: h, role: "img" });
    const colors = SERIES();
    const color = colors[series % colors.length];
    const values = points.map((p) => p.v);
    const lo = Math.min(0, ...values);
    const hi = max !== undefined ? max : Math.max(...values);
    const tickVals = niceTicks(lo, hi, 4);
    const yMin = Math.min(lo, ...tickVals), yMax = Math.max(hi, ...tickVals);
    const X = (i) => padL + (points.length === 1 ? (width - padL - padR) / 2
      : (i * (width - padL - padR)) / (points.length - 1));
    const Y = (v) => h - padB - ((v - yMin) / (yMax - yMin || 1)) * (h - padT - padB);

    axes(svg, {
      x0: padL, x1: width - padR, y0: padT, y1: h - padB,
      ticks: tickVals.map((v) => ({ v, y: Y(v) })),
      yFmt: (v) => (Math.abs(v) < 10 ? fmt(v, 1) : String(v)),
    });

    if (band) {
      svg.appendChild(el("rect", {
        x: padL, y: Y(band[1]), width: width - padL - padR,
        height: Math.max(1, Y(band[0]) - Y(band[1])),
        fill: color, opacity: 0.08,
      }));
    }

    const d = points.map((p, i) => `${i ? "L" : "M"}${X(i)},${Y(p.v)}`).join(" ");
    svg.appendChild(el("path", { d, fill: "none", stroke: color, "stroke-width": 2,
      "stroke-linejoin": "round", "stroke-linecap": "round" }));

    points.forEach((p, i) => {
      const cx = X(i), cy = Y(p.v);
      // 2px surface ring keeps overlapping markers separable
      svg.appendChild(el("circle", { cx, cy, r: 6, fill: color,
        stroke: css("--surface-1"), "stroke-width": 2 }));
      svg.appendChild(el("text", {
        x: cx, y: cy - 13, "text-anchor": "middle", fill: css("--text-secondary"),
        "font-size": 11, "font-family": css("--mono"),
      }, fmt(p.v, 2)));
      const hit = el("circle", { cx, cy, r: 16, fill: "transparent" });
      hit.addEventListener("pointerenter", (e) => TIP.show(
        `<b>${p.label}</b><div class="row">${yLabel || "value"} ${fmt(p.v, 3)}` +
        (p.extra ? `</div><div class="row">${p.extra}` : "") + `</div>`,
        e.clientX, e.clientY));
      hit.addEventListener("pointerleave", () => TIP.hide());
      svg.appendChild(hit);
    });

    const every = Math.ceil(points.length / Math.max(2, Math.floor(width / 90)));
    points.forEach((p, i) => {
      if (i % every) return;
      svg.appendChild(el("text", {
        x: X(i), y: h - padB + 18, "text-anchor": "middle",
        fill: css("--text-muted"), "font-size": 10.5,
      }, p.label.length > 13 ? p.label.slice(0, 12) + "…" : p.label));
    });
    return svg;
  });
}

/** Grouped bars: one group per category, one bar per series. */
export function groupedBars(host, { groups, series, yFmt = (v) => fmt(v, 2), max, xLabel }) {
  responsive(host, (width) => {
    const h = 250, padL = 46, padR = 14, padT = 14, padB = 46;
    const svg = el("svg", { width, height: h, role: "img" });
    const colors = SERIES();
    const all = groups.flatMap((g) => g.values.map((v) => v ?? 0));
    const hi = max !== undefined ? max : Math.max(...all, 0);
    const tickVals = niceTicks(0, hi, 4);
    const yMax = Math.max(hi, ...tickVals);
    const Y = (v) => h - padB - (v / (yMax || 1)) * (h - padT - padB);
    const plot = width - padL - padR;
    const gw = plot / groups.length;
    const bw = Math.max(6, Math.min(34, (gw - 14) / series.length - 2));

    axes(svg, {
      x0: padL, x1: width - padR, y0: padT, y1: h - padB,
      ticks: tickVals.map((v) => ({ v, y: Y(v) })), yFmt,
    });

    groups.forEach((g, gi) => {
      const centre = padL + gw * gi + gw / 2;
      const span = series.length * bw + (series.length - 1) * 2; // 2px surface gap
      g.values.forEach((v, si) => {
        if (v === null || v === undefined) return;
        const x = centre - span / 2 + si * (bw + 2);
        const y = Y(v), bh = Math.max(1, h - padB - y);
        svg.appendChild(el("rect", {
          x, y, width: bw, height: bh, rx: 4, ry: 4, fill: colors[si % colors.length],
        }));
        if (series.length <= 3 && bw >= 16) {
          svg.appendChild(el("text", {
            x: x + bw / 2, y: y - 6, "text-anchor": "middle",
            fill: css("--text-secondary"), "font-size": 10.5, "font-family": css("--mono"),
          }, yFmt(v)));
        }
        const hit = el("rect", { x: x - 2, y: padT, width: bw + 4, height: h - padB - padT, fill: "transparent" });
        hit.addEventListener("pointerenter", (e) => TIP.show(
          `<b>${g.label}</b><div class="row">${series[si]} &middot; ${yFmt(v)}</div>`,
          e.clientX, e.clientY));
        hit.addEventListener("pointerleave", () => TIP.hide());
        svg.appendChild(hit);
      });
      svg.appendChild(el("text", {
        x: centre, y: h - padB + 18, "text-anchor": "middle",
        fill: css("--text-muted"), "font-size": 11,
      }, fitLabel(g.label, gw - 6)));
    });
    if (xLabel) {
      svg.appendChild(el("text", {
        x: padL + plot / 2, y: h - 6, "text-anchor": "middle",
        fill: css("--text-muted"), "font-size": 11,
      }, xLabel));
    }
    return svg;
  });
}

/** Reliability plot: mean predicted probability against observed rate. */
export function reliability(host, { bins }) {
  responsive(host, (width) => {
    const h = 280, pad = 48;
    const svg = el("svg", { width, height: h, role: "img" });
    const size = Math.min(width - pad - 16, h - pad - 26);
    const x0 = pad, y1 = pad + size;
    const X = (v) => x0 + v * size;
    const Y = (v) => y1 - v * size;
    const line = css("--line"), muted = css("--text-muted");

    for (const t of [0, 0.25, 0.5, 0.75, 1]) {
      svg.appendChild(el("line", { x1: x0, x2: x0 + size, y1: Y(t), y2: Y(t), stroke: line }));
      svg.appendChild(el("text", { x: x0 - 8, y: Y(t) + 4, "text-anchor": "end",
        fill: muted, "font-size": 11, "font-family": css("--mono") }, fmt(t, 2)));
      svg.appendChild(el("text", { x: X(t), y: y1 + 18, "text-anchor": "middle",
        fill: muted, "font-size": 11, "font-family": css("--mono") }, fmt(t, 2)));
    }
    // perfect calibration reference
    svg.appendChild(el("line", { x1: X(0), y1: Y(0), x2: X(1), y2: Y(1),
      stroke: css("--line-strong"), "stroke-width": 2, "stroke-dasharray": "5 5" }));
    // Parked below the diagonal on the right, clear of the marker band.
    svg.appendChild(el("text", { x: X(1), y: Y(0.55), "text-anchor": "end",
      fill: muted, "font-size": 11 }, "perfect calibration"));

    const colors = SERIES();
    bins.filter((b) => b.n).forEach((b) => {
      const cx = X(b.mean_prob), cy = Y(b.observed);
      const r = Math.max(6, Math.min(16, 5 + b.n * 1.2));
      svg.appendChild(el("circle", { cx, cy, r, fill: colors[0], opacity: 0.85,
        stroke: css("--surface-1"), "stroke-width": 2 }));
      svg.appendChild(el("text", { x: cx, y: cy - r - 7, "text-anchor": "middle",
        fill: css("--text-secondary"), "font-size": 10.5, "font-family": css("--mono") },
        `n=${b.n}`));
      const hit = el("circle", { cx, cy, r: r + 8, fill: "transparent" });
      hit.addEventListener("pointerenter", (e) => TIP.show(
        `<b>bin ${fmt(b.lo, 1)}&ndash;${fmt(b.hi, 1)}</b>` +
        `<div class="row">predicted ${fmt(b.mean_prob, 3)}</div>` +
        `<div class="row">observed  ${fmt(b.observed, 3)}</div>` +
        `<div class="row">gap       ${b.gap >= 0 ? "+" : ""}${fmt(b.gap, 3)} &middot; n=${b.n}</div>`,
        e.clientX, e.clientY));
      hit.addEventListener("pointerleave", () => TIP.hide());
      svg.appendChild(hit);
    });

    svg.appendChild(el("text", { x: x0 + size / 2, y: y1 + 38, "text-anchor": "middle",
      fill: muted, "font-size": 11.5 }, "mean predicted probability"));
    svg.appendChild(el("text", { x: 14, y: y1 - size / 2, "text-anchor": "middle",
      fill: muted, "font-size": 11.5, transform: `rotate(-90 14 ${y1 - size / 2})` },
      "observed rate"));
    return svg;
  });
}

/** Horizontal probability bar for a table cell. */
export function probBar(value, picked) {
  const wrap = document.createElement("div");
  wrap.className = "pbar" + (picked ? " is-pick" : "");
  const track = document.createElement("div");
  track.className = "track";
  const fill = document.createElement("div");
  fill.className = "fill";
  fill.style.width = Math.max(0, Math.min(1, value)) * 100 + "%";
  if (!picked) fill.style.opacity = "0.45";
  track.appendChild(fill);
  const v = document.createElement("span");
  v.className = "v";
  v.textContent = fmt(value, 2);
  wrap.append(track, v);
  return wrap;
}

export function legend(items) {
  const wrap = document.createElement("div");
  wrap.className = "legend";
  const colors = SERIES();
  items.forEach((label, i) => {
    const s = document.createElement("span");
    s.innerHTML = `<i style="background:${colors[i % colors.length]}"></i>${label}`;
    wrap.appendChild(s);
  });
  return wrap;
}
