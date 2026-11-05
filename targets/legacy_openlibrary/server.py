"""Run the legacy Open Library (web.py) target on a cheroot WSGI server.

Usage::

    python -m targets.legacy_openlibrary.server --port 9001

This mirrors ``web.httpserver.runsimple``: the infogami-style ``web.application``
is served by cheroot (web.py's own HTTP server dependency) with 30 worker threads.
One :class:`~targets.common.dataset.Store` is created per process and shared by
all threads.  SIGTERM / SIGINT stop the server cleanly.

Request tracebacks that web.py writes to ``wsgi.errors`` (e.g. the D-5 crash) are
sent to ``--log-file`` (default ``<STRANGLER_STATE_DIR or .state>/legacy/legacy_openlibrary.log``;
``-`` for stderr) so that a parent process that never drains our stderr pipe cannot stall us.
"""
from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import threading
from pathlib import Path

from cheroot import wsgi

from targets.common.dataset import Store
from targets.legacy_openlibrary.infogami_shim import build_app

ROOT = Path(__file__).resolve().parents[2]


def default_log_file() -> Path:
    """``<STRANGLER_STATE_DIR or <repo>/.state>/legacy/legacy_openlibrary.log``, resolved at call time
    (the legacy target's private state directory)."""
    state_dir = (os.environ.get("STRANGLER_STATE_DIR") or "").strip() or str(ROOT / ".state")
    return Path(state_dir).expanduser() / "legacy" / "legacy_openlibrary.log"


log = logging.getLogger("legacy_openlibrary")


class _LogStream:
    """File-like ``wsgi.errors`` replacement that forwards writes to a logger."""

    def __init__(self, logger: logging.Logger) -> None:
        self._logger = logger

    def write(self, text: str) -> int:
        text = text.rstrip("\n")
        if text:
            self._logger.error(text)
        return len(text)

    def writelines(self, lines) -> None:
        for line in lines:
            self.write(line)

    def flush(self) -> None:
        pass


def with_error_log(app, stream: _LogStream):
    """WSGI middleware pointing ``wsgi.errors`` at ``stream``."""

    def middleware(environ, start_response):
        environ["wsgi.errors"] = stream
        return app(environ, start_response)

    return middleware


def configure_logging(log_file: str) -> None:
    """Send this process's logs to ``log_file`` (``-`` means stderr)."""
    if log_file == "-":
        handler: logging.Handler = logging.StreamHandler(sys.stderr)
    else:
        path = Path(log_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s"))
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)


def main(argv: list[str] | None = None) -> None:
    """Parse arguments and serve until SIGTERM/SIGINT."""
    parser = argparse.ArgumentParser(description="Legacy Open Library web.py target")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9001)
    parser.add_argument("--log-file", default=None,
                        help="where request errors are logged ('-' for stderr; default "
                             "<STRANGLER_STATE_DIR>/legacy/legacy_openlibrary.log)")
    args = parser.parse_args(argv)

    configure_logging(args.log_file if args.log_file is not None else str(default_log_file()))
    app = build_app(Store())
    wsgi_app = with_error_log(app.wsgifunc(), _LogStream(logging.getLogger("legacy_openlibrary.wsgi")))
    server = wsgi.Server((args.host, args.port), wsgi_app, numthreads=30)

    stop = threading.Event()

    def request_stop(signum, _frame):
        log.info("received signal %s, shutting down", signum)
        stop.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)

    server.prepare()
    worker = threading.Thread(target=server.serve, name="cheroot-serve", daemon=True)
    worker.start()
    log.info("legacy Open Library (web.py) listening on http://%s:%d", args.host, args.port)
    print(f"legacy Open Library (web.py) listening on http://{args.host}:{args.port}", file=sys.stderr, flush=True)
    try:
        while not stop.wait(0.5):
            if not worker.is_alive():
                break
    finally:
        server.stop()
        worker.join(timeout=5)
        log.info("stopped")


if __name__ == "__main__":
    main()
