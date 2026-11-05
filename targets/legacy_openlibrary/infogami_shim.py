"""A tiny stand-in for the parts of infogami that Open Library plugins rely on.

Real Open Library plugins are written against ``infogami.utils.delegate``::

    from infogami.utils import delegate

    class lists_json(delegate.page):
        path = r"/people/([^/]+)/lists"
        encoding = "json"

        def GET(self, user):
            ...

Subclassing ``delegate.page`` registers the class; infogami then mounts every
registered page on its ``path`` regex.  Pages that declare ``encoding = "json"``
are *also* reachable with a ``.json`` suffix -- this implicit alias is quirk D-1.
Data access goes through ``web.ctx.site`` (here: a :class:`~targets.common.dataset.Store`).

This module reproduces exactly that surface, nothing more:

* :data:`delegate` -- namespace exposing :class:`page` and the ``pages`` registry;
* :func:`build_app` -- loads the plugins and returns a ``web.application``.
"""
from __future__ import annotations

import importlib
import json
import types

import web

from targets.common.dataset import Store

__all__ = ["delegate", "page", "pages", "build_app", "PLUGINS"]

# web.py's stock 500 handler (``web._InternalError``: text/html "internal server error")
# must stay in effect -- legacy clients have learned to live with it (quirk D-5).
web.config.debug = False

#: Plugin modules imported by :func:`build_app`; importing them registers their pages.
PLUGINS = ("targets.legacy_openlibrary.openlibrary.plugins.openlibrary.lists",)

#: Registry of page classes, in definition order: ``{class name: class}``.
pages: dict[str, type] = {}


class _metapage(type):
    """Metaclass that registers every ``delegate.page`` subclass declaring a ``path``."""

    def __init__(cls, name, bases, attrs):
        super().__init__(name, bases, attrs)
        if attrs.get("path"):
            pages[name] = cls


class page(metaclass=_metapage):
    """Base class for infogami pages.

    Subclasses set ``path`` (a regex; capture groups become handler arguments) and
    optionally ``encoding = "json"`` to get the implicit ``.json`` alias, then
    implement ``GET`` / ``POST`` methods in the usual web.py style.
    """

    path: str | None = None
    encoding: str | None = None


#: Mimics the ``infogami.utils.delegate`` module.
delegate = types.SimpleNamespace(page=page, pages=pages)


def url_pattern(cls: type) -> str:
    """Return the web.py URL pattern under which ``cls`` is mounted."""
    if cls.encoding == "json":
        return cls.path + r"(?:\.json)?"
    return cls.path


class _health:
    """``GET /health`` -- liveness probe used by tests and the proxy."""

    def GET(self):
        web.header("Content-Type", "application/json")
        return json.dumps({"status": "ok", "stack": "web.py"})


class _admin_reset:
    """``POST /_admin/reset`` -- restore the pristine seed data (test/fuzzer hook)."""

    def POST(self):
        web.ctx.site.reset()
        web.header("Content-Type", "application/json")
        return json.dumps({"reset": True})


def build_app(store: Store | None = None) -> web.application:
    """Load the plugins and build the infogami-style ``web.application``.

    Args:
        store: the per-process data store exposed to handlers as ``web.ctx.site``;
            a fresh :class:`Store` is created when omitted.
    """
    site = store if store is not None else Store()
    for plugin in PLUGINS:
        importlib.import_module(plugin)

    mapping: list = ["/health", _health, "/_admin/reset", _admin_reset]
    for cls in pages.values():
        mapping += [url_pattern(cls), cls]

    app = web.application(mapping, {}, autoreload=False)

    def attach_site(handler):
        web.ctx.site = site
        return handler()

    app.add_processor(attach_site)
    return app
