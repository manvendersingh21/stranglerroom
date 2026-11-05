"""Hand-written FastAPI application shell for the modern target (docs/SPEC.md §4).

The business endpoints live in ``app/lists.py``, which is *generated* by @translator
(see :mod:`agents.codegen.fastapi_lists`) and absent from a fresh checkout.  This module
includes that router when the file exists and imports cleanly; otherwise the app still
starts and reports the problem on ``GET /_meta`` so the fuzzer can tell which revision
(if any) is being served.

Static endpoints:

* ``GET /health``        -> ``{"status": "ok"}``
* ``GET /_meta``         -> ``{"rev": <sha256(lists.py)[:12]> | null, "routes": [...], "import_error"?: str}``
* ``POST /_admin/reset`` -> resets the in-process store, ``{"reset": true}``
"""
from __future__ import annotations

import hashlib
import os
import sys
import types
from pathlib import Path

from fastapi import FastAPI
from fastapi.routing import APIRoute

from targets.modern_fastapi.app.store import STORE

APP_DIR = Path(__file__).resolve().parent
#: The generated module. ``MODERN_LISTS_PATH`` points the app at another file so tests (and parallel
#: runs) never touch the real ``app/lists.py`` that the live demo's translator writes.
LISTS_PATH = Path(os.environ["MODERN_LISTS_PATH"]) if os.environ.get("MODERN_LISTS_PATH") else APP_DIR / "lists.py"
LISTS_MODULE = "targets.modern_fastapi.app.lists"

app = FastAPI(title="Open Library lists (modern)", version="1")


def _load_lists_module() -> tuple[types.ModuleType | None, str | None, str | None]:
    """Load the generated ``lists.py`` from the exact bytes that are hashed.

    Reading the file once and executing *those* bytes guarantees that the reported
    ``rev`` always identifies the code actually serving requests, even if the file is
    rewritten while this process starts (uvicorn's reloader restarts us afterwards).

    Returns:
        ``(module, rev, import_error)``; ``module`` is None when the file is absent or
        failed to import, ``rev`` is None only when the file is absent.
    """
    try:
        source = LISTS_PATH.read_bytes()
    except FileNotFoundError:
        return None, None, None
    rev = hashlib.sha256(source).hexdigest()[:12]
    module = types.ModuleType(LISTS_MODULE)
    module.__file__ = str(LISTS_PATH)
    module.__package__ = LISTS_MODULE.rpartition(".")[0]
    sys.modules[LISTS_MODULE] = module
    try:
        exec(compile(source, str(LISTS_PATH), "exec"), module.__dict__)  # noqa: S102 - our own generated code
        if not hasattr(module, "router"):
            raise ImportError("lists.py does not define 'router'")
    except Exception as exc:  # noqa: BLE001 - any failure must surface on /_meta, never crash the app
        sys.modules.pop(LISTS_MODULE, None)
        return None, rev, f"{type(exc).__name__}: {exc}"
    return module, rev, None


_lists_module, LISTS_REV, LISTS_IMPORT_ERROR = _load_lists_module()
if _lists_module is not None:
    try:
        app.include_router(_lists_module.router)
    except Exception as exc:  # noqa: BLE001 - e.g. 'router' is not an APIRouter
        LISTS_IMPORT_ERROR = f"{type(exc).__name__}: {exc}"


def _public_routes() -> list[str]:
    """Return sorted ``"METHOD path"`` strings for every non-underscore API route."""
    out = set()
    for route in app.routes:
        if isinstance(route, APIRoute) and not route.path.startswith("/_"):
            out.update(f"{method} {route.path}" for method in route.methods)
    return sorted(out)


@app.get("/health")
async def health() -> dict:
    """Liveness probe."""
    return {"status": "ok"}


@app.get("/_meta")
async def meta() -> dict:
    """Report the served ``lists.py`` revision, the public route table and any import error."""
    body: dict = {"rev": LISTS_REV, "routes": _public_routes()}
    if LISTS_IMPORT_ERROR is not None:
        body["import_error"] = LISTS_IMPORT_ERROR
    return body


@app.post("/_admin/reset")
async def admin_reset() -> dict:
    """Restore the pristine seed dataset for this process."""
    STORE.reset()
    return {"reset": True}
