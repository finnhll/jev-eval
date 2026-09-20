"""Rendering collected results and findings as a single self-contained HTML page."""

from __future__ import annotations

import html
import json
import os
import time
from typing import Any, Dict, List, Mapping, Sequence

from eval import checks, invariants
from eval.metrics import fmt, mean, percentile
from jev.cases import Case

CSS = """
:root {
  --bg: #ffffff; --fg: #1a1a1a; --muted: #666; --line: #e2e2e2; --panel: #fafafa;
  --pass: #1a7f37; --fail: #c4314b; --warn: #b7791f; --info: #57606a;
  --mono: ui-monospace, SFMono-Regular, "SF Mono", Menlo, monospace;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --bg: #14161a; --fg: #e8e8e8; --muted: #9aa0a6; --line: #2c3037; --panel: #1b1e24;
    --pass: #4ac26b; --fail: #ff6b81; --warn: #e3b341; --info: #8b949e;
  }
}
:root[data-theme="dark"] {
  --bg: #14161a; --fg: #e8e8e8; --muted: #9aa0a6; --line: #2c3037; --panel: #1b1e24;
  --pass: #4ac26b; --fail: #ff6b81; --warn: #e3b341; --info: #8b949e;
}
* { box-sizing: border-box; }
body { background: var(--bg); color: var(--fg); margin: 0; padding: 32px 16px 64px;
  font: 15px/1.6 -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif; }
.wrap { max-width: 980px; margin: 0 auto; }
h1 { font-size: 26px; margin: 0 0 4px; letter-spacing: -0.01em; }
h2 { font-size: 19px; margin: 40px 0 6px; padding-top: 20px; border-top: 1px solid var(--line); }
h2 .cid { font: 12px var(--mono); color: var(--muted); font-weight: normal; }
.sub { color: var(--muted); font-size: 13px; margin: 0 0 24px; }
.note { color: var(--muted); font-size: 13.5px; margin: 8px 0 14px; max-width: 76ch; }
.doc { font: 12px var(--mono); color: var(--muted); margin: 0 0 10px; }
.cards { display: flex; flex-wrap: wrap; gap: 10px; margin: 20px 0 8px; }
.card { flex: 1 1 130px; background: var(--panel); border: 1px solid var(--line);
  border-radius: 8px; padding: 12px 14px; }
.card .n { font-size: 22px; font-weight: 600; letter-spacing: -0.02em; }
.card .l { font-size: 12px; color: var(--muted); text-transform: uppercase; letter-spacing: .04em; }
table { border-collapse: collapse; width: 100%; margin: 10px 0 4px; font-size: 13.5px; }
th { text-align: left; font-weight: 600; color: var(--muted); font-size: 11.5px;
  text-transform: uppercase; letter-spacing: .04em; padding: 6px 8px; border-bottom: 1px solid var(--line); }
td { padding: 6px 8px; border-bottom: 1px solid var(--line); vertical-align: top; }
td.where, td.detail { font: 12.5px/1.5 var(--mono); }
td.where { color: var(--muted); white-space: nowrap; }
.tag { display: inline-block; min-width: 44px; text-align: center; font: 600 11px var(--mono);
  padding: 2px 6px; border-radius: 4px; letter-spacing: .04em; }
.pass { color: var(--pass); background: color-mix(in srgb, var(--pass) 12%, transparent); }
.fail { color: var(--fail); background: color-mix(in srgb, var(--fail) 14%, transparent); }
.warn { color: var(--warn); background: color-mix(in srgb, var(--warn) 14%, transparent); }
.info { color: var(--info); background: color-mix(in srgb, var(--info) 14%, transparent); }
.bar { height: 7px; border-radius: 4px; background: var(--line); overflow: hidden; min-width: 90px; }
.bar > i { display: block; height: 100%; background: var(--info); }
details { margin: 6px 0 0; }
summary { cursor: pointer; color: var(--muted); font-size: 13px; }
@media (max-width: 640px) { body { padding: 20px 16px 48px; } td.where { white-space: normal; } }
"""


def esc(x: Any) -> str:
    return html.escape(str(x))


def tag(status: str) -> str:
    return '<span class="tag %s">%s</span>' % (status, status.upper())


def _findings_table(findings: Sequence[Mapping[str, Any]]) -> str:
    rows = []
    for f in findings:
        where = "/".join(str(f[k]) for k in ("variant", "state", "question") if f.get(k))
        rows.append("<tr><td>%s</td><td class='where'>%s</td><td class='detail'>%s</td>"
                    "<td class='where'>%s</td></tr>"
                    % (tag(f["status"]), esc(where), esc(f["detail"]), esc(f["check"])))
    return ("<table><tr><th>result</th><th>where</th><th>detail</th><th>check</th></tr>"
            + "".join(rows) + "</table>")


def _calibration_block(findings: Sequence[Mapping[str, Any]]) -> str:
    bins = [f for f in findings if f["check"] == "calibration.bin"]
    if not bins:
        return ""
    rows = []
    for f in bins:
        rows.append("<tr><td class='detail'>%s</td></tr>" % esc(f["detail"]))
    return "<table><tr><th>reliability bins</th></tr>%s</table>" % "".join(rows)


def _usage(records: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    cost, tokens, lat, models = 0.0, 0, [], set()
    for r in records:
        if r.get("error") or not r.get("response"):
            continue
        u = r["response"].get("usage") or {}
        cost += u.get("cost") or 0.0
        tokens += (u.get("input_tokens") or 0) + (u.get("output_tokens") or 0)
        if r.get("latency_ms"):
            lat.append(r["latency_ms"])
        if r["response"].get("model"):
            models.add(r["response"]["model"])
    return {"cost": cost, "tokens": tokens, "models": sorted(models),
            "p50": percentile(lat, 0.5) if lat else float("nan"),
            "p95": percentile(lat, 0.95) if lat else float("nan"),
            "mean": mean(lat) if lat else float("nan")}


def write_report(cases: Sequence[Case], records: Sequence[Mapping[str, Any]],
                 out_path: str) -> str:
    wanted = {c.id for c in cases}
    records = [r for r in records if r["case"] in wanted]
    aliases = {(c.id, v.id): v.alias for c in cases for v in c.variants if v.alias}
    index = checks.Index(records, aliases)

    structural: List[Dict[str, Any]] = []
    for rec in records:
        structural.extend(invariants.check_record(rec))

    per_case = []
    totals: Dict[str, int] = {}
    for case in cases:
        findings = checks.check_case(case, index)
        for f in findings:
            totals[f["status"]] = totals.get(f["status"], 0) + 1
        per_case.append((case, findings))

    usage = _usage(records)
    ok = [r for r in records if not r.get("error")]

    parts = ["<!DOCTYPE html><html lang='en'><head><meta charset='utf-8'>",
             "<meta name='viewport' content='width=device-width, initial-scale=1'>",
             "<title>Jev Primitives</title><style>%s</style></head><body><div class='wrap'>" % CSS]

    parts.append("<h1>Jev primitives: Choice, Score, Noul</h1>")
    parts.append("<p class='sub'>%d responses across %d experiments &middot; %s &middot; "
                 "generated %s</p>"
                 % (len(ok), len(cases), esc(", ".join(usage["models"]) or "unknown model"),
                    time.strftime("%Y-%m-%d %H:%M")))

    cards = [
        ("structural violations", str(len(structural)), "fail" if structural else "pass"),
        ("passed", str(totals.get("pass", 0)), "pass"),
        ("failed", str(totals.get("fail", 0)), "fail"),
        ("hypothesis missed", str(totals.get("warn", 0)), "warn"),
        ("total cost", "$%.4f" % usage["cost"], "info"),
        ("latency p50", "%sms" % fmt(usage["p50"], 0), "info"),
    ]
    parts.append("<div class='cards'>")
    for label, value, cls in cards:
        parts.append("<div class='card'><div class='n %s' style='background:none'>%s</div>"
                     "<div class='l'>%s</div></div>" % (cls, esc(value), esc(label)))
    parts.append("</div>")
    parts.append("<p class='note'>A <b>failed</b> check is an expectation taken from the "
                 "documentation that did not reproduce. A <b>hypothesis missed</b> is an "
                 "expectation the case author predicted and the model contradicted, which is "
                 "a result rather than a defect. Structural violations mean a response "
                 "contradicted its own request or arithmetic, and would make every other "
                 "number here unsafe to read.</p>")

    parts.append("<h2>Structural invariants</h2>")
    if structural:
        parts.append(_findings_table(structural))
    else:
        parts.append("<p class='note'>%s All %d responses were internally consistent: "
                     "probabilities summed to one, every choice was the argmax of its own "
                     "distribution, every score matched the probability-weighted mean of its "
                     "level numbers, and answer ids matched the questions asked.</p>"
                     % (tag("pass"), len(ok)))

    for case, findings in per_case:
        if not findings:
            continue
        counts: Dict[str, int] = {}
        for f in findings:
            counts[f["status"]] = counts.get(f["status"], 0) + 1
        chips = " ".join("%s&nbsp;%d" % (tag(s), counts[s])
                         for s in ("fail", "warn", "pass", "info") if counts.get(s))
        parts.append("<h2>%s <span class='cid'>[%s]</span></h2>" % (esc(case.title), esc(case.id)))
        if case.doc_ref:
            parts.append("<p class='doc'>%s</p>" % esc(case.doc_ref))
        parts.append("<p>%s</p>" % chips)
        if case.note:
            parts.append("<p class='note'>%s</p>" % esc(case.note))
        ordered = sorted(findings, key=lambda f: ({"fail": 0, "warn": 1, "info": 2,
                                                   "pass": 3}.get(f["status"], 9), f["check"]))
        headline = [f for f in ordered if f["check"] != "calibration.bin"]
        parts.append(_findings_table(headline))
        parts.append(_calibration_block(findings))

    parts.append("<h2>Run detail</h2>")
    parts.append("<table><tr><th>metric</th><th>value</th></tr>"
                 "<tr><td>responses</td><td>%d</td></tr>"
                 "<tr><td>tokens</td><td>%s</td></tr>"
                 "<tr><td>cost</td><td>$%.4f</td></tr>"
                 "<tr><td>latency p50 / p95 / mean</td><td>%s / %s / %s ms</td></tr></table>"
                 % (len(ok), "{:,}".format(usage["tokens"]), usage["cost"],
                    fmt(usage["p50"], 0), fmt(usage["p95"], 0), fmt(usage["mean"], 0)))

    parts.append("</div></body></html>")

    directory = os.path.dirname(out_path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write("".join(parts))
    return out_path
