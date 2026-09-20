"""Local server for the web UI.

Serving the UI from here adds two things the static page cannot do on its own:
a playground that calls the API, and a button that re-runs a case. The key is
read from the environment on this side and never reaches the browser.

Binds to the loopback interface only, and refuses cross-origin requests, so a
page on another site cannot drive it.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import webbrowser
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict

import uidata
from eval.metrics import fmt  # noqa: F401  (kept for parity with the CLI output)
from jev.cases import load_cases
from jev.client import JevClient, JevError
from jev.runner import ResultStore, Runner

DOCS = "docs"
MAX_BODY = 1 << 20  # 1 MB is far more than any question set needs


class Handler(SimpleHTTPRequestHandler):
    config: Dict[str, Any] = {}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=self.config["docs"], **kwargs)

    # --- logging ---------------------------------------------------------
    def log_message(self, fmt_str, *args):
        if self.config.get("verbose"):
            super().log_message(fmt_str, *args)

    # --- guards ----------------------------------------------------------
    def _local_only(self) -> bool:
        """Reject anything driven from another origin."""
        origin = self.headers.get("Origin")
        if origin and not (origin.startswith("http://127.0.0.1")
                           or origin.startswith("http://localhost")):
            self._json({"error": "cross-origin requests are refused"}, 403)
            return False
        return True

    def _body(self) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            raise ValueError("request body too large")
        return json.loads(self.rfile.read(length) or b"{}")

    def _json(self, payload: Dict[str, Any], status: int = 200) -> None:
        blob = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(blob)))
        self.end_headers()
        self.wfile.write(blob)

    def end_headers(self):
        # A development server that caches its own assets wastes an afternoon.
        self.send_header("Cache-Control", "no-store, must-revalidate")
        super().end_headers()

    # --- routes ----------------------------------------------------------
    def do_GET(self):  # noqa: N802
        if self.path.startswith("/api/capabilities"):
            self._json({"live": True, "model": self.config["model"]})
            return
        if self.path.startswith("/api/check"):
            self._json(self._rebuild())
            return
        if self.path == "/data.json":
            # Always serve the freshest build rather than a stale file.
            self._rebuild()
        super().do_GET()

    def do_POST(self):  # noqa: N802
        if not self._local_only():
            return
        try:
            if self.path.startswith("/api/decide"):
                self._json(self._decide(self._body()))
            elif self.path.startswith("/api/collect"):
                self._json(self._collect(self._body()))
            else:
                self._json({"error": "no such endpoint"}, 404)
        except JevError as exc:
            self._json({"error": str(exc)}, 502)
        except ValueError as exc:
            self._json({"error": "bad request: %s" % exc}, 400)
        except Exception as exc:  # a crash here must not kill the server
            self._json({"error": "%s: %s" % (type(exc).__name__, exc)}, 500)

    # --- work ------------------------------------------------------------
    def _decide(self, body: Dict[str, Any]) -> Dict[str, Any]:
        questions = body.get("questions")
        if not isinstance(questions, dict) or not questions:
            raise ValueError("questions must be a non-empty object")
        if "state" not in body:
            raise ValueError("state is required")
        client: JevClient = self.config["client"]
        return client.post({
            "model": body.get("model") or self.config["model"],
            "state": body["state"],
            "questions": questions,
        })

    def _collect(self, body: Dict[str, Any]) -> Dict[str, Any]:
        select = body.get("select") or None
        cases = load_cases(self.config["cases"], select)
        if not cases:
            raise ValueError("no cases matched")
        with self.config["lock"]:
            store = ResultStore(self.config["results"])
            runner = Runner(self.config["client"], store, self.config["model"],
                            workers=4, on_progress=lambda *_: None)
            runner.run(cases, force=bool(body.get("force")))
            summary = {"collected": runner.done, "failed": runner.failed,
                       "cached": runner.skipped}
        summary.update(self._rebuild())
        return summary

    def _rebuild(self) -> Dict[str, Any]:
        """Regenerate data.json from whatever is on disk now."""
        with self.config["lock"]:
            cases = load_cases(self.config["cases"])
            store = ResultStore(self.config["results"])
            uidata.write(cases, store.records, self.config["data_out"])
        return {"rebuilt": True}


def main(args: argparse.Namespace) -> int:
    root = os.path.dirname(os.path.abspath(__file__))
    docs = os.path.join(root, DOCS)
    if not os.path.isdir(docs):
        print("error: %s is missing" % docs, file=sys.stderr)
        return 2
    try:
        client = JevClient(model=args.model, timeout=60.0)
    except JevError as exc:
        print("error: %s" % exc, file=sys.stderr)
        print("The playground and re-run buttons need a key. To browse the stored "
              "results without one, open docs/index.html through any static server.",
              file=sys.stderr)
        return 1

    Handler.config = {
        "docs": docs,
        "cases": args.cases,
        "results": args.results,
        "data_out": args.out,
        "model": args.model,
        "client": client,
        "lock": threading.Lock(),
        "verbose": False,
    }

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    url = "http://127.0.0.1:%d/" % args.port
    print("jev-eval UI on %s   (ctrl-c to stop)" % url)
    print("live mode: the playground and per-case re-runs will call the API")
    if not args.no_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        server.server_close()
    return 0
