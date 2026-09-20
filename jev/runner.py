"""Executing trials against the API and recording raw results.

Collection is deliberately separate from analysis. Every response is appended
to a JSONL file together with the exact request that produced it, so the
analysis stage can be re-run, re-thresholded and re-metricked offline without
spending another call. Re-running collection skips trials already present.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, Iterable, List, Optional, Set

from .cases import Case, Trial
from .client import JevClient, JevError

DEFAULT_RESULTS = "results/raw.jsonl"


class ResultStore:
    """Append-only JSONL of trial records, keyed by request hash."""

    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()
        self._seen: Set[str] = set()
        self.errored: Set[str] = set()
        self.records: List[Dict[str, Any]] = []
        if os.path.exists(path):
            self.load()

    def load(self) -> None:
        self.records = []
        self._seen = set()
        with open(self.path, "r", encoding="utf-8") as fh:
            for line_no, line in enumerate(fh, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    print("warning: %s:%d is not valid JSON, skipping"
                          % (self.path, line_no), file=sys.stderr)
                    continue
                self.records.append(rec)
                # Only completed trials count as done; failures are retried
                # unless the case says the rejection is the expected result.
                if rec.get("hash"):
                    (self._seen if not rec.get("error") else self.errored).add(rec["hash"])

    def has(self, request_hash: str) -> bool:
        return request_hash in self._seen

    def append(self, record: Dict[str, Any]) -> None:
        with self._lock:
            directory = os.path.dirname(self.path)
            if directory:
                os.makedirs(directory, exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
            self.records.append(record)
            (self._seen if not record.get("error") else self.errored).add(record["hash"])

    def successful(self) -> List[Dict[str, Any]]:
        """Latest successful record per hash, in insertion order."""
        by_hash: Dict[str, Dict[str, Any]] = {}
        for rec in self.records:
            if not rec.get("error"):
                by_hash[rec["hash"]] = rec
        return list(by_hash.values())


class Runner:
    def __init__(
        self,
        client: JevClient,
        store: ResultStore,
        model: str,
        workers: int = 4,
        on_progress: Optional[Callable[[str, Dict[str, Any]], None]] = None,
    ):
        self.client = client
        self.store = store
        self.model = model
        self.workers = max(1, workers)
        self.on_progress = on_progress
        self.done = 0
        self.failed = 0
        self.skipped = 0
        self.expected = 0
        self.expected_errors: Set[str] = set()

    def run_trial(self, trial: Trial) -> Dict[str, Any]:
        request_hash = trial.request_hash(self.model)
        payload = trial.request(self.model)
        record: Dict[str, Any] = {
            "hash": request_hash,
            "case": trial.case.id,
            "group": trial.case.group,
            "variant": trial.variant.id,
            "state": trial.state.id,
            "rep": trial.rep,
            "only_question": trial.only_question,
            "key": trial.key,
            "request": payload,
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        started = time.time()
        try:
            raw = self.client.post(payload)
            record["latency_ms"] = round((time.time() - started) * 1000, 1)
            record["response"] = raw
            record["error"] = None
        except JevError as exc:
            record["latency_ms"] = round((time.time() - started) * 1000, 1)
            record["response"] = None
            record["error"] = str(exc)
        except Exception as exc:  # one bad trial must not abandon the batch
            record["latency_ms"] = round((time.time() - started) * 1000, 1)
            record["response"] = None
            record["error"] = "%s: %s" % (type(exc).__name__, exc)
        return record

    def run(self, cases: Iterable[Case], force: bool = False) -> None:
        cases = list(cases)
        pending_parallel: List[Trial] = []
        pending_serial: List[Trial] = []

        for case in cases:
            for trial in case.trials():
                request_hash = trial.request_hash(self.model)
                if not force and self.store.has(request_hash):
                    self.skipped += 1
                    continue
                if (not force and trial.variant.expect_error
                        and request_hash in self.store.errored):
                    self.skipped += 1
                    continue
                (pending_serial if case.sequential else pending_parallel).append(trial)

        self.expected_errors = {
            t.key for t in pending_parallel + pending_serial if t.variant.expect_error
        }
        total = len(pending_parallel) + len(pending_serial)
        if not total:
            print("nothing to collect: %d trials already recorded" % self.skipped)
            return
        print("collecting %d trials (%d cached, %d serial for timing)"
              % (total, self.skipped, len(pending_serial)))

        # Timing experiments run one at a time so wall-clock means something.
        for trial in pending_serial:
            self._finish(self.run_trial(trial), total)

        if pending_parallel:
            with ThreadPoolExecutor(max_workers=self.workers) as pool:
                for record in pool.map(self.run_trial, pending_parallel):
                    self._finish(record, total)

        print("\ncollected %d, failed %d, cached %d, rejected as expected %d"
              % (self.done, self.failed, self.skipped, self.expected))

    def _finish(self, record: Dict[str, Any], total: int) -> None:
        self.store.append(record)
        self.done += 1
        if record.get("error"):
            self.failed += 1
            if self.expected_errors and record["key"] in self.expected_errors:
                self.failed -= 1
                self.expected += 1
                return
            print("\n  FAIL %s: %s" % (record["key"], record["error"]), file=sys.stderr)
        if self.on_progress:
            self.on_progress(record["key"], record)
        elif sys.stdout.isatty():
            bar_width = 30
            filled = int(bar_width * self.done / total) if total else bar_width
            sys.stdout.write("\r  [%s%s] %d/%d  %s"
                             % ("#" * filled, "." * (bar_width - filled), self.done, total,
                                record["key"][:40].ljust(40)))
            sys.stdout.flush()
        elif self.done % 25 == 0 or self.done == total:
            # Redirected output gets occasional lines instead of a redrawn bar.
            print("  %d/%d" % (self.done, total))


def estimate(cases: Iterable[Case]) -> Dict[str, Any]:
    """Trial counts per case, for a dry run."""
    rows = []
    total = 0
    for case in cases:
        count = case.trial_count()
        total += count
        rows.append({
            "case": case.id,
            "group": case.group,
            "title": case.title,
            "variants": len(case.variants),
            "states": len(case.states),
            "repeat": case.repeat,
            "trials": count,
        })
    return {"rows": rows, "total": total}
