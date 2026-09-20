"""Building the JSON payload the web UI reads.

The UI is static: it consumes one file produced from the same raw responses and
the same check engine the CLI uses, so the page can never disagree with
`python3 cli.py check`. Regenerate with `python3 cli.py build-ui`.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any, Dict, List, Mapping, Sequence

from eval import checks, invariants
from eval.metrics import mean, percentile
from jev.cases import Case

DEFAULT_UI_DATA = "docs/data.json"


def _jsonable(value: Any) -> Any:
    """Keep the payload free of anything json.dump would choke on."""
    if isinstance(value, float) and value != value:  # nan
        return None
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


def build(cases: Sequence[Case], records: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    wanted = {c.id for c in cases}
    records = [r for r in records if r["case"] in wanted]
    aliases = {(c.id, v.id): v.alias for c in cases for v in c.variants if v.alias}
    index = checks.Index(records, aliases)

    structural: List[Dict[str, Any]] = []
    for rec in records:
        structural.extend(invariants.check_record(rec))

    totals: Dict[str, int] = {}
    cost, tokens, lat, models = 0.0, 0, [], set()
    for r in records:
        if r.get("error") or not r.get("response"):
            continue
        usage = r["response"].get("usage") or {}
        cost += usage.get("cost") or 0.0
        tokens += (usage.get("input_tokens") or 0) + (usage.get("output_tokens") or 0)
        if r.get("latency_ms"):
            lat.append(r["latency_ms"])
        if r["response"].get("model"):
            models.add(r["response"]["model"])

    out_cases = []
    for case in cases:
        findings = checks.check_case(case, index)
        counts: Dict[str, int] = {}
        for f in findings:
            counts[f["status"]] = counts.get(f["status"], 0) + 1
            totals[f["status"]] = totals.get(f["status"], 0) + 1

        trials = []
        for (cid, variant, state, rep), entry in sorted(index.trials.items()):
            if cid != case.id:
                continue
            trials.append({
                "variant": variant, "state": state, "rep": rep,
                "latency_ms": entry["latency_ms"], "calls": entry["calls"],
                "input_tokens": entry["input_tokens"], "cost": entry["cost"],
                "answers": entry["answers"],
            })

        errors = [{"variant": v, "state": s, "error": msg}
                  for (cid, v, s), msg in index.errors.items() if cid == case.id]

        out_cases.append({
            "id": case.id, "title": case.title, "group": case.group,
            "note": case.note, "doc_ref": case.doc_ref,
            "repeat": case.repeat, "counts": counts,
            "variants": [{"id": v.id, "note": v.note, "questions": v.questions,
                          "alias": v.alias, "split": v.split,
                          "expect_error": v.expect_error}
                         for v in case.variants],
            "states": [{"id": s.id, "state": s.state, "note": s.note,
                        "tags": s.tags, "only_variants": s.only_variants,
                        "expect": s.expect, "expect_by_variant": s.expect_by_variant}
                       for s in case.states],
            "relations": case.relations,
            "findings": findings,
            "trials": trials,
            "errors": errors,
        })

    ok = [r for r in records if not r.get("error")]
    return _jsonable({
        "meta": {
            "generated": time.strftime("%Y-%m-%d %H:%M"),
            "models": sorted(models),
            "responses": len(ok),
            "cases": len(cases),
            "cost": cost,
            "tokens": tokens,
            "latency": {
                "p50": percentile(lat, 0.5) if lat else None,
                "p95": percentile(lat, 0.95) if lat else None,
                "mean": mean(lat) if lat else None,
            },
            "counts": totals,
            "structural_violations": len(structural),
        },
        "structural": structural,
        "cases": out_cases,
    })


def write(cases: Sequence[Case], records: Sequence[Mapping[str, Any]],
          out_path: str = DEFAULT_UI_DATA) -> str:
    payload = build(cases, records)
    directory = os.path.dirname(out_path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, separators=(",", ":"))
    return out_path
