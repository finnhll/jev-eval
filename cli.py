#!/usr/bin/env python3
"""jev-eval: validate Jev's Choice, Score and Noul primitives.

    python3 cli.py plan                 what would run, and what it costs
    python3 cli.py collect              call the API, append to results/raw.jsonl
    python3 cli.py check                evaluate what was collected (offline)
    python3 cli.py report --out r.html  write the HTML report

Collect once, check many times. Every check reads the stored responses, so
thresholds and metrics can be reworked without spending another call.
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Any, Dict, List, Sequence

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from eval import checks, invariants  # noqa: E402
from eval.metrics import fmt  # noqa: E402
from jev.cases import CaseError, load_cases  # noqa: E402
from jev.client import DEFAULT_MODEL, JevClient, JevError  # noqa: E402
from jev.runner import DEFAULT_RESULTS, ResultStore, Runner, estimate  # noqa: E402

CASES_DIR = "cases"
STATUS_ORDER = {"fail": 0, "warn": 1, "pass": 2, "info": 3}
GREEN, RED, YELLOW, DIM, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"


def colorize(status: str) -> str:
    if not sys.stdout.isatty():
        return status.upper().ljust(4)
    color = {"pass": GREEN, "fail": RED, "warn": YELLOW, "info": DIM}.get(status, "")
    return "%s%s%s" % (color, status.upper().ljust(4), RESET)


def load(args: argparse.Namespace):
    try:
        return load_cases(args.cases, args.select)
    except CaseError as exc:
        print("error: %s" % exc, file=sys.stderr)
        raise SystemExit(2)


# --------------------------------------------------------------------------

def cmd_plan(args: argparse.Namespace) -> int:
    cases = load(args)
    est = estimate(cases)
    print("%-26s %-8s %3s %3s %3s %6s  %s"
          % ("CASE", "GROUP", "VAR", "ST", "REP", "TRIALS", "TITLE"))
    for row in est["rows"]:
        print("%-26s %-8s %3d %3d %3d %6d  %s"
              % (row["case"], row["group"], row["variants"], row["states"],
                 row["repeat"], row["trials"], row["title"][:48]))
    # Observed rate on this account: roughly $0.00002 for a 3-question call.
    print("\n%d cases, %d trials, rough cost $%.3f"
          % (len(cases), est["total"], est["total"] * 0.00002))
    return 0


def cmd_collect(args: argparse.Namespace) -> int:
    cases = load(args)
    if not cases:
        print("no cases matched", file=sys.stderr)
        return 2
    try:
        client = JevClient(model=args.model, timeout=args.timeout)
    except JevError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1
    store = ResultStore(args.results)
    runner = Runner(client, store, args.model, workers=args.workers)
    runner.run(cases, force=args.force)
    return 1 if runner.failed else 0


def cmd_check(args: argparse.Namespace) -> int:
    cases = load(args)
    store = ResultStore(args.results)
    # Failures are read too: a case can assert that a shape is rejected.
    records = store.records
    if not records:
        print("no results in %s - run collect first" % args.results, file=sys.stderr)
        return 2
    wanted = {c.id for c in cases}
    records = [r for r in records if r["case"] in wanted]
    aliases = {(c.id, v.id): v.alias for c in cases for v in c.variants if v.alias}
    index = checks.Index(records, aliases)

    structural: List[Dict[str, Any]] = []
    for rec in records:
        structural.extend(invariants.check_record(rec))

    print("=" * 78)
    print("STRUCTURAL INVARIANTS  (%d responses)" % len(records))
    print("=" * 78)
    if structural:
        for f in structural[:40]:
            print("  %s %s/%s/%s %s: %s"
                  % (colorize("fail"), f.get("case"), f.get("variant"),
                     f.get("state"), f.get("question"), f["detail"]))
        if len(structural) > 40:
            print("  ... %d more" % (len(structural) - 40))
        print("\n  %d violation(s). Downstream numbers are suspect." % len(structural))
    else:
        print("  %s all responses internally consistent" % colorize("pass"))

    all_findings: List[Dict[str, Any]] = []
    for case in cases:
        findings = checks.check_case(case, index)
        if not findings:
            continue
        all_findings.extend(findings)
        print("\n" + "=" * 78)
        print("%s  [%s]" % (case.title, case.id))
        if case.doc_ref:
            print("%sdoc: %s%s" % (DIM if sys.stdout.isatty() else "", case.doc_ref,
                                   RESET if sys.stdout.isatty() else ""))
        print("=" * 78)
        findings.sort(key=lambda f: (STATUS_ORDER.get(f["status"], 9), f["check"]))
        shown = findings if args.verbose else _trim(findings)
        for f in shown:
            where = "/".join(str(f[k]) for k in ("variant", "state", "question") if f.get(k))
            print("  %s %-26s %-28s %s"
                  % (colorize(f["status"]), f["check"], where[:28], f["detail"]))
        hidden = len(findings) - len(shown)
        if hidden:
            print("  %s... %d passing findings hidden (use -v)%s"
                  % (DIM if sys.stdout.isatty() else "", hidden,
                     RESET if sys.stdout.isatty() else ""))

    print("\n" + "=" * 78)
    counts: Dict[str, int] = {}
    for f in all_findings:
        counts[f["status"]] = counts.get(f["status"], 0) + 1
    print("SUMMARY  " + "  ".join(
        "%s %d" % (colorize(s), counts.get(s, 0))
        for s in ("pass", "fail", "warn", "info")))
    if structural:
        print("         %s %d structural violations" % (colorize("fail"), len(structural)))
    return 1 if (counts.get("fail") or structural) else 0


def _trim(findings: Sequence[Dict[str, Any]], keep_passing: int = 3,
          keep_info: int = 10) -> List[Dict[str, Any]]:
    """Show every failure, a sample of repetitive passes, and most info rows.

    A repeated pass carries no information beyond the first few; an info row
    is the measurement itself, so it is kept far longer.
    """
    out, seen = [], {}
    for f in findings:
        if f["status"] in ("fail", "warn"):
            out.append(f)
            continue
        key = (f["check"], f["status"])
        seen[key] = seen.get(key, 0) + 1
        limit = keep_info if f["status"] == "info" else keep_passing
        if seen[key] <= limit:
            out.append(f)
    return out


def cmd_report(args: argparse.Namespace) -> int:
    from report import write_report  # local module

    cases = load(args)
    store = ResultStore(args.results)
    records = store.successful()
    if not records:
        print("no results in %s - run collect first" % args.results, file=sys.stderr)
        return 2
    path = write_report(cases, records, args.out)
    print("wrote %s" % path)
    return 0


def main(argv: Sequence[str] = None) -> int:
    p = argparse.ArgumentParser(prog="jev-eval", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    # Shared options live on every subcommand so they can follow it on the
    # command line, which is the order people actually type.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--cases", default=CASES_DIR, help="case file or directory")
    common.add_argument("--select", nargs="*", help="case ids, id prefixes, or groups")
    common.add_argument("--results", default=DEFAULT_RESULTS, help="raw results JSONL")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("plan", parents=[common],
                   help="list what would run").set_defaults(fn=cmd_plan)

    c = sub.add_parser("collect", parents=[common],
                       help="call the API and store raw responses")
    c.add_argument("--model", default=DEFAULT_MODEL)
    c.add_argument("--workers", type=int, default=4)
    c.add_argument("--timeout", type=float, default=60.0)
    c.add_argument("--force", action="store_true", help="re-run trials already stored")
    c.set_defaults(fn=cmd_collect)

    k = sub.add_parser("check", parents=[common],
                       help="evaluate stored responses offline")
    k.add_argument("-v", "--verbose", action="store_true", help="show every finding")
    k.set_defaults(fn=cmd_check)

    r = sub.add_parser("report", parents=[common],
                       help="write the HTML report")
    r.add_argument("--out", default="results/report.html")
    r.set_defaults(fn=cmd_report)

    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
