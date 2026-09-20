#!/usr/bin/env python3
"""
Jev (~typesafe/jev-latest) client for OpenRouter's Decisions API.

Jev is not a chat model: it answers narrow, typed questions about a piece of
state and returns calibrated probabilities. Three question types:

  noul   -> probability in [0, 1] that the answer is "yes"
  choice -> one option from criteria you define, plus the full distribution
  score  -> a position on an ordered rubric you define

Endpoint: POST https://openrouter.ai/api/alpha/decisions
Auth:     Bearer $OPENROUTER_API_KEY

Stdlib only. Usage as a library:

    from jev import JevClient, noul, choice, score

    client = JevClient()
    d = client.decide(
        state="Help! My payouts have been failing for 3 days.",
        questions={
            "is_urgent": noul("Does this message convey urgency?",
                              true="Explicitly time-sensitive",
                              false="No urgency expressed"),
            "department": choice("Which team should handle this?", {
                "billing":   "Payments, invoicing, refunds",
                "technical": "Bugs, outages, integrations",
                "sales":     "Pricing, upgrades, new accounts",
            }),
            "frustration": score("How frustrated is the customer?",
                                 ["Calm", "Frustrated", "Very angry"]),
        },
    )
    if d["is_urgent"].noul > 0.8 and d["department"].choice == "billing":
        ...
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import http.client
import time
import urllib.error
import urllib.request
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Union

API_URL = "https://openrouter.ai/api/alpha/decisions"
DEFAULT_MODEL = "~typesafe/jev-latest"

# instructions / criteria entries may be a plain string or a nested object
Spec = Union[str, Mapping[str, Any], Sequence[Any]]


def _legend_text(value: Any) -> str:
    """A rubric level may be a string or an object; give it a short label."""
    if isinstance(value, Mapping):
        for key in ("what", "label", "name", "description"):
            if isinstance(value.get(key), str):
                return value[key]
        return json.dumps(value, ensure_ascii=False)
    return str(value)


class JevError(RuntimeError):
    """Any failure talking to the Decisions API."""


class JevAPIError(JevError):
    def __init__(self, status: int, body: str):
        self.status = status
        self.body = body
        detail = body.strip()
        try:
            parsed = json.loads(body)
            detail = json.dumps(parsed.get("error", parsed), ensure_ascii=False)
        except Exception:
            pass
        super().__init__("Decisions API returned HTTP %s: %s" % (status, detail[:1000]))


# --------------------------------------------------------------------------
# question builders
# --------------------------------------------------------------------------

def noul(instructions: Spec, true: Optional[Spec] = None, false: Optional[Spec] = None) -> Dict[str, Any]:
    """A yes/no question. The answer is a probability from 0 (no) to 1 (yes)."""
    q: Dict[str, Any] = {"type": "noul", "instructions": instructions}
    if true is not None or false is not None:
        q["criteria"] = {"true": true, "false": false}
    return q


def choice(instructions: Spec, criteria: Mapping[str, Spec]) -> Dict[str, Any]:
    """Pick one of the named options. Describe what each option covers."""
    if not criteria:
        raise ValueError("choice() needs at least one option in criteria")
    return {"type": "choice", "instructions": instructions, "criteria": dict(criteria)}


def score(instructions: Spec, criteria: Sequence[Spec]) -> Dict[str, Any]:
    """Place the state on an ordered rubric, lowest level first."""
    levels = list(criteria)
    if len(levels) < 2:
        raise ValueError("score() needs at least two rubric levels")
    return {"type": "score", "instructions": instructions, "criteria": levels}


# --------------------------------------------------------------------------
# response wrappers
# --------------------------------------------------------------------------

class Answer:
    """One question's answer. Unknown fields stay reachable through .raw."""

    def __init__(self, name: str, raw: Mapping[str, Any]):
        self.name = name
        self.raw: Dict[str, Any] = dict(raw)

    @property
    def type(self) -> Optional[str]:
        return self.raw.get("type")

    @property
    def noul(self) -> Optional[float]:
        return self.raw.get("noul")

    @property
    def choice(self) -> Optional[str]:
        return self.raw.get("choice")

    @property
    def score(self) -> Optional[float]:
        return self.raw.get("score")

    @property
    def probabilities(self) -> Optional[Dict[str, float]]:
        return self.raw.get("probabilities")

    @property
    def confidence(self) -> Optional[float]:
        """Present on choice and score answers; noul carries none."""
        return self.raw.get("confidence")

    @property
    def legend(self) -> Optional[Dict[str, Any]]:
        """score answers only: {"0": "Calm", "1": "Frustrated", ...}.

        Levels keep whatever shape you sent, so an entry may be an object.
        Use .labels for display-ready strings.
        """
        return self.raw.get("legend")

    @property
    def labels(self) -> Dict[str, str]:
        """The legend flattened to short strings, keyed by level."""
        return {k: _legend_text(v) for k, v in (self.legend or {}).items()}

    @property
    def label(self) -> Optional[str]:
        """The rubric label nearest to .score, e.g. "Frustrated"."""
        if self.score is None or not self.legend:
            return None
        return self.labels.get(str(int(round(self.score))))

    @property
    def value(self) -> Any:
        """The answer itself, whichever type this question was."""
        for key in ("noul", "choice", "score"):
            if self.raw.get(key) is not None:
                return self.raw[key]
        return None

    def __repr__(self) -> str:
        return "Answer(%s, type=%s, value=%r, confidence=%r)" % (
            self.name, self.type, self.value, self.confidence,
        )


class Decision:
    """A full Decisions API response."""

    def __init__(self, raw: Mapping[str, Any]):
        self.raw: Dict[str, Any] = dict(raw)
        answers = self.raw.get("answers") or {}
        if not isinstance(answers, dict):
            raise JevError("unexpected response shape: 'answers' is %s" % type(answers).__name__)
        self.answers: Dict[str, Answer] = {
            name: Answer(name, body if isinstance(body, dict) else {"value": body})
            for name, body in answers.items()
        }

    @property
    def id(self) -> Optional[str]:
        return self.raw.get("id")

    @property
    def model(self) -> Optional[str]:
        """The concrete model the slug resolved to, e.g. typesafe/jev-1.13-...."""
        return self.raw.get("model")

    @property
    def usage(self) -> Dict[str, Any]:
        """{"input_tokens": .., "output_tokens": .., "cost": ..} in USD."""
        return self.raw.get("usage") or {}

    def __getitem__(self, name: str) -> Answer:
        return self.answers[name]

    def __contains__(self, name: str) -> bool:
        return name in self.answers

    def __iter__(self) -> Iterable[str]:
        return iter(self.answers)

    def values(self) -> Dict[str, Any]:
        """Plain {question: answer} mapping, handy for logging."""
        return {name: a.value for name, a in self.answers.items()}


# --------------------------------------------------------------------------
# client
# --------------------------------------------------------------------------

class JevClient:
    def __init__(
        self,
        api_key: Optional[str] = None,
        model: str = DEFAULT_MODEL,
        url: str = API_URL,
        referer: Optional[str] = None,
        title: Optional[str] = None,
        timeout: float = 30.0,
        max_retries: int = 2,
    ):
        self.api_key = api_key or os.environ.get("OPENROUTER_API_KEY")
        if not self.api_key:
            raise JevError(
                "OPENROUTER_API_KEY is not set. Export it first:\n"
                "  export OPENROUTER_API_KEY=sk-or-v1-..."
            )
        self.model = model
        self.url = url
        # Optional, only affects OpenRouter leaderboard attribution.
        self.referer = referer or os.environ.get("OPENROUTER_SITE_URL")
        self.title = title or os.environ.get("OPENROUTER_SITE_NAME")
        self.timeout = timeout
        self.max_retries = max_retries

    def build_payload(
        self,
        state: Any,
        questions: Mapping[str, Mapping[str, Any]],
        model: Optional[str] = None,
        extra: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        if not questions:
            raise ValueError("at least one question is required")
        payload: Dict[str, Any] = {
            "model": model or self.model,
            "state": state,
            "questions": dict(questions),
        }
        if extra:
            payload.update(extra)
        return payload

    def decide(
        self,
        state: Any,
        questions: Mapping[str, Mapping[str, Any]],
        model: Optional[str] = None,
        extra: Optional[Mapping[str, Any]] = None,
    ) -> Decision:
        """Ask all questions about one state. Questions are evaluated in parallel."""
        return Decision(self.post(self.build_payload(state, questions, model, extra)))

    def post(self, payload: Mapping[str, Any]) -> Dict[str, Any]:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {
            "Authorization": "Bearer %s" % self.api_key,
            "Content-Type": "application/json",
        }
        if self.referer:
            headers["HTTP-Referer"] = self.referer
        if self.title:
            headers["X-Title"] = self.title

        last_error: Optional[Exception] = None
        for attempt in range(self.max_retries + 1):
            req = urllib.request.Request(self.url, data=body, headers=headers, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    text = resp.read().decode("utf-8")
                try:
                    return json.loads(text)
                except ValueError as exc:
                    raise JevError("response was not JSON: %s" % text[:500]) from exc
            except urllib.error.HTTPError as exc:
                text = exc.read().decode("utf-8", "replace")
                # 429 and 5xx are worth retrying; 4xx means fix the request.
                if exc.code not in (408, 409, 429) and exc.code < 500:
                    raise JevAPIError(exc.code, text)
                last_error = JevAPIError(exc.code, text)
                wait = _retry_after(exc.headers.get("Retry-After")) or 2 ** attempt
            except urllib.error.URLError as exc:
                last_error = JevError("could not reach %s: %s" % (self.url, exc.reason))
                wait = 2 ** attempt
            except (http.client.HTTPException, OSError) as exc:
                # A dropped keep-alive connection surfaces here rather than as
                # a URLError, and is worth the same retry.
                last_error = JevError("connection to %s failed: %s: %s"
                                      % (self.url, type(exc).__name__, exc))
                wait = 2 ** attempt
            if attempt < self.max_retries:
                time.sleep(wait)
        raise last_error if last_error else JevError("request failed")


def _retry_after(value: Optional[str]) -> Optional[float]:
    try:
        return max(0.0, float(value)) if value else None
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

DEMO_STATE = "Help! My payouts have been failing for 3 days."

DEMO_QUESTIONS: Dict[str, Dict[str, Any]] = {
    "is_urgent": noul(
        "Does this message convey urgency?",
        true="Explicitly time-sensitive",
        false="No urgency expressed",
    ),
    "department": choice("Which team should handle this?", {
        "billing": "Payments, invoicing, refunds",
        "technical": "Bugs, outages, integrations",
        "sales": "Pricing, upgrades, new accounts",
    }),
    "frustration": score("How frustrated is the customer?",
                         ["Calm", "Frustrated", "Very angry"]),
}


def _load_json_arg(raw: str) -> Any:
    """Treat the value as JSON when it parses, otherwise as a plain string."""
    try:
        return json.loads(raw)
    except ValueError:
        return raw


def _format(decision: Decision) -> str:
    lines: List[str] = []
    width = max((len(n) for n in decision.answers), default=0)
    for name, a in decision.answers.items():
        parts = ["%-*s  %-6s" % (width, name, a.type or "?")]
        if a.noul is not None:
            parts.append("noul=%.3f" % a.noul)
        if a.choice is not None:
            parts.append("choice=%s" % a.choice)
        if a.score is not None:
            parts.append("score=%.2f%s" % (a.score, " (%s)" % a.label if a.label else ""))
        if a.confidence is not None:
            parts.append("confidence=%.3f" % a.confidence)
        if a.probabilities:
            items = list(a.probabilities.items())
            if a.type == "score":
                # ordinal rubric: keep the levels in order, not by likelihood
                items.sort(key=lambda kv: int(kv[0]))
                labels = a.labels
                items = [(labels.get(k, k), v) for k, v in items]
            else:
                items.sort(key=lambda kv: -kv[1])
            parts.append("[%s]" % ", ".join("%s=%.2f" % (k, v) for k, v in items))
        lines.append("  ".join(parts))

    usage = decision.usage
    if usage:
        lines.append("")
        lines.append("%s  %s in / %s out  $%.6f" % (
            decision.model or "",
            usage.get("input_tokens", "?"),
            usage.get("output_tokens", "?"),
            usage.get("cost", 0.0),
        ))
    return "\n".join(lines)


def main(argv: Optional[Sequence[str]] = None) -> int:
    p = argparse.ArgumentParser(
        description="Call Jev (~typesafe/jev-latest) on OpenRouter's Decisions API.",
        epilog="With no --state/--questions, a built-in support-ticket demo runs.",
    )
    p.add_argument("state", nargs="?", help="state to ask about (plain text, or JSON)")
    p.add_argument("--state-file", help="read the state from a file (JSON or text)")
    p.add_argument("-q", "--questions-file",
                   help='JSON file: {"name": {"type": "noul|choice|score", ...}}')
    p.add_argument("-m", "--model", default=DEFAULT_MODEL, help="model slug (default: %(default)s)")
    p.add_argument("--timeout", type=float, default=30.0)
    p.add_argument("--json", action="store_true", help="print the raw JSON response")
    p.add_argument("--dry-run", action="store_true", help="print the request payload and exit")
    args = p.parse_args(argv)

    if args.state_file:
        with open(args.state_file, "r", encoding="utf-8") as fh:
            state = _load_json_arg(fh.read())
    elif args.state is not None:
        state = _load_json_arg(args.state)
    else:
        state = DEMO_STATE

    if args.questions_file:
        with open(args.questions_file, "r", encoding="utf-8") as fh:
            questions = json.load(fh)
        if not isinstance(questions, dict):
            p.error("questions file must be a JSON object keyed by question name")
    else:
        questions = DEMO_QUESTIONS

    if args.dry_run:
        payload = {"model": args.model, "state": state, "questions": questions}
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0

    try:
        client = JevClient(model=args.model, timeout=args.timeout)
        decision = client.decide(state, questions)
    except JevError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(decision.raw, indent=2, ensure_ascii=False))
    else:
        print(_format(decision))
    return 0


if __name__ == "__main__":
    sys.exit(main())
