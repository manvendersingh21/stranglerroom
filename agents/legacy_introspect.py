"""Static (AST-only) route extraction from legacy web.py / infogami plugins.

This module analyses a legacy web.py plugin source file using only the
:mod:`ast` module — it never imports or executes the target code.  It
discovers classes that subclass ``delegate.page``, extracts their ``path``
regex and ``encoding`` attribute, and maps GET/POST handler methods to
:class:`LegacyRoute` descriptors carrying FastAPI-style path templates,
query parameter names, and other metadata.

Public API
----------
.. autoclass:: LegacyRoute
.. autofunction:: introspect_file
.. autofunction:: introspect_source
.. autofunction:: render_route_table

CLI
---
::

    python -m agents.legacy_introspect <file>

Prints the Markdown route table for the given legacy module.
"""
from __future__ import annotations

import ast
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence


@dataclass(frozen=True)
class LegacyRoute:
    """Descriptor for a single HTTP handler extracted from a legacy web.py plugin.

    Attributes:
        handler:    Class name, e.g. ``"lists_json"``.
        method:     HTTP method — ``"GET"`` or ``"POST"``.
        regex:      The class-level ``path`` literal (raw regex string).
        template:   FastAPI-style template derived from *regex* with capture
                    groups named from the method's positional params,
                    e.g. ``"/people/{user}/lists/{list_id}/seeds"``.
                    A trailing ``(?:\\.json)?`` suffix is stripped before
                    conversion.
        params:     Path parameter names in order (the method's positional
                    arguments after ``self``).
        query:      Keyword names passed to ``web.input(...)`` inside the
                    method body, in source order.
        encoding:   Class-level ``encoding`` literal, or ``None``.
        json_alias: ``True`` when *encoding* equals ``"json"`` (infogami
                    serves ``<path>.json`` too).
        reads_body: ``True`` when the method body calls ``web.data()``.
        lineno:     Source line number of the ``def`` statement.
    """

    handler: str
    method: str
    regex: str
    template: str
    params: tuple[str, ...]
    query: tuple[str, ...]
    encoding: str | None
    json_alias: bool
    reads_body: bool
    lineno: int


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

#: Suffix appended by infogami for ``encoding = "json"`` pages; stripped
#: before template conversion so the base path is clean.
_JSON_SUFFIX_RE = re.compile(r"\(\?:\\\.json\)\?$")

#: Matches capturing regex groups ``([^/]+)`` and ``(?P<name>...)`` (not ``(?:...)`` / lookarounds, not ``\(``).
_CAPTURE_GROUP_RE = re.compile(r"(?<!\\)\((?!\?(?!P<))[^)]*\)")


def _is_delegate_page_subclass(node: ast.ClassDef) -> bool:
    """Return ``True`` if *node* has a base that looks like ``delegate.page``.

    We check for ``delegate.page``, ``page``, or any ``X.page`` attribute
    access — enough for both ``from infogami_shim import delegate`` and
    ``from x import delegate, web`` import styles found in legacy code.
    """
    for base in node.bases:
        if isinstance(base, ast.Attribute) and base.attr == "page":
            return True
        if isinstance(base, ast.Name) and base.id == "page":
            return True
    return False


def _get_str_attr(node: ast.ClassDef, name: str) -> str | None:
    """Extract a simple string literal assigned to *name* at class level."""
    for item in node.body:
        if isinstance(item, ast.Assign):
            for target in item.targets:
                if isinstance(target, ast.Name) and target.id == name:
                    if isinstance(item.value, ast.Constant) and isinstance(
                        item.value.value, str
                    ):
                        return item.value.value
    return None


def _method_params(func: ast.FunctionDef) -> tuple[str, ...]:
    """Return the positional parameter names after ``self``."""
    args = func.args
    # posonlyargs + args, skip 'self'
    all_args = args.posonlyargs + args.args
    return tuple(a.arg for a in all_args[1:])


def _extract_web_input_keys(func: ast.FunctionDef) -> tuple[str, ...]:
    """Return keyword argument names from ``web.input(...)`` calls, in source order."""
    keys: list[str] = []
    for node in ast.walk(func):
        if not isinstance(node, ast.Call):
            continue
        # Match web.input(...)
        callee = node.func
        if (
            isinstance(callee, ast.Attribute)
            and callee.attr == "input"
            and isinstance(callee.value, ast.Name)
            and callee.value.id == "web"
        ):
            for kw in node.keywords:
                if kw.arg is not None and kw.arg not in keys:
                    keys.append(kw.arg)
    return tuple(keys)


def _calls_web_data(func: ast.FunctionDef) -> bool:
    """Return ``True`` if the function body contains a ``web.data()`` call."""
    for node in ast.walk(func):
        if not isinstance(node, ast.Call):
            continue
        callee = node.func
        if (
            isinstance(callee, ast.Attribute)
            and callee.attr == "data"
            and isinstance(callee.value, ast.Name)
            and callee.value.id == "web"
        ):
            return True
    return False


def _regex_to_template(regex: str, params: tuple[str, ...]) -> str:
    """Convert a web.py path regex to a FastAPI-style path template.

    Capture groups are replaced with ``{param}`` using the names from
    *params* in order.  A trailing ``(?:\\.json)?`` suffix is stripped first.
    """
    # Strip trailing optional .json suffix
    clean = _JSON_SUFFIX_RE.sub("", regex)

    idx = 0

    def _replace(match: re.Match[str]) -> str:
        nonlocal idx
        if idx < len(params):
            name = params[idx]
            idx += 1
            return "{" + name + "}"
        return match.group(0)  # pragma: no cover — safety fallback

    return _CAPTURE_GROUP_RE.sub(_replace, clean)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def introspect_source(source: str) -> list[LegacyRoute]:
    """Extract :class:`LegacyRoute` descriptors from *source* code.

    Parameters:
        source: Python source text of a legacy web.py plugin module.

    Returns:
        A list of routes sorted by ``lineno``.

    Raises:
        SyntaxError: If *source* cannot be parsed by :func:`ast.parse`.
    """
    if not source.strip():
        return []

    tree = ast.parse(source)
    routes: list[LegacyRoute] = []

    for node in ast.iter_child_nodes(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        if not _is_delegate_page_subclass(node):
            continue

        path = _get_str_attr(node, "path")
        if path is None:
            continue

        encoding = _get_str_attr(node, "encoding")
        json_alias = encoding == "json"

        # Walk methods
        for item in node.body:
            if not isinstance(item, ast.FunctionDef):
                continue
            if item.name not in ("GET", "POST"):
                continue

            method = item.name
            params = _method_params(item)
            template = _regex_to_template(path, params)
            query = _extract_web_input_keys(item)
            reads_body = _calls_web_data(item)

            routes.append(
                LegacyRoute(
                    handler=node.name,
                    method=method,
                    regex=path,
                    template=template,
                    params=params,
                    query=query,
                    encoding=encoding,
                    json_alias=json_alias,
                    reads_body=reads_body,
                    lineno=item.lineno,
                )
            )

    routes.sort(key=lambda r: r.lineno)
    return routes


def introspect_file(path: str | Path) -> list[LegacyRoute]:
    """Extract routes from a legacy web.py plugin module on disk.

    Parameters:
        path: Filesystem path to the ``.py`` source file.

    Returns:
        A list of routes sorted by ``lineno``.

    Raises:
        SyntaxError: If the file cannot be parsed.
        FileNotFoundError: If *path* does not exist.
    """
    source = Path(path).read_text(encoding="utf-8")
    return introspect_source(source)


def render_route_table(routes: Sequence[LegacyRoute]) -> str:
    """Render a Markdown table summarising the given routes.

    Columns: Handler, Method, Route, Query, ``.json`` alias.

    Parameters:
        routes: Iterable of :class:`LegacyRoute` instances.

    Returns:
        A Markdown-formatted table string.
    """
    header = "| Handler | Method | Route | Query | .json alias |"
    separator = "| --- | --- | --- | --- | --- |"
    rows: list[str] = []
    for r in routes:
        query_str = ", ".join(r.query) if r.query else "—"
        alias = "✓" if r.json_alias else "✗"
        rows.append(
            f"| `{r.handler}` | {r.method} | `{r.template}` | {query_str} | {alias} |"
        )
    return "\n".join([header, separator, *rows]) + "\n"


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """CLI: ``python -m agents.legacy_introspect <file>`` prints the route table."""
    if len(sys.argv) != 2:
        print(f"Usage: python -m agents.legacy_introspect <file>", file=sys.stderr)
        sys.exit(1)

    filepath = sys.argv[1]
    try:
        routes = introspect_file(filepath)
    except FileNotFoundError:
        print(f"Error: file not found: {filepath}", file=sys.stderr)
        sys.exit(1)
    except SyntaxError as exc:
        print(f"Error: syntax error in {filepath}: {exc}", file=sys.stderr)
        sys.exit(1)

    print(render_route_table(routes))


if __name__ == "__main__":
    main()
