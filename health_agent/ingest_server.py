"""
Ingest server — the URL your phone posts steps and workouts to
==============================================================
A tiny stdlib HTTP server (no Flask). One endpoint:

  POST /ingest     body = Health Auto Export JSON, or {"date","steps","source"}
                   auth = header  "Authorization: Bearer <HEALTH_INGEST_TOKEN>"
                          (or ?token=<...> for apps that can't set headers)
  GET  /ping       liveness check, no auth

The token is REQUIRED — the server refuses to start without one, because
this endpoint writes into your health record.

The phone has to be able to reach this machine. Easiest: install Tailscale on
the phone and the PC and use the PC's Tailscale address
(http://100.x.y.z:8765/ingest). Don't port-forward it to the open internet.

RUN
  set HEALTH_INGEST_TOKEN=<long random string>
  python ingest_server.py                 # 0.0.0.0:8765
  python ingest_server.py --port 9000
"""

import argparse
import hmac
import json
import os
import sys
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from health_store import HealthStore
from ingest import ingest_payload

MAX_BODY = 50 * 1024 * 1024  # a month of unaggregated samples fits easily


def make_handler(store, token):
    class Handler(BaseHTTPRequestHandler):
        def _reply(self, code, obj):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _authorized(self):
            given = ""
            auth = self.headers.get("Authorization", "")
            if auth.startswith("Bearer "):
                given = auth[7:].strip()
            else:
                given = (parse_qs(urlparse(self.path).query).get("token") or [""])[0]
            return bool(given) and hmac.compare_digest(given, token)

        def do_GET(self):
            if urlparse(self.path).path == "/ping":
                return self._reply(200, {"ok": True})
            self._reply(404, {"error": "not found"})

        def do_POST(self):
            if urlparse(self.path).path != "/ingest":
                return self._reply(404, {"error": "not found"})
            if not self._authorized():
                return self._reply(401, {"error": "bad or missing token"})
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0 or length > MAX_BODY:
                return self._reply(413 if length > MAX_BODY else 400,
                                   {"error": "body missing or too large"})
            try:
                payload = json.loads(self.rfile.read(length))
            except (ValueError, UnicodeDecodeError):
                return self._reply(400, {"error": "body is not JSON"})
            try:
                result = ingest_payload(store, payload)
            except Exception as e:  # never 500 silently — tell the phone why
                return self._reply(422, {"error": f"could not ingest: {e}"})
            print(f"[{datetime.now().isoformat(timespec='seconds')}] ingest {result}")
            self._reply(200, {"ok": True, **result})

        def log_message(self, *args):  # quiet: we print our own one-liners
            pass

    return Handler


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--host", default=os.environ.get("HEALTH_INGEST_HOST", "0.0.0.0"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("HEALTH_INGEST_PORT", "8765")))
    args = ap.parse_args()

    token = os.environ.get("HEALTH_INGEST_TOKEN", "")
    if len(token) < 16:
        sys.exit("Set HEALTH_INGEST_TOKEN to a random string of 16+ characters first.")

    server = ThreadingHTTPServer((args.host, args.port), make_handler(HealthStore(), token))
    print(f"Health ingest listening on http://{args.host}:{args.port}/ingest")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
