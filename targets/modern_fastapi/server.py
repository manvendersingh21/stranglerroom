"""Entry point for the modern FastAPI target (docs/SPEC.md §0, §4).

Usage::

    python -m targets.modern_fastapi.server --port 9002

Runs uvicorn with ``reload=True`` watching ``targets/modern_fastapi/app`` so that a
freshly generated ``app/lists.py`` is picked up automatically.  Set
``MODERN_NO_RELOAD=1`` (used by tests) to run a single process without the reloader, and
``MODERN_LISTS_PATH=/some/dir/lists.py`` to serve (and watch) a generated module outside the repo.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import uvicorn

APP_IMPORT = "targets.modern_fastapi.app.main:app"
APP_DIR = Path(__file__).resolve().parent / "app"
ROOT = Path(__file__).resolve().parents[2]


def main(argv: list[str] | None = None) -> None:
    """Parse CLI arguments and serve the app."""
    parser = argparse.ArgumentParser(description="Modern FastAPI Open Library target")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9002)
    args = parser.parse_args(argv)

    reload = os.environ.get("MODERN_NO_RELOAD", "") != "1"
    override = os.environ.get("MODERN_LISTS_PATH")
    watch_dir = Path(override).resolve().parent if override else APP_DIR
    options: dict = {"host": args.host, "port": args.port, "reload": reload, "app_dir": str(ROOT),
                     "log_level": "info", "access_log": False}
    if reload:
        options["reload_dirs"] = [str(watch_dir)]
    # uvicorn's WatchFiles reloader always adds the *current directory* to the watched
    # set (and drops reload_dirs nested inside it), so run from the app directory:
    # otherwise any .py edit anywhere in the repo would restart us and wipe the store.
    # The import string still resolves because the repo root is on sys.path (app_dir).
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    os.chdir(watch_dir)
    uvicorn.run(APP_IMPORT, **options)


if __name__ == "__main__":
    main()
