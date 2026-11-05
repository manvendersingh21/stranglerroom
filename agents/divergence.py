"""Divergence engine (fuzzer core) -- see ``docs/SPEC.md`` section 7.

The engine shadow-tests a *modern* rewrite against the *legacy* system that is the
ground truth.  Every :class:`Probe` is sent to both targets; the two
:class:`Observation` s are compared with :func:`compare`, and each divergence is
tagged by :func:`classify` using request/response *features* only (never a probe's
label or a hard-coded ID).  Tags are mapped to stable human-facing cluster IDs
(``D-1``, ``D-2``, ...) by a persistent :class:`Registry`, numbered in order of first
appearance.  Because :func:`deterministic_corpus` is ordered like the quirk table in
SPEC section 3, the five known quirks receive ``D-1`` .. ``D-5`` and anything new gets
``D-6`` onwards.

:func:`run_campaign` runs the full campaign (deterministic corpus, Hypothesis-generated
GETs fired concurrently, POSTs sequentially with a state reset before each), folds in
the human decisions (``preserve`` / ``deprecate``) and returns a
:class:`CampaignReport` whose :meth:`CampaignReport.to_markdown` table is posted to
the room.

CLI::

    python -m agents.divergence --legacy http://127.0.0.1:9001 --modern http://127.0.0.1:9002 \\
        [--decisions '{"D-1": {"decision": "preserve"}}']
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import re
import tempfile
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Sequence
from urllib.parse import urlencode

import httpx
from hypothesis import HealthCheck, Phase, given, settings
from hypothesis import strategies as st

from targets.common.dataset import SEED_EDITIONS, SEED_LISTS, USERS, natural_key

__all__ = [
    "Probe",
    "Observation",
    "observe",
    "compare",
    "classify",
    "Registry",
    "deterministic_corpus",
    "generated_probes",
    "generated_post_probes",
    "Cluster",
    "CampaignReport",
    "run_campaign",
    "TAG_TITLES",
    "QUIRK_TAGS",
    "main",
]

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REGISTRY_PATH = ROOT / ".state" / "divergence_registry.json"

#: Maximum number of human-readable lines :func:`compare` returns.
MAX_DIFF_LINES = 8

#: The five known quirk tags, in SPEC section 3 order (D-1 .. D-5 on a fresh registry).
QUIRK_TAGS: tuple[str, ...] = (
    "json_suffix",
    "loose_query",
    "missing_resource_200",
    "non_positive_quantity",
    "empty_body_500",
)

#: Human-friendly cluster titles per quirk tag.
TAG_TITLES: dict[str, str] = {
    "json_suffix": "Implicit .json suffix routing",
    "loose_query": "Loose query-param coercion",
    "missing_resource_200": "Missing list answers 200 not_found",
    "non_positive_quantity": "Non-positive quote quantity accepted",
    "empty_body_500": "Empty add/remove POST crashes with 500",
}

#: Cluster statuses that count as resolved.
RESOLVED_STATUSES = frozenset({"preserved-ok", "deprecated-ok", "converged"})

#: Path segment -> placeholder for the segment that follows it (route templating).
_TEMPLATE_PLACEHOLDERS = {
    "people": "{user}",
    "lists": "{list_id}",
    "books": "{olid}",
    "works": "{work_id}",
    "authors": "{author_id}",
}

QUOTE_TEMPLATE = "/books/{olid}/quote"


# --------------------------------------------------------------------------------------
# Probes and observations
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Probe:
    """One HTTP request sent identically to both targets.

    Attributes:
        method: HTTP method (``"GET"`` / ``"POST"``).
        path: Request path, possibly with a ``.json`` suffix.
        params: Ordered query parameters as ``(name, value)`` pairs.
        body: JSON body for mutating requests (``None`` = no body).
        label: Documentation only (e.g. the quirk a corpus probe targets).  It is
            NEVER consulted by :func:`classify`.
    """

    method: str
    path: str
    params: tuple[tuple[str, str], ...] = ()
    body: dict | None = None
    label: str = ""

    @property
    def mutating(self) -> bool:
        """True for any non-GET probe (these run sequentially with a reset first)."""
        return self.method.upper() != "GET"

    @property
    def url_template(self) -> str:
        """Route template, e.g. ``/people/{user}/lists/{list_id}/seeds``.

        The ``.json`` suffix is stripped and every segment following a known
        collection segment (``people``, ``lists``, ``books``, ...) is replaced by a
        placeholder.
        """
        path = self.path[:-5] if self.path.endswith(".json") else self.path
        segments = path.split("/")
        out: list[str] = []
        previous = None
        for seg in segments:
            if previous in _TEMPLATE_PLACEHOLDERS and seg:
                out.append(_TEMPLATE_PLACEHOLDERS[previous])
                previous = None  # a placeholder is never itself a collection name
                continue
            out.append(seg)
            previous = seg
        return "/".join(out)

    @property
    def query_string(self) -> str:
        """URL-encoded query string (no leading ``?``)."""
        return urlencode(list(self.params))

    def param(self, name: str) -> str | None:
        """Return the last value of query parameter ``name`` (web.py semantics), or None."""
        value = None
        for key, val in self.params:
            if key == name:
                value = val
        return value

    def describe(self) -> str:
        """Compact request line such as ``GET /books/OL1M/quote?quantity=2``."""
        line = f"{self.method} {self.path}"
        if self.params:
            line += "?" + self.query_string
        if self.body is not None:
            line += " " + json.dumps(self.body, separators=(",", ":"))
        return line

    def key(self) -> tuple:
        """Hashable identity (``body`` is a dict, so the dataclass itself is not hashable)."""
        body = json.dumps(self.body, sort_keys=True) if self.body is not None else None
        return (self.method, self.path, self.params, body)

    def to_dict(self) -> dict:
        """JSON-serialisable form (used by the registry)."""
        return {"method": self.method, "path": self.path, "params": [list(p) for p in self.params],
                "body": self.body, "label": self.label}

    @classmethod
    def from_dict(cls, data: dict) -> "Probe":
        """Inverse of :meth:`to_dict`."""
        return cls(method=data["method"], path=data["path"],
                   params=tuple((str(k), str(v)) for k, v in data.get("params") or ()),
                   body=data.get("body"), label=data.get("label", ""))


@dataclass
class Observation:
    """What one target answered for one probe.

    Attributes:
        status: HTTP status code (``0`` on transport failure).
        content_type: Lower-cased media type with parameters stripped
            (``"application/json; charset=utf-8"`` -> ``"application/json"``).
        body: Parsed JSON for JSON responses, otherwise the text body.
        error: Transport error message, or None.
    """

    status: int
    content_type: str
    body: Any
    error: str | None = None

    @property
    def is_json(self) -> bool:
        """True when the response declared a JSON media type."""
        return "json" in self.content_type


def normalize_content_type(value: str | None) -> str:
    """Lower-case a Content-Type header and drop its parameters."""
    return (value or "").split(";", 1)[0].strip().lower()


async def observe(client: httpx.AsyncClient, base_url: str, probe: Probe) -> Observation:
    """Send ``probe`` to ``base_url`` and capture the response as an :class:`Observation`.

    Transport errors never raise: they yield ``Observation(status=0, error=str(exc))``.
    Only status, content type and body are captured; volatile headers (``date``,
    ``server``, ``content-length``, ``x-*``) are deliberately ignored.
    """
    url = base_url.rstrip("/") + probe.path
    kwargs: dict[str, Any] = {"params": list(probe.params)}
    if probe.body is not None:
        kwargs["json"] = probe.body
    try:
        resp = await client.request(probe.method, url, **kwargs)
    except (httpx.HTTPError, OSError) as exc:
        return Observation(status=0, content_type="", body=None, error=f"{type(exc).__name__}: {exc}")
    ctype = normalize_content_type(resp.headers.get("content-type"))
    body: Any = resp.text
    if "json" in ctype:
        try:
            body = resp.json()
        except ValueError:
            body = resp.text
    return Observation(status=resp.status_code, content_type=ctype, body=body)


# --------------------------------------------------------------------------------------
# compare
# --------------------------------------------------------------------------------------


def _json_type(value: Any) -> str:
    """JSON-level type name; distinguishes bool from int and int from float."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def _short(value: Any, limit: int = 60) -> str:
    """Compact JSON rendering of a value for diff lines."""
    try:
        text = json.dumps(value, separators=(",", ":"), sort_keys=True)
    except (TypeError, ValueError):
        text = repr(value)
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _pointer(base: str, token: Any) -> str:
    """Append a JSON-pointer token (escaping ``~`` and ``/``)."""
    tok = str(token).replace("~", "~0").replace("/", "~1")
    return f"{base}/{tok}"


def _json_diff(legacy: Any, modern: Any, path: str = "") -> Iterator[str]:
    """Yield diff lines for two JSON values, depth first, keys in legacy order then extras."""
    where = path or "/"
    lt, mt = _json_type(legacy), _json_type(modern)
    if lt != mt:
        yield f"type {where}: legacy {lt} {_short(legacy)} != modern {mt} {_short(modern)}"
        return
    if lt == "object":
        for key in legacy:
            if key not in modern:
                yield f"missing key {_pointer(path, key)}: legacy has {_short(legacy[key])}, modern lacks it"
        for key in modern:
            if key not in legacy:
                yield f"extra key {_pointer(path, key)}: modern has {_short(modern[key])}, legacy lacks it"
        for key in legacy:
            if key in modern:
                yield from _json_diff(legacy[key], modern[key], _pointer(path, key))
    elif lt == "array":
        if len(legacy) != len(modern):
            yield f"length {where}: legacy {len(legacy)} != modern {len(modern)}"
        for idx, (lv, mv) in enumerate(zip(legacy, modern)):
            yield from _json_diff(lv, mv, _pointer(path, idx))
    elif legacy != modern:  # floats compared exactly on purpose
        yield f"value {where}: legacy {_short(legacy)} != modern {_short(modern)}"


def _diff_lines(legacy: Observation, modern: Observation) -> Iterator[str]:
    """Yield every difference between two observations, most significant first."""
    if legacy.error or modern.error:
        yield f"transport error: legacy {legacy.error or 'ok'} | modern {modern.error or 'ok'}"
    if legacy.status != modern.status:
        yield f"status: legacy {legacy.status} != modern {modern.status}"
    if legacy.content_type != modern.content_type:
        yield f"content-type: legacy {legacy.content_type or '-'} != modern {modern.content_type or '-'}"
    if legacy.is_json and modern.is_json:
        yield from _json_diff(legacy.body, modern.body)
    elif not legacy.is_json and not modern.is_json and legacy.status == modern.status:
        if legacy.body != modern.body:
            yield f"body text: legacy {_short(legacy.body)} != modern {_short(modern.body)}"


def compare(legacy: Observation, modern: Observation) -> list[str]:
    """Return human-readable difference lines (at most :data:`MAX_DIFF_LINES`).

    Checks, in order: transport errors (reported even if both sides failed, so an
    unreachable pair can never look identical), status, normalised content type, then
    a deep JSON diff (missing/extra keys, type changes incl. ``int`` vs ``float`` vs
    ``bool``, value changes with exact float comparison, list lengths) using
    JSON-pointer paths.  Text bodies are compared verbatim only when both are non-JSON
    and the statuses are equal.  ``[]`` means the observations are identical.
    """
    lines: list[str] = []
    for line in _diff_lines(legacy, modern):
        lines.append(line)
        if len(lines) >= MAX_DIFF_LINES:
            break
    return lines


# --------------------------------------------------------------------------------------
# classify
# --------------------------------------------------------------------------------------


def _parse_int(value: str | None) -> int | None:
    """``int(value.strip())`` or None if it does not parse."""
    if value is None:
        return None
    try:
        return int(value.strip())
    except ValueError:
        return None


def _empty_add_remove(body: dict | None) -> bool:
    """True when a seeds POST body carries no add and no remove entries."""
    if body is None:
        return True
    if not isinstance(body, dict):
        return False
    return not body.get("add") and not body.get("remove")


def _is_not_found(body: Any) -> bool:
    """True for an application-level ``{"error": "not_found", ...}`` payload."""
    return isinstance(body, dict) and body.get("error") == "not_found"


#: What FastAPI/Starlette answer for a path that no route matches.
ROUTING_404_BODY = {"detail": "Not Found"}


def _is_routing_404(obs: Observation) -> bool:
    return obs.status == 404 and obs.body == ROUTING_404_BODY


def classify(probe: Probe, legacy: Observation, modern: Observation) -> str:
    """Tag a divergence with a quirk name from request/response features (SPEC section 7).

    Rules are evaluated in order; the first match wins:

    1. path ends ``.json``, modern answers the *routing* 404 (``{"detail": "Not Found"}``: no route
       matched) and legacy serves the path (not 404) or answers its *application* 404
       (``{"error": "not_found", ...}``, e.g. an unknown user) -> ``json_suffix``
    2. POST, legacy 500, add/remove both empty -> ``empty_body_500``
    3. legacy 200 body has ``"error": "not_found"``, modern 404 -> ``missing_resource_200``
    4. quote endpoint, ``quantity`` parses as int <= 0, modern 422 -> ``non_positive_quantity``
    5. GET, modern 422, legacy 2xx or non-422 4xx -> ``loose_query``
    6. otherwise ``unclassified:<method>:<route-template>:<legacy>:<modern>``

    ``probe.label`` is never consulted.

    Why rule 1 demands the exact routing 404: the quirk is "the ``.json`` route does not exist".
    A modern 404 with any other body means the route exists and answered — e.g. its own
    ``not_found`` payload for a missing list (that is the D-3 status quirk, rule 3) or a wrong
    body (a real divergence that must stay ``unclassified``, never be absorbed into D-1).
    A legacy *text* 404 means legacy has no such route either, so that pair is not the quirk.
    """
    method = probe.method.upper()
    if (probe.path.endswith(".json") and _is_routing_404(modern)
            and (legacy.status != 404 or _is_not_found(legacy.body))):
        return "json_suffix"
    if method == "POST" and legacy.status == 500 and _empty_add_remove(probe.body):
        return "empty_body_500"
    if legacy.status == 200 and _is_not_found(legacy.body) and modern.status == 404:
        return "missing_resource_200"
    if probe.url_template == QUOTE_TEMPLATE and modern.status == 422:
        quantity = _parse_int(probe.param("quantity"))
        if quantity is not None and quantity <= 0:
            return "non_positive_quantity"
    if method == "GET" and modern.status == 422 and (
            200 <= legacy.status < 300 or (400 <= legacy.status < 500 and legacy.status != 422)):
        return "loose_query"
    return f"unclassified:{method}:{probe.url_template}:{legacy.status}:{modern.status}"


def tag_title(tag: str) -> str:
    """Human-friendly title for a tag (unclassified tags are spelled out)."""
    if tag in TAG_TITLES:
        return TAG_TITLES[tag]
    if tag.startswith("unclassified:"):
        _, method, template, ls, ms = (tag.split(":", 4) + [""] * 5)[:5]
        return f"Unclassified {method} {template} ({ls} vs {ms})"
    return tag


# --------------------------------------------------------------------------------------
# Registry
# --------------------------------------------------------------------------------------


class Registry:
    """Persistent ``tag -> cluster`` mapping with first-come ``D-n`` numbering.

    File format (``$ROOT/.state/divergence_registry.json``)::

        {"json_suffix": {"id": "D-1", "modern_status_at_discovery": 404,
                         "legacy_status_at_discovery": 200, "sample": {...probe...},
                         "first_seen": "2025-...Z"}, ...}

    The registry is re-read from disk on every access so several processes (or a
    reset from a test) never see stale numbering; writes are atomic.
    """

    def __init__(self, path: Path | str = DEFAULT_REGISTRY_PATH) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()

    # -- persistence ---------------------------------------------------------------
    def _load(self) -> dict[str, dict]:
        try:
            data = json.loads(self.path.read_text())
        except FileNotFoundError:
            return {}
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _save(self, data: dict[str, dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".divergence_registry.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as fh:
                json.dump(data, fh, indent=2, sort_keys=False)
            os.replace(tmp, self.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    @staticmethod
    def _number(cluster_id: str) -> int:
        try:
            return int(str(cluster_id).split("-", 1)[1])
        except (IndexError, ValueError):
            return 0

    # -- public API ----------------------------------------------------------------
    def id_for(self, tag: str, modern_status: int, *, legacy_status: int | None = None,
               sample: Probe | None = None) -> str:
        """Return the cluster ID for ``tag``, allocating the next ``D-n`` on first sight.

        ``modern_status`` (and optional ``legacy_status`` / ``sample`` probe) are
        recorded only when the tag is first registered; later calls never overwrite them.
        """
        with self._lock:
            data = self._load()
            entry = data.get(tag)
            if entry is not None:
                return entry["id"]
            next_n = max((self._number(e.get("id", "")) for e in data.values()), default=0) + 1
            entry = {
                "id": f"D-{next_n}",
                "modern_status_at_discovery": modern_status,
                "legacy_status_at_discovery": legacy_status,
                "sample": sample.to_dict() if sample is not None else None,
                "first_seen": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }
            data[tag] = entry
            self._save(data)
            return entry["id"]

    def get(self, tag: str) -> dict | None:
        """Return the stored entry for ``tag`` (a copy), or None."""
        entry = self._load().get(tag)
        return dict(entry) if entry is not None else None

    def tag_for(self, cluster_id: str) -> str | None:
        """Reverse lookup: the tag registered under ``cluster_id``, or None."""
        for tag, entry in self._load().items():
            if entry.get("id") == cluster_id:
                return tag
        return None

    def all(self) -> dict[str, dict]:
        """All entries, ordered by numeric cluster ID."""
        data = self._load()
        return dict(sorted(data.items(), key=lambda kv: self._number(kv[1].get("id", ""))))

    def reset(self) -> None:
        """Forget every cluster (deletes the registry file)."""
        with self._lock:
            self.path.unlink(missing_ok=True)


# --------------------------------------------------------------------------------------
# Probe corpus
# --------------------------------------------------------------------------------------


def _get(path: str, label: str, **params: str) -> Probe:
    return Probe("GET", path, tuple(params.items()), None, label)


def _post(path: str, body: dict | None, label: str) -> Probe:
    return Probe("POST", path, (), body, label)


def deterministic_corpus() -> list[Probe]:
    """The fixed probe corpus, ordered so the SPEC section 3 quirks surface as D-1..D-5.

    Groups (in order): ``json_suffix``, ``loose_query``, ``missing_resource_200``,
    ``non_positive_quantity``, ``empty_body_500``, then ``baseline`` probes that must
    be identical in every correct build.  Labels document intent only.
    """
    sci_fi = "/people/george08/lists/OL1L"
    return [
        # D-1 json_suffix: implicit .json aliases
        _get("/people/george08/lists.json", "json_suffix"),
        _get(f"{sci_fi}/seeds.json", "json_suffix"),
        _get("/books/OL7353617M/quote.json", "json_suffix", quantity="2"),
        _get("/people/openlibrary/lists/OL10L/editions.json", "json_suffix"),
        _post("/people/george08/lists/OL2L/seeds.json", {"add": ["/books/OL3M"]}, "json_suffix"),
        # D-2 loose_query: lenient coercion of query params
        _get("/people/george08/lists", "loose_query", limit="none"),
        _get("/people/george08/lists", "loose_query", limit=""),
        _get("/people/george08/lists", "loose_query", details="maybe"),
        _get("/books/OL1M/quote", "loose_query", quantity="abc"),
        # D-3 missing_resource_200: unknown list answers 200 {"error": "not_found"}
        _get("/people/george08/lists/OL99L/seeds", "missing_resource_200"),
        _get("/people/george08/lists/OL99L/editions", "missing_resource_200"),
        _get("/people/nobody/lists/OL1L/seeds", "missing_resource_200"),
        _post("/people/george08/lists/OL99L/seeds", {"add": ["/books/OL1M"]}, "missing_resource_200"),
        # D-4 non_positive_quantity: quote quantity in -10..0 is legal in legacy
        _get("/books/OL1M/quote", "non_positive_quantity", quantity="-1"),
        _get("/books/OL4M/quote", "non_positive_quantity", quantity="-10"),
        _get("/books/OL2M/quote", "non_positive_quantity", quantity="0"),
        # D-5 empty_body_500: ZeroDivisionError in the legacy analytics line
        _post(f"{sci_fi}/seeds", {}, "empty_body_500"),
        _post(f"{sci_fi}/seeds", {"add": []}, "empty_body_500"),
        _post(f"{sci_fi}/seeds", {"add": [], "remove": []}, "empty_body_500"),
        # Baseline: must match in every build
        _get("/people/nobody/lists", "baseline:unknown_user_404"),
        _get(f"{sci_fi}/seeds", "baseline:seeds"),
        _get("/people/openlibrary/lists", "baseline:lists"),
        _get("/books/OL2M/quote", "baseline:float_total", quantity="3"),
        _get("/books/OL4M/quote", "baseline:float_total", quantity="7"),
        _get("/books/OL1M/quote", "baseline:quantity_out_of_range", quantity="101"),
        _get("/books/OL9999M/quote", "baseline:unknown_edition_404", quantity="1"),
        _get("/people/openlibrary/lists/OL10L/editions", "baseline:editions_paging", limit="2", offset="1"),
        _get("/people/george08/lists", "baseline:details", details="1"),
        _post(f"{sci_fi}/seeds", {"add": ["bogus"]}, "baseline:invalid_seed_400"),
        _post(f"{sci_fi}/seeds", {"add": ["/works/OL1W"], "remove": ["/books/OL1M"]}, "baseline:valid_change"),
    ]


# --------------------------------------------------------------------------------------
# Generated probes (Hypothesis)
# --------------------------------------------------------------------------------------

_SPECIAL_VALUES = ("", "none", "all", "abc", "1", "yes", "maybe", "0", "false")
_UNKNOWN_USERS = ("nobody", "ghost_42")
_UNKNOWN_LISTS = ("OL99L", "OL7L")
_UNKNOWN_OLIDS = ("OL9999M", "OL0M")
_EXTRA_SEEDS = ("/works/OL1W", "/books/OL3M", "/authors/OL2A", "/subjects/poetry", "/books/OL5M")
_BAD_SEED = "bogus"


class _Picker:
    """Source of choices, backed either by Hypothesis ``data`` or a seeded ``Random``.

    Probe builders are written once against this interface so the Hypothesis-driven
    generation and the deterministic top-up produce probes from the same space.
    """

    def __init__(self, data: Any = None, rng: random.Random | None = None) -> None:
        self._data, self._rng = data, rng

    def choice(self, pool: Sequence[Any]) -> Any:
        if self._data is not None:
            return self._data.draw(st.sampled_from(pool))
        return self._rng.choice(pool)

    def integer(self, lo: int, hi: int) -> int:
        if self._data is not None:
            return self._data.draw(st.integers(lo, hi))
        return self._rng.randint(lo, hi)

    def boolean(self) -> bool:
        if self._data is not None:
            return self._data.draw(st.booleans())
        return self._rng.random() < 0.5


def _pools(seed: int) -> dict[str, list]:
    """Value pools, shuffled by ``seed`` so different seeds explore different probes."""
    rng = random.Random(seed)
    pools = {
        "users": sorted(USERS) + list(_UNKNOWN_USERS),
        "lists": sorted({lid for (_, lid) in SEED_LISTS}, key=natural_key) + list(_UNKNOWN_LISTS),
        "olids": sorted(SEED_EDITIONS, key=natural_key) + list(_UNKNOWN_OLIDS),
        "pairs": sorted(SEED_LISTS, key=lambda k: (k[0], natural_key(k[1])))
                 + [("george08", "OL99L"), ("nobody", "OL1L")],
        "seeds": sorted({s for rec in SEED_LISTS.values() for s in rec["seeds"]} | set(_EXTRA_SEEDS)),
        "specials": list(_SPECIAL_VALUES),
        "endpoints": ["lists", "seeds", "editions", "quote"],
    }
    for pool in pools.values():
        rng.shuffle(pool)
    return pools


def _param_value(pick: _Picker, pools: dict[str, list]) -> str:
    """An int in [-15, 120] or one of the special loose values."""
    if pick.boolean():
        return str(pick.integer(-15, 120))
    return pick.choice(pools["specials"])


def _build_get(pick: _Picker, pools: dict[str, list]) -> Probe:
    """Build one GET probe across E1/E2/E4/E5."""
    endpoint = pick.choice(pools["endpoints"])
    suffix = ".json" if pick.boolean() else ""
    params: list[tuple[str, str]] = []
    if endpoint == "lists":
        path = f"/people/{pick.choice(pools['users'])}/lists{suffix}"
        names = ("limit", "offset", "details")
    elif endpoint in ("seeds", "editions"):
        path = f"/people/{pick.choice(pools['users'])}/lists/{pick.choice(pools['lists'])}/{endpoint}{suffix}"
        names = ("limit", "offset") if endpoint == "editions" else ()
    else:
        path = f"/books/{pick.choice(pools['olids'])}/quote{suffix}"
        names = ("quantity",)
    for name in names:
        if pick.boolean():
            params.append((name, _param_value(pick, pools)))
    return Probe("GET", path, tuple(params), None, f"generated:{endpoint}")


def _build_post(pick: _Picker, pools: dict[str, list]) -> Probe:
    """Build one seeds POST probe with add/remove lists of seed strings."""
    user, list_id = pick.choice(pools["pairs"])
    suffix = ".json" if pick.boolean() else ""

    def seeds() -> list[str]:
        out = [pick.choice(pools["seeds"]) for _ in range(pick.integer(0, 3))]
        if out and pick.integer(0, 5) == 0:
            out[pick.integer(0, len(out) - 1)] = _BAD_SEED
        return out

    body: dict[str, list[str]] = {}
    if pick.boolean() or pick.boolean():
        body["add"] = seeds()
    if pick.boolean():
        body["remove"] = seeds()
    return Probe("POST", f"/people/{user}/lists/{list_id}/seeds{suffix}", (), body, "generated:post")


def _collect(builder: Callable[[_Picker, dict], Probe], n: int, seed: int) -> list[Probe]:
    """Run ``builder`` under Hypothesis for up to ``n`` distinct probes, then top up.

    Hypothesis runs derandomized (no example database), so the result depends only on
    ``n`` and ``seed``.  If Hypothesis yields fewer than ``n`` distinct probes, a
    ``Random(seed)``-driven picker fills the remainder deterministically.
    """
    if n <= 0:
        return []
    pools = _pools(seed)
    out: list[Probe] = []
    seen: set[tuple] = set()

    def add(probe: Probe) -> None:
        if len(out) < n and probe.key() not in seen:
            seen.add(probe.key())
            out.append(probe)

    @settings(max_examples=n, derandomize=True, database=None, deadline=None,
              suppress_health_check=list(HealthCheck), phases=(Phase.generate,))
    @given(st.data())
    def _explore(data: Any) -> None:
        add(builder(_Picker(data=data), pools))

    _explore()

    rng = random.Random(seed)
    attempts = 0
    while len(out) < n and attempts < n * 50:
        add(builder(_Picker(rng=rng), pools))
        attempts += 1
    while len(out) < n:  # space exhausted: allow repeats rather than return short
        out.append(builder(_Picker(rng=rng), pools))
    return out


def generated_probes(n: int = 120, seed: int = 1337) -> list[Probe]:
    """``n`` distinct Hypothesis-generated GET probes across E1/E2/E4/E5 (deterministic)."""
    return _collect(_build_get, n, seed)


def generated_post_probes(k: int = 12, seed: int = 1337) -> list[Probe]:
    """``k`` distinct Hypothesis-generated seeds POST probes (deterministic)."""
    return _collect(_build_post, k, seed)


# --------------------------------------------------------------------------------------
# Campaign
# --------------------------------------------------------------------------------------


@dataclass
class Cluster:
    """All divergences sharing one tag, plus the human decision about them."""

    id: str
    tag: str
    title: str
    count: int
    legacy_status: int | None
    modern_status: int | None
    sample_request: str
    sample_diff: list[str]
    decision: str | None
    status: str

    @property
    def resolved(self) -> bool:
        """True when the cluster no longer blocks parity."""
        return self.status in RESOLVED_STATUSES


@dataclass
class CampaignReport:
    """Outcome of one :func:`run_campaign` run."""

    total_requests: int
    identical: int
    divergent: int
    clusters: list[Cluster]
    resolved: int
    unresolved: int
    approved_parity: bool
    parity_label: str
    duration_s: float
    legacy_url: str = ""
    modern_url: str = ""

    def cluster(self, cluster_id: str) -> Cluster | None:
        """Look up a cluster by ``D-n`` id."""
        return next((c for c in self.clusters if c.id == cluster_id), None)

    def to_dict(self) -> dict:
        """JSON-serialisable form."""
        return asdict(self)

    def to_markdown(self) -> str:
        """Compact Markdown summary line plus the divergence table."""
        head = (f"**{self.parity_label}** · {self.total_requests} probes · {self.identical} identical · "
                f"{self.divergent} divergent · {self.duration_s:.2f}s")
        if not self.clusters:
            return head + "\n\nNo divergences between legacy and modern."
        rows = [
            "| ID | Divergence | Legacy | Modern | Hits | Sample | Decision/Status |",
            "|----|------------|--------|--------|------|--------|-----------------|",
        ]
        for c in self.clusters:
            sample = c.sample_request if len(c.sample_request) <= 70 else c.sample_request[:67] + "..."
            sample = sample.replace("|", "\\|").replace("`", "'")
            rows.append(
                f"| {c.id} | {_md(c.title)} | {_status_cell(c.legacy_status)} | {_status_cell(c.modern_status)} "
                f"| {c.count} | `{sample}` | {c.decision or '—'} / {c.status} |"
            )
        return head + "\n\n" + "\n".join(rows)


def _md(text: str) -> str:
    return text.replace("|", "\\|")


def _status_cell(status: int | None) -> str:
    return "—" if status is None else str(status)


@dataclass
class _Outcome:
    probe: Probe
    legacy: Observation
    modern: Observation
    diff: list[str] = field(default_factory=list)


@dataclass
class _Bucket:
    tag: str
    cluster_id: str
    outcomes: list[_Outcome] = field(default_factory=list)


class CampaignError(RuntimeError):
    """A target could not be reset (usually: it is not running)."""


async def _reset(client: httpx.AsyncClient, base_url: str) -> None:
    """``POST {base_url}/_admin/reset``; raise :class:`CampaignError` on failure."""
    try:
        resp = await client.post(base_url.rstrip("/") + "/_admin/reset")
    except (httpx.HTTPError, OSError) as exc:
        raise CampaignError(f"reset failed for {base_url}: {exc}") from exc
    if resp.status_code >= 400:
        raise CampaignError(f"reset failed for {base_url}: HTTP {resp.status_code}")


async def _reset_both(client: httpx.AsyncClient, legacy_url: str, modern_url: str) -> None:
    await asyncio.gather(_reset(client, legacy_url), _reset(client, modern_url))


async def _observe_pair(client: httpx.AsyncClient, legacy_url: str, modern_url: str,
                        probe: Probe) -> tuple[Observation, Observation]:
    legacy, modern = await asyncio.gather(observe(client, legacy_url, probe), observe(client, modern_url, probe))
    return legacy, modern


def _normalize_decisions(decisions: dict[str, Any] | None) -> dict[str, str]:
    """Accept ``{"D-1": {"decision": "preserve"}}`` or ``{"D-1": "preserve"}``."""
    out: dict[str, str] = {}
    for cid, value in (decisions or {}).items():
        raw = value.get("decision") if isinstance(value, dict) else value
        if isinstance(raw, str) and raw.strip().lower() in ("preserve", "deprecate"):
            out[str(cid).strip().upper()] = raw.strip().lower()
    return out


def _parity_label(resolved: int, total: int, approved: bool) -> str:
    if approved:
        return f"100% Approved Parity ({resolved}/{total})"
    pct = (100 * resolved) // total if total else 0
    return f"Parity {pct}% ({resolved}/{total} clusters resolved)"


async def run_campaign(legacy_url: str, modern_url: str, decisions: dict[str, dict] | None = None, *,
                       n_random: int = 120, seed: int = 1337, registry: Registry | None = None,
                       n_post: int = 12) -> CampaignReport:
    """Shadow-test ``modern_url`` against ``legacy_url`` and fold in human decisions.

    Flow: reset both targets; fire every GET (corpus + ``n_random`` generated)
    concurrently against both targets; then run each mutating probe (corpus +
    ``n_post`` generated) sequentially with a reset of both targets before it; reset
    once more at the end.  Divergences are classified and assigned IDs in corpus order
    (then generated GETs, then generated POSTs) regardless of completion order, so
    numbering is deterministic.

    Every cluster ever recorded in ``registry`` is reported, including clusters with
    zero hits this run.  Cluster status:

    * no decision & hits -> ``open``; no decision & 0 hits -> ``converged`` (resolved,
      except for ``unclassified:*`` tags, which stay ``open`` until decided)
    * ``preserve`` -> ``preserved-ok`` with 0 hits, else ``preserved-regressed``
    * ``deprecate`` -> ``deprecated-ok`` when every hit (or, with 0 hits, every corpus /
      sample re-probe) shows ``modern_status_at_discovery``, else ``deprecated-violated``

    Raises:
        CampaignError: if either target cannot be reset.
    """
    registry = registry if registry is not None else Registry()
    decided = _normalize_decisions(decisions)
    started = time.perf_counter()

    corpus = deterministic_corpus()
    gen_gets = generated_probes(n_random, seed) if n_random > 0 else []
    gen_posts = generated_post_probes(n_post, seed) if n_post > 0 else []
    ordered = corpus + gen_gets + gen_posts  # canonical order for ID assignment
    get_idx = [i for i, p in enumerate(ordered) if not p.mutating]
    post_idx = [i for i, p in enumerate(ordered) if p.mutating]
    results: dict[int, tuple[Observation, Observation]] = {}

    limits = httpx.Limits(max_connections=128, max_keepalive_connections=64)
    async with httpx.AsyncClient(limits=limits, timeout=httpx.Timeout(30.0)) as client:
        await _reset_both(client, legacy_url, modern_url)

        pairs = await asyncio.gather(*(_observe_pair(client, legacy_url, modern_url, ordered[i]) for i in get_idx))
        results.update(zip(get_idx, pairs))

        for i in post_idx:
            await _reset_both(client, legacy_url, modern_url)
            results[i] = await _observe_pair(client, legacy_url, modern_url, ordered[i])
        if post_idx:
            await _reset_both(client, legacy_url, modern_url)

        # ---- classify in canonical order -------------------------------------------
        outcomes = [_Outcome(ordered[i], *results[i]) for i in range(len(ordered))]
        buckets: dict[str, _Bucket] = {}
        identical = 0
        for out in outcomes:
            out.diff = compare(out.legacy, out.modern)
            if not out.diff:
                identical += 1
                continue
            tag = classify(out.probe, out.legacy, out.modern)
            cid = registry.id_for(tag, out.modern.status, legacy_status=out.legacy.status, sample=out.probe)
            buckets.setdefault(tag, _Bucket(tag, cid)).outcomes.append(out)

        # ---- build clusters (every registry entry, seen this run or before) ---------
        corpus_outcomes = outcomes[: len(corpus)]
        clusters: list[Cluster] = []
        for tag, entry in registry.all().items():
            cid = entry["id"]
            bucket = buckets.get(tag)
            hits = bucket.outcomes if bucket else []
            decision = decided.get(cid)
            at_discovery = entry.get("modern_status_at_discovery")
            legacy_status = modern_status = None
            sample_request, sample_diff = "", []
            if hits:
                first = hits[0]
                legacy_status, modern_status = first.legacy.status, first.modern.status
                sample_request, sample_diff = first.probe.describe(), first.diff
            elif entry.get("sample"):
                sample_request = Probe.from_dict(entry["sample"]).describe()

            if decision is None:
                if hits or tag.startswith("unclassified:"):
                    status = "open"
                else:
                    status = "converged"
            elif decision == "preserve":
                status = "preserved-ok" if not hits else "preserved-regressed"
            else:
                if hits:
                    # The deprecated behaviour is the contract for every probe of this quirk: one that
                    # silently converged back to legacy behaviour is an inconsistent API, not "ok".
                    reverted = [o for o in corpus_outcomes if o.probe.label == tag and not o.diff]
                    ok = not reverted and all(h.modern.status == at_discovery for h in hits)
                else:
                    reprobes = await _reprobe(client, legacy_url, modern_url, tag, entry, corpus_outcomes)
                    ok = bool(reprobes) and all(o.modern.status == at_discovery for o in reprobes)
                    if reprobes:
                        legacy_status, modern_status = reprobes[0].legacy.status, reprobes[0].modern.status
                status = "deprecated-ok" if ok else "deprecated-violated"

            clusters.append(Cluster(
                id=cid, tag=tag, title=tag_title(tag), count=len(hits), legacy_status=legacy_status,
                modern_status=modern_status, sample_request=sample_request, sample_diff=list(sample_diff),
                decision=decision, status=status,
            ))

    clusters.sort(key=lambda c: Registry._number(c.id))
    resolved = sum(1 for c in clusters if c.resolved)
    unresolved = len(clusters) - resolved
    divergent = len(outcomes) - identical
    approved = unresolved == 0 and (len(clusters) > 0 or divergent == 0)
    return CampaignReport(
        total_requests=len(outcomes), identical=identical, divergent=divergent, clusters=clusters,
        resolved=resolved, unresolved=unresolved, approved_parity=approved,
        parity_label=_parity_label(resolved, len(clusters), approved),
        duration_s=round(time.perf_counter() - started, 3), legacy_url=legacy_url, modern_url=modern_url,
    )


async def _reprobe(client: httpx.AsyncClient, legacy_url: str, modern_url: str, tag: str, entry: dict,
                   corpus_outcomes: Iterable[_Outcome]) -> list[_Outcome]:
    """Evidence for a deprecated cluster that produced no divergence this run.

    Uses this run's observations of the corpus probes documented for ``tag`` plus a
    fresh re-probe of the sample request recorded at discovery (reset around it if
    it mutates state).
    """
    evidence = [o for o in corpus_outcomes if o.probe.label == tag]
    sample = entry.get("sample")
    if sample:
        probe = Probe.from_dict(sample)
        if probe.mutating:
            await _reset_both(client, legacy_url, modern_url)
        legacy, modern = await _observe_pair(client, legacy_url, modern_url, probe)
        if probe.mutating:
            await _reset_both(client, legacy_url, modern_url)
        evidence.append(_Outcome(probe, legacy, modern))
    return evidence


# --------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------


def _load_decisions(raw: str | None) -> dict | None:
    """Parse ``--decisions``: inline JSON, or ``@path`` to a JSON file."""
    if not raw:
        return None
    text = Path(raw[1:]).read_text() if raw.startswith("@") else raw
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("--decisions must be a JSON object")
    return data


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point for ``python -m agents.divergence``; prints the Markdown report."""
    ap = argparse.ArgumentParser(prog="python -m agents.divergence", description=__doc__.split("\n\n")[0])
    ap.add_argument("--legacy", default=os.environ.get("LEGACY_URL", "http://127.0.0.1:9001"))
    ap.add_argument("--modern", default=os.environ.get("MODERN_URL", "http://127.0.0.1:9002"))
    ap.add_argument("--decisions", help='JSON like {"D-1": {"decision": "preserve"}} or @file.json')
    ap.add_argument("--n-random", type=int, default=120)
    ap.add_argument("--seed", type=int, default=1337)
    ap.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY_PATH)
    ap.add_argument("--json", action="store_true", help="print the report as JSON instead of Markdown")
    args = ap.parse_args(argv)

    report = asyncio.run(run_campaign(args.legacy, args.modern, _load_decisions(args.decisions),
                                      n_random=args.n_random, seed=args.seed, registry=Registry(args.registry)))
    print(json.dumps(report.to_dict(), indent=2) if args.json else report.to_markdown())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
