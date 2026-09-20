import { ladder, groupedBars, reliability, multiLine, probBar, legend } from "./charts.js";

const $ = (sel, root = document) => root.querySelector(sel);
const STATUS = ["fail", "warn", "pass", "info"];

function h(tag, attrs = {}, ...kids) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") node.className = v;
    else if (k === "html") node.innerHTML = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v);
  }
  for (const kid of kids.flat()) {
    if (kid === null || kid === undefined || kid === false) continue;
    node.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
  }
  return node;
}

/** A table that scrolls inside its own box rather than widening the page. */
function tbl(...rows) {
  const table = document.createElement("table");
  for (const row of rows.flat()) if (row) table.append(row);
  return h("div", { class: "tablewrap" }, table);
}

const fmt = (v, d = 2) =>
  v === null || v === undefined || Number.isNaN(v) ? "-" : Number(v).toFixed(d);
const tag = (status) => h("span", { class: "tag " + status }, status.toUpperCase());

const state = { data: null, live: false, route: "" };

/** value of an answer: noul or score, whichever the type carries */
function answerValue(a) {
  if (!a) return null;
  if (a.noul !== undefined && a.noul !== null) return a.noul;
  if (a.score !== undefined && a.score !== null) return a.score;
  return null;
}

function stateText(s) {
  return typeof s === "string" ? s : JSON.stringify(s);
}

// ---------------------------------------------------------------- overview

function viewOverview() {
  const m = state.data.meta;
  const c = m.counts || {};
  const main = h("div", {},
    h("h1", {}, "Jev primitives: Choice, Score, Noul"),
    h("p", { class: "lede" },
      `${m.responses} responses across ${m.cases} experiments · ` +
      `${(m.models || []).join(", ") || "unknown model"} · generated ${m.generated}`),
    h("div", { class: "tiles" },
      tile(m.structural_violations, "structural violations",
        m.structural_violations ? "critical" : "good",
        m.structural_violations ? "downstream numbers unsafe" : "every response self-consistent"),
      tile(c.pass || 0, "passed", "good", "documented expectations met"),
      tile(c.fail || 0, "failed", (c.fail ? "critical" : "good"), "documented expectation missed"),
      tile(c.warn || 0, "hypothesis missed", "warning", "author predicted, model disagreed"),
      tile("$" + fmt(m.cost, 4), "run cost", "muted", `${(m.tokens || 0).toLocaleString()} tokens`),
      tile(fmt(m.latency?.p50, 0) + "ms", "latency p50", "muted",
        `p95 ${fmt(m.latency?.p95, 0)}ms`)),
    h("p", { class: "note" },
      "A failed check is an expectation taken from the TypeSafe documentation that did not " +
      "reproduce. A hypothesis missed is an expectation this suite's author predicted and the " +
      "model contradicted — a result rather than a defect. Structural violations mean a response " +
      "contradicted its own request or arithmetic."));

  const notable = [];
  for (const cs of state.data.cases) {
    for (const f of cs.findings) {
      if (f.status === "fail" || f.status === "warn") notable.push({ ...f, case_title: cs.title });
    }
  }
  main.append(h("h2", {}, "Everything that did not go as expected"));
  if (!notable.length) {
    main.append(h("p", { class: "empty" }, "Nothing. Every expectation held."));
  } else {
    main.append(findingsTable(notable, true));
  }

  main.append(h("h2", {}, "By experiment"));
  const groups = {};
  for (const cs of state.data.cases) (groups[cs.group] ||= []).push(cs);
  for (const [group, list] of Object.entries(groups)) {
    main.append(h("h3", {}, group));
    const rows = list.map((cs) => h("tr", {},
      h("td", {}, h("a", { href: "#/case/" + cs.id, class: "mono" }, cs.id)),
      h("td", { class: "wrap" }, cs.title),
      h("td", { class: "num" }, cs.trials.length),
      h("td", {}, ...STATUS.filter((s) => cs.counts[s])
        .map((s) => h("span", { style: "margin-right:6px" }, tag(s), " ", String(cs.counts[s]))))));
    main.append(tbl(
      h("tr", {}, h("th", {}, "case"), h("th", {}, "title"),
        h("th", {}, "trials"), h("th", {}, "findings")), ...rows));
  }
  return main;
}

function tile(value, label, cls, sub) {
  return h("div", { class: "tile" },
    h("div", { class: "n " + (cls || "") }, String(value)),
    h("div", { class: "l" }, label),
    sub ? h("div", { class: "s" }, sub) : null);
}

function findingsTable(findings, withCase) {
  const rows = findings.map((f) => h("tr", {},
    h("td", {}, tag(f.status)),
    withCase ? h("td", {}, h("a", { href: "#/case/" + f.case, class: "mono" }, f.case)) : null,
    h("td", { class: "mono muted" },
      ["variant", "state", "question"].map((k) => f[k]).filter(Boolean).join("/")),
    h("td", { class: "wrap" }, f.detail),
    h("td", { class: "mono muted" }, f.check)));
  return tbl(
    h("tr", {}, h("th", {}, "result"), withCase ? h("th", {}, "case") : null,
      h("th", {}, "where"), h("th", {}, "detail"), h("th", {}, "check")),
    ...rows);
}

// ------------------------------------------------------------- case detail

function viewCase(id) {
  const cs = state.data.cases.find((c) => c.id === id);
  if (!cs) return h("p", { class: "empty" }, "No such case.");

  const main = h("div", {},
    h("h1", {}, cs.title),
    h("p", { class: "lede" }, h("span", { class: "mono" }, cs.id), " · ", cs.group,
      " · ", `${cs.trials.length} trials`),
    cs.doc_ref ? h("p", { class: "docref" }, cs.doc_ref) : null,
    cs.note ? h("p", { class: "note" }, cs.note) : null);

  if (state.live) {
    main.append(h("div", { class: "bar" },
      h("button", {
        class: "btn", onclick: (e) => rerunCase(cs.id, e.target),
      }, "Re-run this case against the API")));
  }

  for (const card of caseCharts(cs)) main.append(card);

  main.append(h("h2", {}, "Findings"));
  const filtered = cs.findings.filter((f) => f.check !== "calibration.bin");
  filtered.sort((a, b) => STATUS.indexOf(a.status) - STATUS.indexOf(b.status));
  main.append(findingsTable(filtered, false));

  main.append(h("h2", {}, "Answers"));
  for (const variant of cs.variants) {
    const trials = cs.trials.filter((t) => t.variant === variant.id && t.rep === 0);
    if (!trials.length && !cs.errors.some((e) => e.variant === variant.id)) continue;
    if (cs.variants.length > 1 || variant.id !== "default") {
      main.append(h("h3", {}, variant.id, variant.note ? " — " + variant.note : ""));
    }
    const err = cs.errors.find((e) => e.variant === variant.id);
    if (err) {
      main.append(h("div", { class: "banner" },
        h("b", {}, "Rejected by the API. "), err.error.slice(0, 400)));
    }
    if (trials.length) main.append(answersTable(cs, variant, trials));
  }

  main.append(h("h2", {}, "Questions as sent"));
  for (const variant of cs.variants) {
    main.append(h("details", { class: "raw" },
      h("summary", {}, variant.id),
      h("pre", {}, JSON.stringify(variant.questions, null, 2))));
  }
  return main;
}

function answersTable(cs, variant, trials) {
  const qids = Object.keys(variant.questions);
  const rows = [];
  for (const t of trials) {
    const st = cs.states.find((s) => s.id === t.state);
    for (const qid of qids) {
      const a = t.answers[qid];
      if (!a) continue;
      const value = answerValue(a);
      rows.push(h("tr", {},
        h("td", { class: "mono" }, t.state),
        h("td", { class: "mono muted" }, qid),
        h("td", { class: "mono" }, a.type),
        h("td", { class: "num" }, a.choice !== undefined && a.choice !== null
          ? a.choice : fmt(value, 3)),
        h("td", { class: "num" }, a.confidence !== undefined && a.confidence !== null
          ? fmt(a.confidence, 2) : "—"),
        h("td", {}, distributionCell(a))));
    }
  }
  const table = tbl(
    h("tr", {}, h("th", {}, "state"), h("th", {}, "question"), h("th", {}, "type"),
      h("th", {}, "answer"), h("th", {}, "confidence"), h("th", {}, "distribution")),
    ...rows);
  const texts = h("details", { class: "raw" }, h("summary", {}, "State text"),
    tbl( ...cs.states.map((s) => h("tr", {},
      h("td", { class: "mono" }, s.id),
      h("td", { class: "wrap" }, stateText(s.state))))));
  return h("div", {}, table, texts);
}

function distributionCell(a) {
  const probs = a.probabilities;
  if (!probs) {
    if (a.noul === undefined || a.noul === null) return h("span", { class: "muted" }, "—");
    // A Noul's single value is its whole distribution.
    return probBar(a.noul, a.noul >= 0.5);
  }
  const labels = a.legend || {};
  const entries = Object.entries(probs)
    .sort((x, y) => (a.type === "score" ? Number(x[0]) - Number(y[0]) : y[1] - x[1]))
    .slice(0, 5);
  return h("div", {}, ...entries.map(([k, v]) => {
    const label = a.type === "score" ? (labels[k] || k) : k;
    const short = String(label).length > 26 ? String(label).slice(0, 25) + "…" : String(label);
    return h("div", { style: "display:flex;gap:8px;align-items:center;margin:1px 0" },
      h("span", { class: "mono muted", style: "min-width:13ch;font-size:11.5px" }, short),
      probBar(v, k === a.choice || (a.type === "score" && v === Math.max(...Object.values(probs)))));
  }));
}

// --------------------------------------------------------------- charts per case

function card(title, caption, body, legendItems) {
  const node = h("div", { class: "card" }, h("h3", {}, title),
    caption ? h("p", { class: "cap" }, caption) : null);
  if (legendItems) node.append(legend(legendItems));
  const host = h("div", { class: "chart" });
  node.append(host);
  body(host);
  return node;
}

function caseCharts(cs) {
  const cards = [];
  for (const f of cs.findings) {
    if (f.check === "rank" && f.values) {
      const points = Object.entries(f.values).map(([label, v]) => ({ label, v }));
      cards.push(card(
        `Ordering: ${f.question}${f.variant && f.variant !== "default" ? " (" + f.variant + ")" : ""}`,
        `${f.detail}. The states were written in this order before any of them were sent.`,
        (host) => ladder(host, { points, yLabel: f.question })));
    }
    if (f.check === "threshold_sweep" && f.rows) {
      cards.push(thresholdCard(f));
    }
    if (f.check === "calibration") {
      const bins = cs.findings.find((x) => x.check === "calibration")?.bins;
      if (bins) {
        cards.push(card("Calibration", f.detail +
          ". Marker area is the number of states in the bin; the dashed line is perfect calibration.",
          (host) => reliability(host, { bins })));
      }
    }
  }

  const latency = cs.findings.filter((f) => f.check === "latency_profile");
  if (latency.length > 1) {
    cards.push(card("Latency against question count",
      "Serial requests, ten per configuration. p95 carries network noise; p50 is the claim under test.",
      (host) => groupedBars(host, {
        groups: latency.map((f) => ({ label: String(f.questions), values: [f.p50, f.p95] })),
        series: ["p50", "p95"], yFmt: (v) => fmt(v, 0) + "ms",
        xLabel: "questions per request",
      }), ["p50 (ms)", "p95 (ms)"]));
  }

  const batching = cs.findings.filter((f) => f.check === "batching_gain" && f.cost_ratio);
  if (batching.length) {
    cards.push(card("Batching advantage, observed against published",
      "The docs put thirteen questions in one call at 11.5x cheaper and 9.6x faster than " +
      "thirteen calls. Both arms here ran serially.",
      (host) => groupedBars(host, {
        groups: [
          ...batching.map((f) => ({ label: f.state + " cost", values: [f.cost_ratio, 11.5] })),
          ...batching.map((f) => ({ label: f.state + " speed", values: [f.speed_ratio, 9.6] })),
        ],
        series: ["observed", "documented"], yFmt: (v) => fmt(v, 1) + "x",
      }), ["observed", "documented claim"]));
  }

  const profiles = cs.findings.filter((f) => f.check === "variant_profile" &&
    f.mean_confidence !== null && f.mean_confidence !== undefined);
  if (profiles.length > 1) {
    cards.push(card("Mean confidence by variant",
      "Same states, same judgement, different ways of writing the question.",
      (host) => groupedBars(host, {
        groups: profiles.map((f) => ({ label: f.variant, values: [f.mean_confidence] })),
        series: ["mean confidence"], max: 1,
      })));
  }

  const agree = cs.findings.filter((f) =>
    (f.check === "variant_agreement" || f.check === "state_agreement") && f.agreement !== undefined);
  if (agree.length) {
    cards.push(card("Agreement with the baseline",
      "Share of answers that match the baseline variant, answer by answer.",
      (host) => groupedBars(host, {
        groups: agree.map((f) => ({ label: f.variant, values: [f.agreement] })),
        series: ["agreement"], max: 1,
      })));
  }

  // Option-order sensitivity: the distributions themselves, side by side.
  const distFindings = cs.findings.filter((f) => f.check === "dist_close");
  if (distFindings.length) {
    for (const f of distFindings.filter((x) => x.status === "fail")) {
      const trials = cs.trials.filter((t) => t.state === f.state);
      const options = new Set();
      trials.forEach((t) => Object.keys(t.answers[f.question]?.probabilities || {})
        .forEach((k) => options.add(k)));
      const optionList = [...options];
      cards.push(card(`Distribution drift: ${f.state}`,
        `${f.detail}. Each group is one option; each bar is one way of ordering the criteria map.`,
        (host) => groupedBars(host, {
          groups: optionList.map((opt) => ({
            label: opt,
            values: trials.map((t) => t.answers[f.question]?.probabilities?.[opt] ?? 0),
          })),
          series: trials.map((t) => t.variant), max: 1,
        }), trials.map((t) => t.variant)));
    }
  }
  return cards;
}

/** The sweep: a chart of what each cutoff decides, then the two tables. */
function thresholdCard(f) {
  const rows = f.rows;
  const node = h("div", { class: "card" },
    h("h3", {}, "Where to put the cutoff"),
    h("p", { class: "cap" },
      "A Noul returns a probability and leaves the yes/no to your code. Each row is one " +
      "candidate cutoff applied to the labelled states. " + f.detail));
  node.append(legend(["accuracy", "precision", "recall"]));
  const host = h("div", { class: "chart" });
  node.append(host);
  multiLine(host, {
    x: rows.map((r) => r.threshold),
    series: [
      { label: "accuracy", values: rows.map((r) => r.accuracy) },
      { label: "precision", values: rows.map((r) => r.precision) },
      { label: "recall", values: rows.map((r) => r.recall) },
    ],
    xLabel: "cutoff (predict yes when value ≥ cutoff)",
  });

  node.append(h("h3", {}, "Two-way cutoff"));
  node.append(tbl(
    h("tr", {}, h("th", {}, "cutoff"), h("th", {}, "accuracy"), h("th", {}, "precision"),
      h("th", {}, "recall"), h("th", {}, "false yes"), h("th", {}, "missed yes")),
    ...rows.map((r) => h("tr", {},
      h("td", { class: "num" }, fmt(r.threshold, 2)),
      h("td", { class: "num" }, fmt(r.accuracy, 2)),
      h("td", { class: "num" }, fmt(r.precision, 2)),
      h("td", { class: "num" }, fmt(r.recall, 2)),
      h("td", { class: "num" }, r.fp),
      h("td", { class: "num" }, r.fn)))));

  if (f.bands) {
    node.append(h("h3", {}, "Three-way band, with a review lane"));
    node.append(h("p", { class: "cap" },
      "Below the low edge is an automatic no, at or above the high edge an automatic yes, " +
      "and everything between goes to a person. The number worth optimising is accuracy on " +
      "the part you decide automatically, read next to how much you hand over."));
    node.append(tbl(
      h("tr", {}, h("th", {}, "band"), h("th", {}, "decided automatically"),
        h("th", {}, "accuracy when decided"), h("th", {}, "sent to review"),
        h("th", {}, "wrong decisions")),
      ...f.bands.map((b) => h("tr", {},
        h("td", { class: "num" }, `${fmt(b.lo, 2)} – ${fmt(b.hi, 2)}`),
        h("td", { class: "num" }, fmt(100 * b.coverage, 0) + "%"),
        h("td", { class: "num" }, fmt(b.auto_accuracy, 2)),
        h("td", { class: "num" }, b.review),
        h("td", { class: "num" }, b.errors)))));
  }

  if (f.values) {
    node.append(h("p", { class: "cap" },
      "Observed values, so a cutoff can be placed in a gap rather than on top of the data: " +
      f.values.map((v) => fmt(v, 2)).join("  ")));
  }
  return node;
}

// ------------------------------------------------------------------ analysis

function viewAnalysis() {
  const main = h("div", {},
    h("h1", {}, "Analysis"),
    h("p", { class: "lede" },
      "The charts that answer a question on their own, pulled out of their cases."));
  const picks = [
    ["n4_calibration", "Do the probabilities mean what they claim, and where should the cutoff go?"],
    ["c2_option_order", "Does the order of the options matter?"],
    ["m3_latency_scaling", "What does an extra question cost in time?"],
    ["m2_batching_gain", "What does batching actually buy?"],
    ["s5_level_count", "How many levels should a rubric have?"],
    ["l1_language_parity", "What does Chinese content cost?"],
    ["s1_doc_baseline", "Does the published Score table reproduce?"],
    ["x1_three_types", "One judgement, three primitives"],
  ];
  for (const [id, question] of picks) {
    const cs = state.data.cases.find((c) => c.id === id);
    if (!cs) continue;
    main.append(h("h2", {}, question));
    main.append(h("p", { class: "note" },
      h("a", { href: "#/case/" + cs.id, class: "mono" }, cs.id), " — ", cs.title));
    const cards = caseCharts(cs);
    if (cards.length) cards.forEach((c) => main.append(c));
    else main.append(h("p", { class: "empty" }, "No chart for this case; see its findings."));
  }
  return main;
}

// ----------------------------------------------------------------- raw data

function viewRaw() {
  const main = h("div", {},
    h("h1", {}, "Raw responses"),
    h("p", { class: "lede" },
      "Every stored answer, exactly as the API returned it."));
  const caseSel = h("select", {}, h("option", { value: "" }, "all cases"),
    ...state.data.cases.map((c) => h("option", { value: c.id }, c.id)));
  const search = h("input", { type: "search", placeholder: "filter by state, variant or question" });
  const out = h("div", {});
  const render = () => {
    const cid = caseSel.value, q = search.value.trim().toLowerCase();
    const rows = [];
    for (const cs of state.data.cases) {
      if (cid && cs.id !== cid) continue;
      for (const t of cs.trials) {
        for (const [qid, a] of Object.entries(t.answers)) {
          const hay = `${cs.id} ${t.variant} ${t.state} ${qid}`.toLowerCase();
          if (q && !hay.includes(q)) continue;
          rows.push({ cs, t, qid, a });
        }
      }
    }
    out.replaceChildren(
      h("p", { class: "note" }, `${rows.length} answers`),
      tbl(
        h("tr", {}, h("th", {}, "case"), h("th", {}, "variant"), h("th", {}, "state"),
          h("th", {}, "question"), h("th", {}, "answer"), h("th", {}, "conf"),
          h("th", {}, "latency"), h("th", {}, "json")),
        ...rows.slice(0, 400).map(({ cs, t, qid, a }) => h("tr", {},
          h("td", { class: "mono" }, h("a", { href: "#/case/" + cs.id }, cs.id)),
          h("td", { class: "mono muted" }, t.variant),
          h("td", { class: "mono" }, t.state + (t.rep ? `#${t.rep}` : "")),
          h("td", { class: "mono muted" }, qid),
          h("td", { class: "num" }, a.choice ?? fmt(answerValue(a), 3)),
          h("td", { class: "num" }, a.confidence !== undefined && a.confidence !== null
            ? fmt(a.confidence, 2) : "—"),
          h("td", { class: "num" }, fmt(t.latency_ms, 0)),
          h("td", {}, h("details", {}, h("summary", {}, "view"),
            h("pre", {}, JSON.stringify(a, null, 2))))))),
      rows.length > 400 ? h("p", { class: "note" }, "Showing the first 400. Narrow the filter.") : null);
  };
  caseSel.addEventListener("change", render);
  search.addEventListener("input", render);
  main.append(h("div", { class: "bar" }, caseSel, search), out);
  render();
  return main;
}

// ---------------------------------------------------------------- playground

const PLAYGROUND_DEFAULT = {
  state: "Help! My payouts have been failing for 3 days.",
  questions: {
    is_urgent: {
      type: "noul",
      instructions: "Does this message convey urgency?",
      criteria: { true: "Explicitly time-sensitive", false: "No urgency expressed" },
    },
    department: {
      type: "choice",
      instructions: "Which team should handle this?",
      criteria: {
        billing: "Payments, invoicing, refunds",
        technical: "Bugs, outages, integrations",
        sales: "Pricing, upgrades, new accounts",
      },
    },
    frustration: {
      type: "score",
      instructions: "How frustrated is the customer?",
      criteria: ["Calm", "Frustrated", "Very angry"],
    },
  },
};

function viewPlayground() {
  const main = h("div", {},
    h("h1", {}, "Playground"),
    h("p", { class: "lede" },
      "Ask Jev a question of your own. The key stays on the local server and never reaches this page."));

  if (!state.live) {
    return h("div", {},
      h("h1", {}, "Playground"),
      h("div", { class: "banner" },
        h("b", {}, "Needs the local server. "),
        "This page is served as a static file, so it has no way to reach the API without " +
        "putting your key in the browser. Run ",
        h("code", {}, "python3 cli.py serve"),
        " from the repo and open the address it prints."));
  }

  const stateBox = h("textarea", { rows: 4 }, PLAYGROUND_DEFAULT.state);
  const qBox = h("textarea", { rows: 18 }, JSON.stringify(PLAYGROUND_DEFAULT.questions, null, 2));
  const out = h("div", {});

  const loader = h("select", {}, h("option", { value: "" }, "load questions from a case…"),
    ...state.data.cases.flatMap((c) => c.variants.map((v) =>
      h("option", { value: `${c.id}::${v.id}` }, `${c.id} / ${v.id}`))));
  loader.addEventListener("change", () => {
    if (!loader.value) return;
    const [cid, vid] = loader.value.split("::");
    const cs = state.data.cases.find((c) => c.id === cid);
    const variant = cs.variants.find((v) => v.id === vid);
    qBox.value = JSON.stringify(variant.questions, null, 2);
    const first = cs.states.find((s) => !s.only_variants || s.only_variants.includes(vid));
    if (first) stateBox.value = typeof first.state === "string"
      ? first.state : JSON.stringify(first.state, null, 2);
  });

  const run = h("button", { class: "btn primary" }, "Run");
  run.addEventListener("click", async () => {
    let questions, payloadState;
    try {
      questions = JSON.parse(qBox.value);
    } catch (err) {
      out.replaceChildren(h("div", { class: "banner" }, h("b", {}, "Questions are not valid JSON. "),
        String(err.message)));
      return;
    }
    try {
      payloadState = JSON.parse(stateBox.value);
    } catch {
      payloadState = stateBox.value; // a plain string state is perfectly valid
    }
    run.disabled = true;
    run.textContent = "Running…";
    out.replaceChildren(h("p", { class: "empty" }, "Waiting for the API…"));
    try {
      const res = await fetch("api/decide", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ state: payloadState, questions }),
      });
      const body = await res.json();
      if (!res.ok) throw new Error(body.error || res.statusText);
      out.replaceChildren(playgroundResult(body, questions));
    } catch (err) {
      out.replaceChildren(h("div", { class: "banner" },
        h("b", {}, "Request failed. "), String(err.message)));
    } finally {
      run.disabled = false;
      run.textContent = "Run";
    }
  });

  main.append(
    h("div", { class: "bar" }, loader, run),
    h("h3", {}, "State"), stateBox,
    h("h3", {}, "Questions"), qBox,
    h("h2", {}, "Answer"), out);
  return main;
}

function playgroundResult(body, questions) {
  const answers = body.answers || {};
  const rows = Object.entries(answers).map(([qid, a]) => h("tr", {},
    h("td", { class: "mono" }, qid),
    h("td", { class: "mono" }, a.type),
    h("td", { class: "num" }, a.choice ?? fmt(answerValue(a), 3)),
    h("td", { class: "num" }, a.confidence !== undefined && a.confidence !== null
      ? fmt(a.confidence, 2) : "—"),
    h("td", {}, distributionCell(a))));
  const usage = body.usage || {};
  return h("div", {},
    tbl(
      h("tr", {}, h("th", {}, "question"), h("th", {}, "type"), h("th", {}, "answer"),
        h("th", {}, "confidence"), h("th", {}, "distribution")),
      ...rows),
    h("p", { class: "note" },
      `${body.model || ""} · ${usage.input_tokens || 0} in / ${usage.output_tokens || 0} out · ` +
      `$${fmt(usage.cost, 6)}`),
    h("details", { class: "raw" }, h("summary", {}, "Raw response"),
      h("pre", {}, JSON.stringify(body, null, 2))));
}

async function rerunCase(id, button) {
  button.disabled = true;
  const original = button.textContent;
  button.textContent = "Running…";
  try {
    const res = await fetch("api/collect", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ select: [id], force: true }),
    });
    const body = await res.json();
    if (!res.ok) throw new Error(body.error || res.statusText);
    await loadData();
    render();
  } catch (err) {
    button.textContent = "Failed: " + err.message;
    setTimeout(() => { button.textContent = original; button.disabled = false; }, 4000);
    return;
  }
  button.disabled = false;
  button.textContent = original;
}

// -------------------------------------------------------------------- shell

function buildNav() {
  const nav = $("#nav");
  const items = [
    ["#/overview", "Overview"],
    ["#/analysis", "Analysis"],
    ["#/raw", "Raw responses"],
    ["#/playground", "Playground" + (state.live ? "" : " (local only)")],
  ];
  const frag = document.createDocumentFragment();
  for (const [href, label] of items) {
    frag.append(h("button", {
      class: "nav", "data-route": href,
      onclick: () => { location.hash = href; closeSidebar(); },
    }, label));
  }
  const groups = {};
  for (const cs of state.data.cases) (groups[cs.group] ||= []).push(cs);
  for (const [group, list] of Object.entries(groups)) {
    frag.append(h("div", { class: "navgroup" }, group));
    for (const cs of list) {
      const worst = STATUS.find((s) => cs.counts[s]) || "info";
      frag.append(h("button", {
        class: "nav", "data-route": "#/case/" + cs.id,
        onclick: () => { location.hash = "#/case/" + cs.id; closeSidebar(); },
      }, h("span", { class: "tag " + worst, style: "min-width:0;padding:1px 5px" },
        worst === "fail" ? "!" : worst === "warn" ? "~" : worst === "pass" ? "✓" : "·"),
        cs.id.split("_")[0].toUpperCase(),
        h("span", { class: "cid" }, cs.id.split("_").slice(1).join(" ").slice(0, 14))));
    }
  }
  nav.replaceChildren(frag);
}

function closeSidebar() { $(".side").classList.remove("open"); $("#scrim")?.remove(); }

function render() {
  const hash = location.hash || "#/overview";
  state.route = hash;
  const main = $("#main");
  let view;
  if (hash.startsWith("#/case/")) view = viewCase(hash.slice(7));
  else if (hash === "#/analysis") view = viewAnalysis();
  else if (hash === "#/raw") view = viewRaw();
  else if (hash === "#/playground") view = viewPlayground();
  else view = viewOverview();
  main.replaceChildren(view);
  main.scrollTo?.(0, 0);
  window.scrollTo(0, 0);
  for (const b of document.querySelectorAll(".nav")) {
    b.setAttribute("aria-current", String(b.dataset.route === hash));
  }
}

async function loadData() {
  const res = await fetch("data.json", { cache: "no-store" });
  state.data = await res.json();
}

async function probeServer() {
  try {
    const res = await fetch("api/capabilities", { cache: "no-store" });
    if (!res.ok) return;
    state.live = Boolean((await res.json()).live);
  } catch { /* served as a static file; playground stays disabled */ }
}

function initTheme() {
  const saved = (() => { try { return localStorage.getItem("jev-theme"); } catch { return null; } })();
  if (saved) document.documentElement.setAttribute("data-theme", saved);
  $("#theme").addEventListener("click", () => {
    const now = document.documentElement.getAttribute("data-theme");
    const dark = now ? now === "dark"
      : matchMedia("(prefers-color-scheme: dark)").matches;
    const next = dark ? "light" : "dark";
    document.documentElement.setAttribute("data-theme", next);
    try { localStorage.setItem("jev-theme", next); } catch { /* private mode */ }
    window.dispatchEvent(new Event("themechange"));
  });
}

async function main() {
  initTheme();
  $("#menu").addEventListener("click", () => {
    const side = $(".side");
    side.classList.toggle("open");
    if (side.classList.contains("open")) {
      const scrim = h("div", { class: "scrim", id: "scrim", onclick: closeSidebar });
      document.body.append(scrim);
    } else closeSidebar();
  });
  await Promise.all([loadData(), probeServer()]);
  if (state.live) document.body.dataset.live = "true";
  buildNav();
  addEventListener("hashchange", render);
  render();
}

main().catch((err) => {
  document.querySelector("#main").replaceChildren(
    h("div", { class: "banner" }, h("b", {}, "Could not load data.json. "),
      String(err.message),
      " Run ", h("code", {}, "python3 cli.py build-ui"), " first."));
});
