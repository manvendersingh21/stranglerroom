"""Generate the modern target's ``app/lists.py`` (FastAPI port of the legacy lists plugin).

@translator calls :func:`render_lists_module` with a *quirk policy* -- a mapping from
the quirk tags of docs/SPEC.md §3 to ``"preserve"`` or ``"deprecate"`` -- and writes the
returned source to ``targets/modern_fastapi/app/lists.py``.  uvicorn's reloader picks the
file up and ``GET /_meta`` reports :func:`module_rev` of its bytes.

Semantics of the policy:

* A tag set to ``"preserve"`` makes the port reproduce the legacy (web.py) behaviour
  for that quirk exactly.
* A tag set to ``"deprecate"`` -- or missing from the policy -- keeps the "textbook"
  FastAPI v1 behaviour from the *Modern v1 (naive)* column of SPEC §3, which then
  becomes the approved contract.

Everything that is not one of the five quirks is identical to legacy in every policy
(JSON keys/values/floats, status codes, ``Content-Type: application/json``).

The output is deterministic: the same policy (and header comment) always yields
byte-identical source, so its revision hash is stable.
"""
from __future__ import annotations

import hashlib
import textwrap
from collections.abc import Mapping

__all__ = ["QUIRK_TAGS", "PRESERVE", "DEPRECATE", "render_lists_module", "module_rev"]

#: The five legacy quirks, in SPEC §3 order (D-1 .. D-5).
QUIRK_TAGS: tuple[str, ...] = (
    "json_suffix",
    "loose_query",
    "missing_resource_200",
    "non_positive_quantity",
    "empty_body_500",
)

PRESERVE = "preserve"
DEPRECATE = "deprecate"
_DECISIONS = (PRESERVE, DEPRECATE)


def module_rev(source: str) -> str:
    """Return the revision id of a generated module: ``sha256(source.encode())[:12]``.

    This equals the ``rev`` that ``GET /_meta`` on the modern target reports once
    ``source`` has been written (UTF-8, unmodified) to ``app/lists.py``.
    """
    return hashlib.sha256(source.encode()).hexdigest()[:12]


def _normalise_policy(policy: Mapping[str, str] | None) -> dict[str, str | None]:
    """Validate ``policy`` and return ``{tag: "preserve" | "deprecate" | None}`` for every tag."""
    policy = dict(policy or {})
    unknown = sorted(set(policy) - set(QUIRK_TAGS))
    if unknown:
        raise ValueError(f"unknown quirk tag(s): {', '.join(unknown)}; expected one of {QUIRK_TAGS}")
    out: dict[str, str | None] = {}
    for tag in QUIRK_TAGS:
        decision = policy.get(tag)
        if decision is not None:
            decision = str(decision).strip().lower()
            if decision not in _DECISIONS:
                raise ValueError(f"quirk {tag!r}: decision must be 'preserve' or 'deprecate', got {policy[tag]!r}")
        out[tag] = decision
    return out


def _docstring_literal(text: str) -> str:
    """Escape ``text`` so it can sit inside a triple-double-quoted string literal."""
    return text.replace("\\", "\\\\").replace('"""', '\\"\\"\\"')


def _block(code: str) -> str:
    """Dedent a code fragment and strip its surrounding blank lines."""
    return textwrap.dedent(code).strip("\n")


def _body(code: str) -> str:
    """Dedent a code fragment and indent it one level, as a function body."""
    return textwrap.indent(_block(code), "    ")


class _Renderer:
    """Assemble the module source for one normalised policy."""

    def __init__(self, decisions: dict[str, str | None], header_comment: str) -> None:
        self.decisions = decisions
        self.header_comment = header_comment
        self.json_suffix = decisions["json_suffix"] == PRESERVE
        self.loose_query = decisions["loose_query"] == PRESERVE
        self.missing_200 = decisions["missing_resource_200"] == PRESERVE
        self.non_positive = decisions["non_positive_quantity"] == PRESERVE
        self.empty_500 = decisions["empty_body_500"] == PRESERVE

    # ------------------------------------------------------------------ helpers

    def _routes(self, method: str, path: str) -> str:
        """Decorator lines for ``path`` (plus its ``.json`` alias when D-1 is preserved)."""
        lines = [f'@router.{method}("{path}")']
        if self.json_suffix:
            lines.append(f'@router.{method}("{path}.json", include_in_schema=False)')
        return "\n".join(lines)

    # ------------------------------------------------------------------ sections

    def header(self) -> str:
        rows = []
        for tag in QUIRK_TAGS:
            decision = self.decisions[tag] or "unset (naive v1 behaviour)"
            rows.append(f"    {tag:<23} {decision}")
        text = "\n".join([
            "Open Library lists API -- FastAPI port of openlibrary/plugins/openlibrary/lists.py.",
            "",
            "GENERATED FILE -- do not edit by hand.  Regenerate with",
            "agents.codegen.fastapi_lists.render_lists_module(policy).",
            "",
            "Endpoints (docs/SPEC.md section 2): E1 lists_json, E2/E3 list_seeds GET/POST,",
            "E4 list_editions_json, E5 edition_quote_json.",
            "",
            "Quirk policy (docs/SPEC.md section 3; unset behaves like deprecate):",
            *rows,
        ])
        if self.header_comment.strip():
            text += "\n\n" + self.header_comment.strip("\n")
        return f'"""{_docstring_literal(text)}\n"""'

    def imports(self) -> str:
        responses = "HTMLResponse, JSONResponse" if self.empty_500 else "JSONResponse"
        fastapi_names = "APIRouter" if self.loose_query or self.non_positive else "APIRouter, Query"
        return _block(f"""
            import re

            from fastapi import {fastapi_names}
            from fastapi.responses import {responses}
            from pydantic import BaseModel

            from targets.modern_fastapi.app.store import STORE
            """)

    def constants(self) -> str:
        def literal(decision: str | None) -> str:
            return "None" if decision is None else f'"{decision}"'

        policy_lines = "\n".join(f'    "{tag}": {literal(self.decisions[tag])},' for tag in QUIRK_TAGS)
        return "\n".join([
            "#: The quirk policy this module was generated from (None = unset, naive behaviour).",
            "POLICY = {",
            policy_lines,
            "}",
            "",
            "router = APIRouter()",
            "",
            "#: Seed key prefix -> seed ``type`` reported by the seeds endpoint.",
            'SEED_TYPES = {"/works/": "work", "/books/": "edition", "/authors/": "author", "/subjects/": "subject"}',
            "",
            "#: Inclusive quantity range accepted by the legacy quote endpoint.",
            "QUANTITY_MIN = -10",
            "QUANTITY_MAX = 100",
            "",
            '_NON_ALNUM = re.compile(r"[^a-z0-9]+")',
        ])

    def common_helpers(self) -> list[str]:
        missing_status = 200 if self.missing_200 else 404
        missing_doc = (
            "Legacy quirk D-3 (preserved): unknown user/list answers 200 with an error body."
            if self.missing_200 else
            "D-3 deprecated: unknown user/list is a real 404 (same JSON body as legacy)."
        )
        blocks = [
            _block('''
                class SeedChanges(BaseModel):
                    """POST body of the seeds endpoint; both fields optional so ``{}`` parses."""

                    add: list[str] = []
                    remove: list[str] = []
                '''),
            _block('''
                def json_response(body: dict, status_code: int = 200) -> JSONResponse:
                    """Serialise ``body`` as ``application/json`` with the given status."""
                    return JSONResponse(body, status_code=status_code)
                '''),
            _block('''
                def slug(name: str) -> str:
                    """Lowercase ``name``, collapse non-alphanumeric runs to ``-`` and strip ``-``."""
                    return _NON_ALNUM.sub("-", name.lower()).strip("-")
                '''),
            _block('''
                def seed_type(seed: str) -> str | None:
                    """Return the seed type for a valid seed key, else None."""
                    for prefix, kind in SEED_TYPES.items():
                        if seed.startswith(prefix):
                            return kind
                    return None
                '''),
            _block('''
                def page(items: list, offset: int, limit: int | None) -> list:
                    """Legacy paging: plain Python slicing, ``limit=None`` means unlimited."""
                    return items[offset:(offset + limit) if limit is not None else None]
                '''),
            _block(f'''
                def missing_resource() -> JSONResponse:
                    """Response for an unknown user or list on a per-list endpoint.

                    {missing_doc}
                    """
                    return json_response({{"error": "not_found", "entries": []}}, {missing_status})
                '''),
        ]
        if self.loose_query:
            blocks += [
                _block('''
                    def legacy_int(value: str | None, default: int | None) -> int | None:
                        """Legacy D-2 integer coercion: ``none``/``all`` -> None (unlimited), junk -> default."""
                        if value is None:
                            return default
                        if value.strip().lower() in ("none", "all"):
                            return None
                        try:
                            return int(value.strip())
                        except ValueError:
                            return default
                    '''),
                _block('''
                    def legacy_bool(value: str | None) -> bool:
                        """Legacy D-2 boolean coercion: anything but an explicit falsy word is True."""
                        if value is None:
                            return False
                        return value.strip().lower() not in {"", "0", "false", "no", "off", "n", "f", "none"}
                    '''),
            ]
            if not self.non_positive:
                blocks.append(_block('''
                    def positive_quantity_error(raw: str | None) -> JSONResponse:
                        """FastAPI-shaped 422 for a non-positive quantity (D-4 deprecated)."""
                        detail = {"type": "greater_than", "loc": ["query", "quantity"],
                                  "msg": "Input should be greater than 0", "input": raw, "ctx": {"gt": 0}}
                        return json_response({"detail": [detail]}, 422)
                    '''))
        return blocks

    def e1_lists_json(self) -> str:
        if self.loose_query:
            signature = "user: str, limit: str | None = None, offset: str | None = None, details: str | None = None"
            coerce = _block('''
                    limit_n = legacy_int(limit, 100)
                    offset_n = legacy_int(offset, 0)
                    if offset_n is None:
                        offset_n = 0
                    show_details = legacy_bool(details)
                ''')
        else:
            signature = "user: str, limit: int = 100, offset: int = 0, details: bool = False"
            coerce = "limit_n, offset_n, show_details = limit, offset, details"
        return "\n".join([
            self._routes("get", "/people/{user}/lists"),
            f"async def lists_json({signature}) -> JSONResponse:",
            '    """E1: all lists of ``user``."""',
            textwrap.indent(coerce, "    "),
            _body('''
                    if not STORE.user_exists(user):
                        return json_response({"error": "not_found", "key": f"/people/{user}"}, 404)
                    lists = STORE.user_lists(user)
                    entries = []
                    for lst in lists:
                        entry = {
                            "url": lst["key"],
                            "full_url": lst["key"] + "/" + slug(lst["name"]),
                            "name": lst["name"],
                            "seed_count": len(lst["seeds"]),
                            "last_update": lst["last_update"],
                        }
                        if show_details:
                            entry["description"] = lst["description"]
                            entry["seeds"] = lst["seeds"]
                        entries.append(entry)
                    return json_response({
                        "links": {"self": f"/people/{user}/lists.json", "user": f"/people/{user}"},
                        "size": len(lists),
                        "entries": page(entries, offset_n, limit_n),
                    })
            '''),
        ])

    def e2_list_seeds_get(self) -> str:
        return "\n".join([
            self._routes("get", "/people/{user}/lists/{list_id}/seeds"),
            _block('''
                async def list_seeds_get(user: str, list_id: str) -> JSONResponse:
                    """E2: the seeds of one list."""
                    lst = STORE.get_list(user, list_id)
                    if lst is None:
                        return missing_resource()
                    key = lst["key"]
                    return json_response({
                        "links": {"self": key + "/seeds.json", "list": key},
                        "size": len(lst["seeds"]),
                        "entries": [{"url": seed, "type": seed_type(seed)} for seed in lst["seeds"]],
                    })
                '''),
        ])

    def e3_list_seeds_post(self) -> str:
        if self.empty_500:
            empty = _block('''
                        # Legacy quirk D-5 (preserved): the legacy analytics line divides by
                        # len(add + remove) and web.py answers its default HTML 500.
                        return HTMLResponse("internal server error", status_code=500)
                ''')
        else:
            empty = _block('''
                        # D-5 deprecated: an empty change set is a validation error.
                        return json_response({"detail": "add or remove must contain at least one seed"}, 422)
                ''')
        return "\n".join([
            self._routes("post", "/people/{user}/lists/{list_id}/seeds"),
            _block('''
                async def list_seeds_post(user: str, list_id: str, changes: SeedChanges | None = None) -> JSONResponse:
                    """E3: add/remove seeds.  Check order mirrors legacy: missing list, invalid seed, empty body."""
                    lst = STORE.get_list(user, list_id)
                    if lst is None:
                        return missing_resource()
                    changes = changes if changes is not None else SeedChanges()
                    for seed in changes.add + changes.remove:
                        if seed_type(seed) is None:
                            return json_response({"error": "invalid_seed", "seed": seed}, 400)
                    if not changes.add and not changes.remove:
                '''),
            textwrap.indent(empty, "        "),
            _body('''
                    before = lst["seeds"]
                    updated = STORE.apply_seed_changes(user, list_id, changes.add, changes.remove)
                    after = updated["seeds"]
                    return json_response({
                        "success": True,
                        "size": len(after),
                        "added": sum(1 for seed in after if seed not in before),
                        "removed": sum(1 for seed in before if seed not in after),
                    })
            '''),
        ])

    def e4_list_editions_json(self) -> str:
        if self.loose_query:
            signature = "user: str, list_id: str, limit: str | None = None, offset: str | None = None"
            coerce = _block('''
                    limit_n = legacy_int(limit, 50)
                    offset_n = legacy_int(offset, 0)
                    if offset_n is None:
                        offset_n = 0
                ''')
        else:
            signature = "user: str, list_id: str, limit: int = 50, offset: int = 0"
            coerce = "limit_n, offset_n = limit, offset"
        return "\n".join([
            self._routes("get", "/people/{user}/lists/{list_id}/editions"),
            f"async def list_editions_json({signature}) -> JSONResponse:",
            '    """E4: edition records (without price) for the list\'s ``/books/`` seeds."""',
            textwrap.indent(coerce, "    "),
            _body('''
                    lst = STORE.get_list(user, list_id)
                    if lst is None:
                        return missing_resource()
                    key = lst["key"]
                    # Like legacy: page the edition *keys*, then skip keys without a record.
                    edition_keys = [seed for seed in lst["seeds"] if seed.startswith("/books/")]
                    entries = []
                    for seed in page(edition_keys, offset_n, limit_n):
                        edition = STORE.get_edition(seed.split("/")[2])
                        if edition is not None:
                            edition.pop("price", None)
                            entries.append(edition)
                    return json_response({
                        "links": {"self": key + "/editions.json", "list": key},
                        "size": len(edition_keys),
                        "entries": entries,
                    })
            '''),
        ])

    def e5_edition_quote_json(self) -> str:
        if self.loose_query:
            signature = "olid: str, quantity: str | None = None"
            coerce_lines = [
                "qty = legacy_int(quantity, 1)",
                "if qty is None:",
                "    qty = 1",
            ]
            if not self.non_positive:
                coerce_lines += [
                    "if qty <= 0:",
                    "    return positive_quantity_error(quantity)",
                ]
            coerce = "\n".join(coerce_lines)
        elif self.non_positive:
            signature = "olid: str, quantity: int = 1"
            coerce = "qty = quantity"
        else:
            signature = "olid: str, quantity: int = Query(1, gt=0)"
            coerce = "qty = quantity"
        doc = ("E5: price quote; legacy quirk D-4 (preserved) allows quantities -10..0."
               if self.non_positive else
               "E5: price quote; D-4 deprecated, quantity must be a positive integer.")
        return "\n".join([
            self._routes("get", "/books/{olid}/quote"),
            f"async def edition_quote_json({signature}) -> JSONResponse:",
            f'    """{doc}"""',
            textwrap.indent(coerce, "    "),
            _body('''
                    edition = STORE.get_edition(olid)
                    if edition is None:
                        return json_response({"error": "not_found", "key": f"/books/{olid}"}, 404)
                    if not QUANTITY_MIN <= qty <= QUANTITY_MAX:
                        return json_response({"error": "quantity_out_of_range", "min": QUANTITY_MIN, "max": QUANTITY_MAX}, 400)
                    price = edition["price"]
                    return json_response({
                        "key": f"/books/{olid}",
                        "title": edition["title"],
                        "unit_price": price,
                        "quantity": qty,
                        "total": round(price * qty, 2),
                        "currency": "USD",
                        "type": "credit_reversal" if qty < 0 else ("noop" if qty == 0 else "sale"),
                    })
            '''),
        ])

    def render(self) -> str:
        preamble = self.header() + "\n\n" + self.imports()
        sections = [
            preamble,
            self.constants(),
            *self.common_helpers(),
            self.e1_lists_json(),
            self.e2_list_seeds_get(),
            self.e3_list_seeds_post(),
            self.e4_list_editions_json(),
            self.e5_edition_quote_json(),
        ]
        return "\n\n\n".join(sections) + "\n"


def render_lists_module(policy: dict[str, str] | None = None, *, header_comment: str = "") -> str:
    """Return the full source of the modern target's ``app/lists.py``.

    Args:
        policy: quirk tag (see :data:`QUIRK_TAGS`) -> ``"preserve"`` | ``"deprecate"``.
            Missing tags (or ``None``) get the naive FastAPI v1 behaviour, which is the
            same as ``"deprecate"``.
        header_comment: free text appended to the module docstring (e.g. who approved
            the policy and when).  It is part of the source, hence of the revision.

    Returns:
        Deterministic, self-contained Python source defining ``router = APIRouter()``.

    Raises:
        ValueError: on an unknown tag or a decision other than preserve/deprecate.
    """
    return _Renderer(_normalise_policy(policy), header_comment).render()
