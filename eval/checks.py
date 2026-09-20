"""Turning collected records into findings.

Two kinds of judgment live here:

  expectations  per state: this answer should land near this value
  relations     across states or variants: this ordering, gap or agreement
                should hold

Relations carry most of the weight. A single absolute threshold on one
probabilistic answer is brittle; an ordering over six states, or a confidence
gap between a clear and an ambiguous input, is not.
"""

from __future__ import annotations

import math
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from jev.cases import Case
from . import metrics as M

Finding = Dict[str, Any]
TrialKey = Tuple[str, str, str, int]  # case, variant, state, rep


# --------------------------------------------------------------------------
# indexing collected records
# --------------------------------------------------------------------------

class Index:
    """Collected records, merged per trial and addressable by coordinates."""

    def __init__(self, records: Sequence[Mapping[str, Any]],
                 aliases: Optional[Mapping[Tuple[str, str], Mapping[str, str]]] = None):
        self.trials: Dict[TrialKey, Dict[str, Any]] = {}
        self.aliases = dict(aliases or {})
        self.errors: Dict[Tuple[str, str, str], str] = {}
        for rec in records:
            if rec.get("error") or not rec.get("response"):
                if rec.get("error"):
                    self.errors[(rec["case"], rec["variant"], rec["state"])] = rec["error"]
                continue
            key = (rec["case"], rec["variant"], rec["state"], rec.get("rep", 0))
            entry = self.trials.setdefault(key, {
                "answers": {}, "latency_ms": 0.0, "calls": 0,
                "input_tokens": 0, "output_tokens": 0, "cost": 0.0,
                "model": rec["response"].get("model"),
            })
            # A split variant arrives as several records, one question each.
            answers = rec["response"].get("answers") or {}
            alias = self.aliases.get((rec["case"], rec["variant"]))
            if alias:
                answers = {qid: _apply_alias(a, alias) for qid, a in answers.items()}
            entry["answers"].update(answers)
            entry["latency_ms"] += rec.get("latency_ms") or 0.0
            entry["calls"] += 1
            usage = rec["response"].get("usage") or {}
            entry["input_tokens"] += usage.get("input_tokens") or 0
            entry["output_tokens"] += usage.get("output_tokens") or 0
            entry["cost"] += usage.get("cost") or 0.0

    def reps(self, case: str, variant: str, state: str) -> List[Dict[str, Any]]:
        out = [(k[3], v) for k, v in self.trials.items()
               if k[0] == case and k[1] == variant and k[2] == state]
        return [v for _rep, v in sorted(out, key=lambda kv: kv[0])]

    def answer(self, case: str, variant: str, state: str, question: str,
               rep: int = 0) -> Optional[Dict[str, Any]]:
        entry = self.trials.get((case, variant, state, rep))
        if not entry:
            return None
        return entry["answers"].get(question)

    def series(self, case: str, variant: str, state: str, question: str,
               field: str = "value") -> List[Any]:
        """One value per repeat, skipping trials that did not come back."""
        out = []
        for entry in self.reps(case, variant, state):
            answer = entry["answers"].get(question)
            if answer is None:
                continue
            value = field_of(answer, field)
            if value is not None:
                out.append(value)
        return out

    def mean_of(self, case: str, variant: str, state: str, question: str,
                field: str = "value") -> Optional[float]:
        values = [v for v in self.series(case, variant, state, question, field)
                  if isinstance(v, (int, float))]
        return M.mean(values) if values else None

    def trial_stats(self, case: str, variant: str, state: str) -> Dict[str, Any]:
        entries = self.reps(case, variant, state)
        if not entries:
            return {}
        return {
            "latency_ms": M.mean([e["latency_ms"] for e in entries]),
            "calls": entries[0]["calls"],
            "input_tokens": M.mean([e["input_tokens"] for e in entries]),
            "cost": M.mean([e["cost"] for e in entries]),
        }


def _apply_alias(answer: Any, alias: Mapping[str, str]) -> Any:
    """Rewrite option names to their canonical form so variants can be compared."""
    if not isinstance(answer, dict):
        return answer
    out = dict(answer)
    if isinstance(out.get("choice"), str):
        out["choice"] = alias.get(out["choice"], out["choice"])
    if isinstance(out.get("probabilities"), dict):
        out["probabilities"] = {alias.get(k, k): v for k, v in out["probabilities"].items()}
    return out


def field_of(answer: Mapping[str, Any], field: str) -> Any:
    """Read one field from an answer. 'value' means noul or score."""
    if field == "value":
        for key in ("noul", "score"):
            if answer.get(key) is not None:
                return answer[key]
        return None
    return answer.get(field)


def finding(check: str, status: str, case: str, detail: str, **extra: Any) -> Finding:
    out = {"check": check, "status": status, "case": case, "detail": detail}
    out.update(extra)
    return out


# --------------------------------------------------------------------------
# expectations
# --------------------------------------------------------------------------

def check_expectations(case: Case, index: Index) -> List[Finding]:
    out: List[Finding] = []
    for variant in case.variants:
        for state in case.states:
            if not state.runs_on(variant.id):
                continue
            for qid, spec in state.expectations_for(variant.id).items():
                answer = index.answer(case.id, variant.id, state.id, qid)
                if answer is None:
                    out.append(finding(
                        "expect", "fail", case.id,
                        "no answer collected", variant=variant.id,
                        state=state.id, question=qid))
                    continue
                out.extend(_check_one(case, variant.id, state.id, qid, spec, answer, index))
    return out


def _check_one(case: Case, variant: str, state: str, qid: str,
               spec: Mapping[str, Any], answer: Mapping[str, Any],
               index: Index) -> List[Finding]:
    out: List[Finding] = []
    where = {"variant": variant, "state": state, "question": qid}
    # Average across repeats so a stability experiment does not fail on noise.
    value = index.mean_of(case.id, variant, state, qid, "value")
    conf = index.mean_of(case.id, variant, state, qid, "confidence")
    picked = field_of(answer, "choice")

    # An expectation copied from the docs is a fact the run must reproduce.
    # An expectation the case author guessed is a hypothesis under test, and
    # its violation is a result worth reading rather than a failure.
    on_miss = "warn" if spec.get("hypothesis") else "fail"

    def add(ok: bool, name: str, detail: str, **nums: Any) -> None:
        out.append(finding(name, "pass" if ok else on_miss, case.id, detail,
                           **dict(where, **nums)))

    if "near" in spec:
        tol = float(spec.get("tol", 0.1))
        if value is None:
            add(False, "expect.near", "no numeric value in answer")
        else:
            delta = abs(value - float(spec["near"]))
            add(delta <= tol,
                "expect.near",
                "value %.3f vs expected %.3f (delta %.3f, tol %.2f)"
                % (value, spec["near"], delta, tol),
                value=value, expected=spec["near"], delta=delta)
    if "min" in spec and value is not None:
        add(value >= float(spec["min"]), "expect.min",
            "value %.3f, floor %.2f" % (value, spec["min"]), value=value)
    if "max" in spec and value is not None:
        add(value <= float(spec["max"]), "expect.max",
            "value %.3f, ceiling %.2f" % (value, spec["max"]), value=value)
    if "band" in spec and value is not None:
        lo, hi = spec["band"]
        add(lo <= value <= hi, "expect.band",
            "value %.3f, band [%.2f, %.2f]" % (value, lo, hi), value=value)
    if "equals" in spec:
        add(picked == spec["equals"], "expect.equals",
            "chose %r, expected %r" % (picked, spec["equals"]), choice=picked)
    if "one_of" in spec:
        add(picked in spec["one_of"], "expect.one_of",
            "chose %r, allowed %s" % (picked, spec["one_of"]), choice=picked)
    if "conf_min" in spec:
        if conf is None:
            add(False, "expect.conf_min", "answer carries no confidence")
        else:
            add(conf >= float(spec["conf_min"]), "expect.conf_min",
                "confidence %.3f, floor %.2f" % (conf, spec["conf_min"]), confidence=conf)
    if "conf_max" in spec:
        if conf is None:
            add(False, "expect.conf_max", "answer carries no confidence")
        else:
            add(conf <= float(spec["conf_max"]), "expect.conf_max",
                "confidence %.3f, ceiling %.2f" % (conf, spec["conf_max"]), confidence=conf)
    return out


# --------------------------------------------------------------------------
# relations
# --------------------------------------------------------------------------

HANDLERS: Dict[str, Callable[[Case, Index, Mapping[str, Any]], List[Finding]]] = {}


def handler(name: str):
    def register(fn):
        HANDLERS[name] = fn
        return fn
    return register


def _variants(case: Case, rel: Mapping[str, Any]) -> List[str]:
    return list(rel.get("variants") or [v.id for v in case.variants])


def _states(case: Case, rel: Mapping[str, Any]) -> List[str]:
    return list(rel.get("states") or [s.id for s in case.states])


def _default_variant(case: Case, rel: Mapping[str, Any]) -> str:
    return rel.get("variant") or case.variants[0].id


@handler("rank")
def _rank(case: Case, index: Index, rel: Mapping[str, Any]) -> List[Finding]:
    """Values should not decrease along the given order of states."""
    qid = rel["question"]
    variant = _default_variant(case, rel)
    order = rel["order"]
    tol = float(rel.get("tolerance", 0.0))
    field = rel.get("metric", "value")
    values = [index.mean_of(case.id, variant, s, qid, field) for s in order]
    if any(v is None for v in values):
        return [finding("rank", "fail", case.id,
                        "missing values for %s" % qid, variant=variant, question=qid)]
    bad = M.inversions(values, tol)
    rho = M.spearman(list(range(len(values))), values)
    detail = "%s along %d states: rho=%s, %d inversion(s)" % (
        field, len(order), M.fmt(rho), len(bad))
    if bad:
        detail += " -> " + ", ".join("%s(%.2f) > %s(%.2f)"
                                     % (order[i], values[i], order[j], values[j])
                                     for i, j in bad)
    return [finding("rank", "pass" if not bad else "fail", case.id, detail,
                    variant=variant, question=qid, spearman=rho,
                    values=dict(zip(order, values)))]


@handler("sum_to")
def _sum_to(case: Case, index: Index, rel: Mapping[str, Any]) -> List[Finding]:
    """Two questions whose values should add up, e.g. a question and its negation."""
    a, b = rel["questions"]
    target = float(rel.get("value", 1.0))
    tol = float(rel.get("tol", 0.15))
    variant = _default_variant(case, rel)
    out = []
    for state in _states(case, rel):
        va = index.mean_of(case.id, variant, state, a)
        vb = index.mean_of(case.id, variant, state, b)
        if va is None or vb is None:
            out.append(finding("sum_to", "fail", case.id,
                               "missing values", variant=variant, state=state))
            continue
        total = va + vb
        ok = abs(total - target) <= tol
        out.append(finding("sum_to", "pass" if ok else "fail", case.id,
                           "%s=%.3f + %s=%.3f = %.3f (target %.2f, tol %.2f)"
                           % (a, va, b, vb, total, target, tol),
                           variant=variant, state=state, total=total))
    return out


@handler("mean_gap")
def _mean_gap(case: Case, index: Index, rel: Mapping[str, Any]) -> List[Finding]:
    """A metric averaged over one group of states should exceed another group."""
    qid = rel["question"]
    field = rel.get("metric", "confidence")
    variant = _default_variant(case, rel)
    min_gap = float(rel.get("min_gap", 0.0))
    high = [index.mean_of(case.id, variant, s, qid, field) for s in rel["high"]]
    low = [index.mean_of(case.id, variant, s, qid, field) for s in rel["low"]]
    high = [v for v in high if v is not None]
    low = [v for v in low if v is not None]
    if not high or not low:
        return [finding("mean_gap", "fail", case.id, "missing values",
                        variant=variant, question=qid)]
    gap = M.mean(high) - M.mean(low)
    ok = gap >= min_gap
    return [finding("mean_gap", "pass" if ok else "fail", case.id,
                    "%s: high group %.3f - low group %.3f = %.3f (need >= %.2f)"
                    % (field, M.mean(high), M.mean(low), gap, min_gap),
                    variant=variant, question=qid, gap=gap)]


@handler("dist_close")
def _dist_close(case: Case, index: Index, rel: Mapping[str, Any]) -> List[Finding]:
    """The same question across variants should give the same distribution."""
    qid = rel["question"]
    max_tv = float(rel.get("max_tv", 0.05))
    variants = _variants(case, rel)
    out = []
    for state in _states(case, rel):
        dists = {}
        for v in variants:
            answer = index.answer(case.id, v, state, qid)
            if answer and isinstance(answer.get("probabilities"), dict):
                dists[v] = answer["probabilities"]
        if len(dists) < 2:
            out.append(finding("dist_close", "fail", case.id,
                               "fewer than two distributions collected",
                               state=state, question=qid))
            continue
        worst, pair = 0.0, ("", "")
        names = list(dists)
        for i in range(len(names)):
            for j in range(i + 1, len(names)):
                d = M.tv_distance(dists[names[i]], dists[names[j]])
                if d > worst:
                    worst, pair = d, (names[i], names[j])
        ok = worst <= max_tv
        out.append(finding("dist_close", "pass" if ok else "fail", case.id,
                           "max TV distance %.3f between %s and %s (limit %.2f)"
                           % (worst, pair[0], pair[1], max_tv),
                           state=state, question=qid, tv=worst))
    return out


@handler("answers_match")
def _answers_match(case: Case, index: Index, rel: Mapping[str, Any]) -> List[Finding]:
    """The selected answer should be identical across variants."""
    qid = rel["question"]
    field = rel.get("field", "choice")
    tol = float(rel.get("tol", 0.05))
    variants = _variants(case, rel)
    out = []
    for state in _states(case, rel):
        seen = {}
        for v in variants:
            answer = index.answer(case.id, v, state, qid)
            if answer:
                seen[v] = field_of(answer, field)
        values = [v for v in seen.values() if v is not None]
        if len(values) < 2:
            out.append(finding("answers_match", "fail", case.id,
                               "fewer than two answers collected",
                               state=state, question=qid))
            continue
        if all(isinstance(v, (int, float)) for v in values):
            spread = max(values) - min(values)
            ok = spread <= tol
            detail = "%s spread %.3f across variants (tol %.2f)" % (field, spread, tol)
        else:
            ok = len(set(values)) == 1
            detail = "%s = %s" % (field, seen)
        out.append(finding("answers_match", "pass" if ok else "fail", case.id,
                           detail, state=state, question=qid))
    return out


@handler("variant_gap")
def _variant_gap(case: Case, index: Index, rel: Mapping[str, Any]) -> List[Finding]:
    """One variant's metric should sit below (or above) another's, per state."""
    qid = rel["question"]
    field = rel.get("metric", "confidence")
    direction = rel.get("direction", "less")
    min_gap = float(rel.get("min_gap", 0.0))
    lo_variant, hi_variant = rel["variant"], rel["than_variant"]
    out = []
    for state in _states(case, rel):
        a = index.mean_of(case.id, lo_variant, state, qid, field)
        b = index.mean_of(case.id, hi_variant, state, qid, field)
        if a is None or b is None:
            out.append(finding("variant_gap", "fail", case.id, "missing values",
                               state=state, question=qid))
            continue
        gap = (b - a) if direction == "less" else (a - b)
        ok = gap >= min_gap
        out.append(finding("variant_gap", "pass" if ok else "fail", case.id,
                           "%s: %s=%.3f vs %s=%.3f, gap %.3f (need >= %.2f)"
                           % (field, lo_variant, a, hi_variant, b, gap, min_gap),
                           state=state, question=qid, gap=gap))
    return out


@handler("stability")
def _stability(case: Case, index: Index, rel: Mapping[str, Any]) -> List[Finding]:
    """Repeated identical requests should return the same answer."""
    max_std = float(rel.get("max_std", 0.05))
    max_flip = float(rel.get("max_flip_rate", 0.0))
    out = []
    for variant in _variants(case, rel):
        for state in _states(case, rel):
            questions = rel.get("questions") or list(case.variant(variant).questions)
            for qid in questions:
                values = [v for v in index.series(case.id, variant, state, qid, "value")
                          if isinstance(v, (int, float))]
                choices = [c for c in index.series(case.id, variant, state, qid, "choice")
                           if c is not None]
                if len(values) < 2 and len(choices) < 2:
                    continue
                if values:
                    sd = M.stdev(values)
                    out.append(finding(
                        "stability.value", "pass" if sd <= max_std else "fail", case.id,
                        "n=%d, sd=%.4f, range [%.2f, %.2f] (limit %.3f)"
                        % (len(values), sd, min(values), max(values), max_std),
                        variant=variant, state=state, question=qid, sd=sd))
                if choices:
                    modal = max(set(choices), key=choices.count)
                    flip = 1.0 - choices.count(modal) / len(choices)
                    out.append(finding(
                        "stability.choice", "pass" if flip <= max_flip else "fail", case.id,
                        "n=%d, flip rate %.2f, modal %r (limit %.2f)"
                        % (len(choices), flip, modal, max_flip),
                        variant=variant, state=state, question=qid, flip_rate=flip))
    return out


@handler("calibration")
def _calibration(case: Case, index: Index, rel: Mapping[str, Any]) -> List[Finding]:
    """Do the probabilities mean what they claim, on states with known truth?"""
    qid = rel["question"]
    variant = _default_variant(case, rel)
    labels: Dict[str, bool] = rel["labels"]
    bins = int(rel.get("bins", 5))
    pairs: List[Tuple[float, bool]] = []
    for state, truth in labels.items():
        value = index.mean_of(case.id, variant, state, qid)
        if value is not None:
            pairs.append((value, bool(truth)))
    if len(pairs) < 4:
        return [finding("calibration", "fail", case.id,
                        "only %d labelled answers" % len(pairs), question=qid)]

    brier = M.brier(pairs)
    err = M.ece(pairs, bins)
    acc = M.mean([1.0 if (p >= 0.5) == y else 0.0 for p, y in pairs])
    lo, hi = M.bootstrap_ci(pairs, M.brier)
    acc_lo, acc_hi = M.bootstrap_ci(
        [(p, y) for p, y in pairs],
        lambda s: M.mean([1.0 if (p >= 0.5) == y else 0.0 for p, y in s]))

    status = "info"
    if "max_ece" in rel:
        status = "pass" if err <= float(rel["max_ece"]) else "fail"
    detail = ("n=%d  brier=%.3f [%.3f, %.3f]  ece=%.3f  acc@0.5=%.2f [%.2f, %.2f]"
              % (len(pairs), brier, lo, hi, err, acc, acc_lo, acc_hi))
    out = [finding("calibration", status, case.id, detail, question=qid,
                   brier=brier, ece=err, accuracy=acc, n=len(pairs),
                   bins=M.reliability_bins(pairs, bins))]
    for b in M.reliability_bins(pairs, bins):
        if b["n"]:
            out.append(finding(
                "calibration.bin", "info", case.id,
                "[%.1f, %.1f)  n=%d  mean p=%.3f  observed=%.3f  gap=%+.3f"
                % (b["lo"], b["hi"], b["n"], b["mean_prob"], b["observed"], b["gap"]),
                question=qid))
    return out


@handler("threshold_sweep")
def _threshold_sweep(case: Case, index: Index, rel: Mapping[str, Any]) -> List[Finding]:
    """What each cutoff would actually decide, on states whose truth is known.

    A Noul hands back a probability and leaves the cutoff to the caller. This
    turns that choice into a table: for every candidate threshold, what the
    code would have decided and how often it would have been right, and for
    every candidate review band, how much traffic is decided automatically and
    how accurate that automatic part is.
    """
    qid = rel["question"]
    variant = _default_variant(case, rel)
    labels: Dict[str, bool] = rel["labels"]
    thresholds = rel.get("thresholds") or [0.5, 0.55, 0.6, 0.65, 0.7, 0.75, 0.8, 0.85, 0.9, 0.95]
    bands = rel.get("bands") or [[0.5, 0.5], [0.3, 0.7], [0.2, 0.8], [0.1, 0.9], [0.05, 0.95]]

    pairs: List[Tuple[float, bool]] = []
    for state, truth in labels.items():
        value = index.mean_of(case.id, variant, state, qid)
        if value is not None:
            pairs.append((value, bool(truth)))
    if len(pairs) < 4:
        return [finding("threshold_sweep", "fail", case.id,
                        "only %d labelled answers" % len(pairs), question=qid)]

    rows = [M.confusion(pairs, t) for t in thresholds]
    best = max(rows, key=lambda r: (r["accuracy"], r["f1"] if r["f1"] == r["f1"] else 0))

    # A cutoff sitting on top of observed values flips on noise, so say where
    # the nearest recorded value is to the one that looks best.
    values = sorted(v for v, _ in pairs)
    nearest = min(values, key=lambda v: abs(v - best["threshold"]))
    margin = abs(nearest - best["threshold"])

    out = [finding(
        "threshold_sweep", "info", case.id,
        "n=%d  best cutoff %.2f at accuracy %.2f (%d wrong)  nearest observed value %.2f, %.2f away"
        % (len(pairs), best["threshold"], best["accuracy"], best["fp"] + best["fn"],
           nearest, margin),
        question=qid, variant=variant, best_threshold=best["threshold"],
        rows=rows, bands=[M.band_split(pairs, lo, hi) for lo, hi in bands],
        values=values)]

    for r in rows:
        out.append(finding(
            "threshold_sweep.row", "info", case.id,
            ">= %.2f   acc %.2f   precision %s   recall %s   %d false yes / %d missed yes"
            % (r["threshold"], r["accuracy"], M.fmt(r["precision"], 2),
               M.fmt(r["recall"], 2), r["fp"], r["fn"]),
            question=qid))
    for b in [M.band_split(pairs, lo, hi) for lo, hi in bands]:
        out.append(finding(
            "threshold_sweep.band", "info", case.id,
            "review %.2f-%.2f   decides %.0f%% automatically at %.2f accuracy   "
            "%d to a person   %d wrong decisions"
            % (b["lo"], b["hi"], 100 * b["coverage"], b["auto_accuracy"],
               b["review"], b["errors"]),
            question=qid))
    return out


@handler("confidence_auroc")
def _confidence_auroc(case: Case, index: Index, rel: Mapping[str, Any]) -> List[Finding]:
    """Does confidence separate answers that are right from ones that are wrong?"""
    qid = rel["question"]
    variant = _default_variant(case, rel)
    truth: Dict[str, str] = rel["correct"]
    scores, labels = [], []
    for state, expected in truth.items():
        answer = index.answer(case.id, variant, state, qid)
        conf = index.mean_of(case.id, variant, state, qid, "confidence")
        if answer is None or conf is None:
            continue
        scores.append(conf)
        labels.append(field_of(answer, "choice") == expected)
    if len(set(labels)) < 2:
        return [finding("confidence_auroc", "info", case.id,
                        "every answer was %s; AUROC undefined"
                        % ("correct" if all(labels) else "incorrect"),
                        question=qid, n=len(labels))]
    value = M.auroc(scores, labels)
    status = "info"
    if "min_auroc" in rel:
        status = "pass" if value >= float(rel["min_auroc"]) else "fail"
    return [finding("confidence_auroc", status, case.id,
                    "AUROC=%.3f over n=%d (%d correct)"
                    % (value, len(labels), sum(labels)),
                    question=qid, auroc=value)]


@handler("variant_profile")
def _variant_profile(case: Case, index: Index, rel: Mapping[str, Any]) -> List[Finding]:
    """Side-by-side numbers per variant. Reporting, not pass/fail."""
    qid = rel.get("question")
    out = []
    for variant in _variants(case, rel):
        confs, values, toks, lats = [], [], [], []
        for state in _states(case, rel):
            if qid:
                c = index.mean_of(case.id, variant, state, qid, "confidence")
                v = index.mean_of(case.id, variant, state, qid, "value")
                if c is not None:
                    confs.append(c)
                if v is not None:
                    values.append(v)
            stats = index.trial_stats(case.id, variant, state)
            if stats:
                toks.append(stats["input_tokens"])
                lats.append(stats["latency_ms"])
        out.append(finding(
            "variant_profile", "info", case.id,
            "%-14s conf=%s  value=%s  in_tokens=%s  latency_p50=%sms"
            % (variant, M.fmt(M.mean(confs) if confs else None),
               M.fmt(M.mean(values) if values else None),
               M.fmt(M.mean(toks) if toks else None, 0),
               M.fmt(M.percentile(lats, 0.5) if lats else None, 0)),
            variant=variant, question=qid,
            mean_confidence=M.mean(confs) if confs else None))
    return out


@handler("variant_agreement")
def _variant_agreement(case: Case, index: Index, rel: Mapping[str, Any]) -> List[Finding]:
    """How closely each variant tracks a baseline variant, answer by answer."""
    baseline = rel["baseline"]
    questions = rel.get("questions")
    out = []
    for variant in _variants(case, rel):
        if variant == baseline:
            continue
        same, total, deltas, conf_deltas = 0, 0, [], []
        for state in _states(case, rel):
            qids = questions or list(case.variant(variant).questions)
            for qid in qids:
                base = index.answer(case.id, baseline, state, qid)
                other = index.answer(case.id, variant, state, qid)
                if not base or not other:
                    continue
                total += 1
                bc, oc = field_of(base, "choice"), field_of(other, "choice")
                bv, ov = field_of(base, "value"), field_of(other, "value")
                if bc is not None and oc is not None:
                    same += 1 if bc == oc else 0
                elif isinstance(bv, (int, float)) and isinstance(ov, (int, float)):
                    same += 1 if abs(bv - ov) <= float(rel.get("tol", 0.15)) else 0
                if isinstance(bv, (int, float)) and isinstance(ov, (int, float)):
                    deltas.append(abs(bv - ov))
                b_conf = field_of(base, "confidence")
                o_conf = field_of(other, "confidence")
                if isinstance(b_conf, (int, float)) and isinstance(o_conf, (int, float)):
                    conf_deltas.append(o_conf - b_conf)
        if not total:
            continue
        rate = same / total
        status = "info"
        if "min_agreement" in rel:
            status = "pass" if rate >= float(rel["min_agreement"]) else "fail"
        out.append(finding(
            "variant_agreement", status, case.id,
            "%-18s agreement %.2f (%d/%d)  mean |delta value| %s  mean delta conf %s"
            % (variant, rate, same, total,
               M.fmt(M.mean(deltas) if deltas else None),
               M.fmt(M.mean(conf_deltas) if conf_deltas else None)),
            variant=variant, agreement=rate))
    return out


@handler("expect_error")
def _expect_error(case: Case, index: Index, rel: Mapping[str, Any]) -> List[Finding]:
    """Assert the API rejects a shape, and show what it said."""
    out = []
    contains = rel.get("contains")
    for variant in _variants(case, rel):
        for state in _states(case, rel):
            message = index.errors.get((case.id, variant, state))
            if message is None:
                out.append(finding("expect_error", "fail", case.id,
                                   "expected a rejection but the request succeeded",
                                   variant=variant, state=state))
                continue
            ok = contains is None or contains.lower() in message.lower()
            out.append(finding("expect_error", "pass" if ok else "fail", case.id,
                               "rejected: %s" % " ".join(message.split())[:160],
                               variant=variant, state=state))
    return out


@handler("state_agreement")
def _state_agreement(case: Case, index: Index, rel: Mapping[str, Any]) -> List[Finding]:
    """Compare paired states to each other within a variant.

    Where variant_agreement asks "does the same content answered under a
    different question wording agree", this asks "does different content that
    should mean the same thing agree" - the shape needed for translation pairs.
    """
    pairs: Sequence[Sequence[str]] = rel["pairs"]
    tol = float(rel.get("tol", 0.15))
    out = []
    for variant in _variants(case, rel):
        qids = rel.get("questions") or list(case.variant(variant).questions)
        same, total, deltas, conf_deltas, misses = 0, 0, [], [], []
        for left, right in pairs:
            for qid in qids:
                a = index.answer(case.id, variant, left, qid)
                b = index.answer(case.id, variant, right, qid)
                if not a or not b:
                    continue
                total += 1
                ac, bc = field_of(a, "choice"), field_of(b, "choice")
                av, bv = field_of(a, "value"), field_of(b, "value")
                agreed = None
                if ac is not None and bc is not None:
                    agreed = ac == bc
                elif isinstance(av, (int, float)) and isinstance(bv, (int, float)):
                    agreed = abs(av - bv) <= tol
                if agreed:
                    same += 1
                elif agreed is False:
                    misses.append("%s/%s" % (right, qid))
                if isinstance(av, (int, float)) and isinstance(bv, (int, float)):
                    deltas.append(abs(av - bv))
                a_conf, b_conf = field_of(a, "confidence"), field_of(b, "confidence")
                if isinstance(a_conf, (int, float)) and isinstance(b_conf, (int, float)):
                    conf_deltas.append(b_conf - a_conf)
        if not total:
            continue
        rate = same / total
        status = "info"
        if "min_agreement" in rel:
            status = "pass" if rate >= float(rel["min_agreement"]) else "fail"
        detail = ("%-8s agreement %.2f (%d/%d)  mean |delta value| %s  mean delta conf %s"
                  % (variant, rate, same, total,
                     M.fmt(M.mean(deltas) if deltas else None),
                     M.fmt(M.mean(conf_deltas) if conf_deltas else None)))
        if misses:
            detail += "  diverged: " + ", ".join(misses[:6])
        out.append(finding("state_agreement", status, case.id, detail,
                           variant=variant, agreement=rate))
    return out


@handler("batching_gain")
def _batching_gain(case: Case, index: Index, rel: Mapping[str, Any]) -> List[Finding]:
    """One call with N questions versus N calls with one question each."""
    batched, split = rel["batched"], rel["split"]
    out = []
    for state in _states(case, rel):
        b = index.trial_stats(case.id, batched, state)
        s = index.trial_stats(case.id, split, state)
        if not b or not s:
            out.append(finding("batching_gain", "fail", case.id,
                               "missing trials", state=state))
            continue
        token_ratio = (s["input_tokens"] / b["input_tokens"]) if b["input_tokens"] else float("nan")
        cost_ratio = (s["cost"] / b["cost"]) if b["cost"] else float("nan")
        speed_ratio = (s["latency_ms"] / b["latency_ms"]) if b["latency_ms"] else float("nan")
        out.append(finding(
            "batching_gain", "info", case.id,
            "%d calls vs 1: cost x%s cheaper, wall-clock x%s faster, input tokens x%s"
            % (s["calls"], M.fmt(cost_ratio, 1), M.fmt(speed_ratio, 1), M.fmt(token_ratio, 1)),
            state=state, cost_ratio=cost_ratio, speed_ratio=speed_ratio))

        # The docs also claim the answers themselves do not change.
        drifted = []
        for qid in case.variant(batched).questions:
            bv = index.mean_of(case.id, batched, state, qid, "value")
            sv = index.mean_of(case.id, split, state, qid, "value")
            bc = index.answer(case.id, batched, state, qid)
            sc = index.answer(case.id, split, state, qid)
            if isinstance(bv, (int, float)) and isinstance(sv, (int, float)):
                if abs(bv - sv) > float(rel.get("max_delta", 0.05)):
                    drifted.append("%s %.2f->%.2f" % (qid, bv, sv))
            if bc and sc and field_of(bc, "choice") != field_of(sc, "choice"):
                drifted.append("%s %s->%s" % (qid, field_of(bc, "choice"),
                                              field_of(sc, "choice")))
        out.append(finding(
            "batching_gain.answers_unchanged",
            "pass" if not drifted else "fail", case.id,
            "answers identical" if not drifted else "drift: " + "; ".join(drifted),
            state=state))
    return out


@handler("latency_profile")
def _latency_profile(case: Case, index: Index, rel: Mapping[str, Any]) -> List[Finding]:
    """Response time against the number of questions in the request."""
    out = []
    for variant in _variants(case, rel):
        lats = []
        n_questions = len(case.variant(variant).questions)
        for state in _states(case, rel):
            for entry in index.reps(case.id, variant, state):
                lats.append(entry["latency_ms"])
        if not lats:
            continue
        out.append(finding(
            "latency_profile", "info", case.id,
            "%-12s %2d questions  n=%d  p50=%sms  p95=%sms  mean=%sms"
            % (variant, n_questions, len(lats),
               M.fmt(M.percentile(lats, 0.5), 0), M.fmt(M.percentile(lats, 0.95), 0),
               M.fmt(M.mean(lats), 0)),
            variant=variant, questions=n_questions,
            p50=M.percentile(lats, 0.5), p95=M.percentile(lats, 0.95)))
    return out


@handler("report")
def _report(case: Case, index: Index, rel: Mapping[str, Any]) -> List[Finding]:
    """Print answers without judging them. For probes with no expected result."""
    out = []
    for variant in _variants(case, rel):
        for state in _states(case, rel):
            if not case.state(state).runs_on(variant):
                continue  # this pairing was never meant to be collected
            for qid in (rel.get("questions") or list(case.variant(variant).questions)):
                answer = index.answer(case.id, variant, state, qid)
                if answer is None:
                    out.append(finding("report", "warn", case.id,
                                       "%s/%s/%s: no answer" % (variant, state, qid),
                                       variant=variant, state=state, question=qid))
                    continue
                value = field_of(answer, "value")
                conf = field_of(answer, "confidence")
                picked = field_of(answer, "choice")
                bits = []
                if picked is not None:
                    bits.append("choice=%s" % picked)
                if isinstance(value, (int, float)):
                    bits.append("value=%.3f" % value)
                if isinstance(conf, (int, float)):
                    bits.append("conf=%.3f" % conf)
                out.append(finding("report", "info", case.id,
                                   "%s/%s/%s: %s" % (variant, state, qid, "  ".join(bits)),
                                   variant=variant, state=state, question=qid))
    return out


def check_relations(case: Case, index: Index) -> List[Finding]:
    out: List[Finding] = []
    for rel in case.relations:
        fn = HANDLERS.get(rel["type"])
        if fn is None:
            out.append(finding("relation", "fail", case.id,
                               "unknown relation type %r" % rel["type"]))
            continue
        try:
            found = fn(case, index, rel)
            # Same distinction as for expectations: a relation the case author
            # predicted rather than read in the docs reports as a warning.
            if rel.get("hypothesis"):
                for f in found:
                    if f["status"] == "fail":
                        f["status"] = "warn"
            out.extend(found)
        except (KeyError, TypeError, ValueError) as exc:
            out.append(finding(rel["type"], "fail", case.id,
                               "relation could not be evaluated: %s" % exc))
    return out


def check_case(case: Case, index: Index) -> List[Finding]:
    return check_expectations(case, index) + check_relations(case, index)
