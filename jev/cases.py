"""Loading declarative case files and expanding them into API trials.

A case file is one experiment: a set of questions (or several named variants of
them), a list of states, per-state expectations, and relations that must hold
across states or variants. Expanding a case yields one Trial per
(variant, state, repeat), and each Trial becomes exactly one API request.

Schema (JSON):

    {
      "id": "n1_doc_baseline",
      "title": "Reproduce the documented is_human_escalation values",
      "group": "noul",
      "note": "Free text explaining what the experiment is for.",
      "repeat": 1,

      // Either a single question set...
      "questions": {"<question_id>": {"type": ..., "instructions": ...}},
      // ...or several named variants of it.
      "variants": [{"id": "baseline", "questions": {...}, "note": "..."}],

      "states": [
        {
          "id": "fixed_it",
          "state": "Thanks, that fixed it!",
          "expect": {"<question_id>": {"near": 0.02, "tol": 0.1}},
          "expect_by_variant": {"<variant_id>": {"<question_id>": {...}}},
          "only_variants": ["baseline"]
        }
      ],

      "relations": [{"type": "rank", "question": "...", "order": [...]}]
    }

Expectation forms, all optional and combinable:

    {"near": 0.5, "tol": 0.1}   value within tol of near
    {"min": 0.8}                value at least this
    {"max": 0.2}                value at most this
    {"band": [0.4, 0.6]}        value inside the closed interval
    {"equals": "billing"}       choice equals this option
    {"one_of": ["a", "b"]}      choice is one of these
    {"conf_min": 0.8}           confidence at least this
    {"conf_max": 0.5}           confidence at most this

"value" means noul for a noul answer and score for a score answer.
"""

from __future__ import annotations

import hashlib
import json
import os
from typing import Any, Dict, Iterator, List, Mapping, Optional, Sequence

CASE_SUFFIX = ".json"

# Fields the Decisions API accepts inside a question object.
QUESTION_FIELDS = frozenset({"type", "instructions", "criteria"})


class CaseError(ValueError):
    """A case file is malformed."""


class Variant:
    """One named question set inside a case."""

    def __init__(self, case_id: str, raw: Mapping[str, Any]):
        self.id: str = raw.get("id") or "default"
        self.note: str = raw.get("note", "")
        # A split variant issues one request per question instead of one
        # request carrying all of them. Used to measure what batching buys.
        self.split: bool = bool(raw.get("split", False))
        # Maps this variant's option names onto canonical ones, so a variant
        # that renames or translates its options stays comparable to the rest.
        self.alias: Dict[str, str] = dict(raw.get("alias") or {})
        # Set when the API is expected to reject this shape: the rejection is
        # the result, so it is not retried and not reported as a run failure.
        self.expect_error: bool = bool(raw.get("expect_error", False))
        self.questions: Dict[str, Any] = {}
        self.annotations: Dict[str, Dict[str, Any]] = {}
        for qid, q in (raw.get("questions") or {}).items():
            qtype = q.get("type")
            if qtype not in ("noul", "choice", "score"):
                raise CaseError("%s/%s/%s: bad question type %r" % (case_id, self.id, qid, qtype))
            # Anything outside the wire schema would be sent to the model as
            # part of the question, so case-file annotations are stripped here
            # and kept beside the question instead.
            self.questions[qid] = {k: v for k, v in q.items() if k in QUESTION_FIELDS}
            extra = {k: v for k, v in q.items() if k not in QUESTION_FIELDS}
            if extra:
                self.annotations[qid] = extra
        if not self.questions:
            raise CaseError("%s/%s: variant has no questions" % (case_id, self.id))


class State:
    """One piece of content to evaluate, with its expectations."""

    def __init__(self, case_id: str, raw: Mapping[str, Any], index: int):
        self.id: str = raw.get("id") or "s%d" % index
        if "state" not in raw:
            raise CaseError("%s/%s: state entry has no 'state' field" % (case_id, self.id))
        self.state: Any = raw["state"]
        self.note: str = raw.get("note", "")
        self.expect: Dict[str, Any] = dict(raw.get("expect") or {})
        self.expect_by_variant: Dict[str, Any] = dict(raw.get("expect_by_variant") or {})
        self.only_variants: Optional[List[str]] = raw.get("only_variants")
        self.tags: List[str] = list(raw.get("tags") or [])

    def expectations_for(self, variant_id: str) -> Dict[str, Any]:
        """Per-variant expectations override the shared ones, question by question."""
        merged = dict(self.expect)
        merged.update(self.expect_by_variant.get(variant_id) or {})
        return merged

    def runs_on(self, variant_id: str) -> bool:
        return self.only_variants is None or variant_id in self.only_variants


class Trial:
    """One state evaluated against one variant, once. Becomes one API request."""

    def __init__(
        self,
        case: "Case",
        variant: Variant,
        state: State,
        rep: int,
        only_question: Optional[str] = None,
    ):
        self.case = case
        self.variant = variant
        self.state = state
        self.rep = rep
        # Set on split variants: this request carries just the one question.
        self.only_question = only_question

    @property
    def key(self) -> str:
        suffix = "|%s" % self.only_question if self.only_question else ""
        return "%s/%s/%s#%d%s" % (
            self.case.id, self.variant.id, self.state.id, self.rep, suffix,
        )

    @property
    def questions(self) -> Dict[str, Any]:
        if self.only_question:
            return {self.only_question: self.variant.questions[self.only_question]}
        return self.variant.questions

    def request(self, model: str) -> Dict[str, Any]:
        return {
            "model": model,
            "state": self.state.state,
            "questions": self.questions,
        }

    def request_hash(self, model: str) -> str:
        """Identity of this trial for caching. Repeats hash differently on purpose."""
        blob = json.dumps(
            {"request": self.request(model), "rep": self.rep, "key": self.key},
            sort_keys=True, ensure_ascii=False,
        )
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


class Case:
    """One experiment loaded from one file."""

    def __init__(self, raw: Mapping[str, Any], path: Optional[str] = None):
        self.path = path
        self.id: str = raw.get("id") or (os.path.basename(path or "").replace(CASE_SUFFIX, ""))
        if not self.id:
            raise CaseError("case has no id (file: %s)" % path)
        self.title: str = raw.get("title", self.id)
        self.group: str = raw.get("group", "misc")
        self.note: str = raw.get("note", "")
        self.repeat: int = int(raw.get("repeat", 1))
        # Force serial execution when the experiment measures wall-clock time.
        self.sequential: bool = bool(raw.get("sequential", False))
        self.doc_ref: str = raw.get("doc_ref", "")

        if "variants" in raw and "questions" in raw:
            raise CaseError("%s: use either 'questions' or 'variants', not both" % self.id)
        if "variants" in raw:
            self.variants = [Variant(self.id, v) for v in raw["variants"]]
        elif "questions" in raw:
            self.variants = [Variant(self.id, {"id": "default", "questions": raw["questions"]})]
        else:
            raise CaseError("%s: case has neither 'questions' nor 'variants'" % self.id)

        seen = set()
        for v in self.variants:
            if v.id in seen:
                raise CaseError("%s: duplicate variant id %r" % (self.id, v.id))
            seen.add(v.id)

        states = raw.get("states")
        if not states:
            raise CaseError("%s: case has no states" % self.id)
        self.states = [State(self.id, s, i) for i, s in enumerate(states)]

        state_ids = set()
        for s in self.states:
            if s.id in state_ids:
                raise CaseError("%s: duplicate state id %r" % (self.id, s.id))
            state_ids.add(s.id)

        self.relations: List[Dict[str, Any]] = list(raw.get("relations") or [])
        self._validate_relations(state_ids, seen)

    def _validate_relations(self, state_ids: set, variant_ids: set) -> None:
        for rel in self.relations:
            if "type" not in rel:
                raise CaseError("%s: relation without a type" % self.id)
            for field in ("order", "high", "low", "states"):
                for sid in rel.get(field) or []:
                    if sid not in state_ids:
                        raise CaseError(
                            "%s: relation %s references unknown state %r"
                            % (self.id, rel["type"], sid)
                        )
            for field in ("variant", "than_variant"):
                vid = rel.get(field)
                if vid is not None and vid not in variant_ids:
                    raise CaseError(
                        "%s: relation %s references unknown variant %r"
                        % (self.id, rel["type"], vid)
                    )

    def state(self, state_id: str) -> State:
        for s in self.states:
            if s.id == state_id:
                return s
        raise KeyError(state_id)

    def variant(self, variant_id: str) -> Variant:
        for v in self.variants:
            if v.id == variant_id:
                return v
        raise KeyError(variant_id)

    def trials(self) -> Iterator[Trial]:
        for variant in self.variants:
            for state in self.states:
                if not state.runs_on(variant.id):
                    continue
                for rep in range(self.repeat):
                    if variant.split:
                        for qid in variant.questions:
                            yield Trial(self, variant, state, rep, only_question=qid)
                    else:
                        yield Trial(self, variant, state, rep)

    def trial_count(self) -> int:
        return sum(1 for _ in self.trials())


def load_case(path: str) -> Case:
    with open(path, "r", encoding="utf-8") as fh:
        try:
            raw = json.load(fh)
        except ValueError as exc:
            raise CaseError("%s: invalid JSON: %s" % (path, exc)) from exc
    return Case(raw, path=path)


def load_cases(root: str, select: Optional[Sequence[str]] = None) -> List[Case]:
    """Load every case file under root, optionally filtered by id or group."""
    paths: List[str] = []
    if os.path.isfile(root):
        paths = [root]
    else:
        for dirpath, _dirnames, filenames in os.walk(root):
            for name in sorted(filenames):
                if name.endswith(CASE_SUFFIX) and not name.startswith("_"):
                    paths.append(os.path.join(dirpath, name))
    cases = [load_case(p) for p in sorted(paths)]
    if select:
        wanted = set(select)
        cases = [
            c for c in cases
            if c.id in wanted or c.group in wanted or any(c.id.startswith(w) for w in wanted)
        ]
    ids = set()
    for c in cases:
        if c.id in ids:
            raise CaseError("duplicate case id %r" % c.id)
        ids.add(c.id)
    return cases
