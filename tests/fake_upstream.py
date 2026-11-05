"""Tiny echo upstream used by proxy tests: `python -m tests.fake_upstream --port N --name legacy`."""
import argparse
import gzip
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit


def make_handler(name: str):
    class H(BaseHTTPRequestHandler):
        def _reply(self):
            n = int(self.headers.get("content-length") or 0)
            body = self.rfile.read(n).decode() if n else ""
            u = urlsplit(self.path)
            status = 200
            if u.path.startswith("/echo/status/"):
                status = int(u.path.rsplit("/", 1)[1])
            payload = json.dumps({"served_by": name, "method": self.command, "path": u.path, "query": u.query,
                                  "body": body, "cookie": self.headers.get("Cookie"),
                                  "probe": self.headers.get("X-Probe")}).encode()
            extra = []
            if u.path == "/echo/gzip":
                payload = gzip.compress(payload)
                extra.append(("Content-Encoding", "gzip"))
            if u.path == "/echo/setcookie":
                extra.append(("Set-Cookie", "session=secret-of-first-caller; Path=/"))
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            for k, v in extra:
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(payload)

        do_GET = do_POST = do_PUT = do_DELETE = _reply

        def log_message(self, *a):
            pass

    return H


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--name", default="upstream")
    a = ap.parse_args()
    ThreadingHTTPServer(("127.0.0.1", a.port), make_handler(a.name)).serve_forever()
