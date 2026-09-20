"""Structural checks that need no ground truth.

These verify the response is internally consistent with what was asked and with
the arithmetic the docs describe. If any of these fail, every downstream
measurement is suspect, so they run first and their failures are fatal.
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping

TOL = 0.011  # responses are rounded to two decimals


def _finding(rule: str, status: str, detail: str, **ctx: Any) -> Dict[str, Any]:
    out = {"rule": rule, "status": status, "detail": detail}
    out.update(ctx)
    return out


def check_record(record: Mapping[str, Any]) -> List[Dict[str, Any]]:
    """Validate one collected trial. Returns a finding per violated rule."""
    out: List[Dict[str, Any]] = []
    if record.get("error") or not record.get("response"):
        return out

    ctx = {
        "case": record.get("case"),
        "variant": record.get("variant"),
        "state": record.get("state"),
        "rep": record.get("rep"),
    }
    asked: Dict[str, Any] = record["request"]["questions"]
    answers = record["response"].get("answers")

    if not isinstance(answers, dict):
        return [_finding("answers_present", "fail",
                         "response has no answers object", question=None, **ctx)]

    missing = set(asked) - set(answers)
    extra = set(answers) - set(asked)
    if missing:
        out.append(_finding("answer_ids_match", "fail",
                            "no answer for %s" % ", ".join(sorted(missing)),
                            question=None, **ctx))
    if extra:
        out.append(_finding("answer_ids_match", "fail",
                            "unexpected answer ids %s" % ", ".join(sorted(extra)),
                            question=None, **ctx))

    for qid, question in asked.items():
        answer = answers.get(qid)
        if not isinstance(answer, dict):
            continue
        out.extend(_check_answer(qid, question, answer, ctx))
    return out


def _check_answer(
    qid: str, question: Mapping[str, Any], answer: Mapping[str, Any], ctx: Dict[str, Any]
) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    want_type = question.get("type")
    got_type = answer.get("type")
    c = dict(ctx, question=qid)

    if got_type != want_type:
        out.append(_finding("type_matches", "fail",
                            "asked %s, got %s" % (want_type, got_type), **c))
        return out

    probs = answer.get("probabilities")
    if want_type in ("choice", "score"):
        if not isinstance(probs, dict):
            out.append(_finding("probabilities_present", "fail",
                                "%s answer has no probabilities" % want_type, **c))
        else:
            total = sum(probs.values())
            if abs(total - 1.0) > TOL:
                out.append(_finding("probabilities_sum_to_one", "fail",
                                    "probabilities sum to %.4f" % total, **c))
        conf = answer.get("confidence")
        if conf is None:
            out.append(_finding("confidence_present", "fail",
                                "%s answer carries no confidence" % want_type, **c))
        elif not 0.0 <= conf <= 1.0:
            out.append(_finding("confidence_in_range", "fail",
                                "confidence %r outside [0, 1]" % conf, **c))

    if want_type == "noul":
        value = answer.get("noul")
        if not isinstance(value, (int, float)):
            out.append(_finding("noul_present", "fail", "noul is %r" % value, **c))
        elif not 0.0 <= value <= 1.0:
            out.append(_finding("noul_in_range", "fail",
                                "noul %r outside [0, 1]" % value, **c))
        # The docs are explicit that a two-outcome distribution needs no
        # separate confidence. If one ever appears, the reader should know.
        if "confidence" in answer:
            out.append(_finding("noul_has_no_confidence", "fail",
                                "noul answer carries confidence=%r" % answer["confidence"], **c))

    elif want_type == "choice":
        options = list((question.get("criteria") or {}).keys())
        picked = answer.get("choice")
        if picked not in options:
            out.append(_finding("choice_within_options", "fail",
                                "chose %r, options were %s" % (picked, options), **c))
        if isinstance(probs, dict):
            if set(probs) != set(options):
                out.append(_finding("probability_keys_match_options", "fail",
                                    "probabilities over %s, options were %s"
                                    % (sorted(probs), sorted(options)), **c))
            elif picked in probs:
                best = max(probs.values())
                if probs[picked] < best - TOL:
                    out.append(_finding("choice_is_argmax", "fail",
                                        "chose %r at %.3f but %.3f is higher"
                                        % (picked, probs[picked], best), **c))

    elif want_type == "score":
        levels = question.get("criteria") or []
        n = len(levels)
        value = answer.get("score")
        legend = answer.get("legend")

        if isinstance(legend, dict) and set(legend) != {str(i) for i in range(n)}:
            out.append(_finding("legend_matches_levels", "fail",
                                "legend keys %s for %d levels" % (sorted(legend), n), **c))
        if isinstance(value, (int, float)) and n:
            if not -TOL <= value <= (n - 1) + TOL:
                out.append(_finding("score_in_level_range", "fail",
                                    "score %.3f outside [0, %d]" % (value, n - 1), **c))
            if isinstance(probs, dict):
                # The docs define score as the probability-weighted mean of the
                # level numbers. Verify the arithmetic rather than trusting it.
                expected = sum(int(k) * v for k, v in probs.items())
                # Probabilities come back rounded to two decimals, and that
                # rounding is multiplied by the level number before it is
                # summed, so the slack has to grow with the number of levels.
                slack = 0.01 + 0.005 * (n - 1)
                if abs(expected - value) > slack:
                    out.append(_finding("score_equals_weighted_mean", "fail",
                                        "score %.3f but sum(level x p) = %.3f (slack %.3f)"
                                        % (value, expected, slack), **c))
    return out


def summarize(findings: List[Dict[str, Any]]) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for f in findings:
        counts[f["rule"]] = counts.get(f["rule"], 0) + 1
    return counts
