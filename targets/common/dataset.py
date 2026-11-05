"""Shared Open Library seed dataset used by both the legacy and the modern target.

Both stacks read the same seed data -- just as the real web.py Open Library and a
FastAPI rewrite would read the same production database -- but every process keeps
its *own* mutable copy inside a :class:`Store`, so writes made through one target
never leak into the other.  ``POST /_admin/reset`` on either target calls
:meth:`Store.reset` to restore the pristine seed state between test probes.

See ``docs/SPEC.md`` section 1 for the binding contract.
"""
from __future__ import annotations

import copy
import re
import threading

__all__ = ["SEED_LISTS", "SEED_EDITIONS", "USERS", "SEED_UPDATE_STAMP", "Store", "natural_key"]

#: ``last_update`` value written by :meth:`Store.apply_seed_changes`.  It is a fixed
#: string (not ``now()``) so that the legacy and modern targets produce identical bodies.
SEED_UPDATE_STAMP = "2025-01-01T00:00:00"

USERS: set[str] = {"george08", "openlibrary", "mekBot"}


def _list(user: str, list_id: str, name: str, description: str, seeds: list[str], last_update: str) -> dict:
    """Build a list record in the canonical Open Library shape."""
    return {
        "key": f"/people/{user}/lists/{list_id}",
        "name": name,
        "description": description,
        "seeds": list(seeds),
        "last_update": last_update,
    }


SEED_LISTS: dict[tuple[str, str], dict] = {
    ("george08", "OL1L"): _list(
        "george08", "OL1L", "Sci-Fi Classics", "The genre-defining novels everyone should read.",
        ["/works/OL45883W", "/books/OL7353617M", "/authors/OL26320A", "/books/OL1M", "/subjects/science_fiction"],
        "2024-03-01T10:00:00",
    ),
    ("george08", "OL2L"): _list(
        "george08", "OL2L", "Reading 2025", "My reading challenge for 2025.",
        ["/works/OL27258W", "/books/OL2M"],
        "2024-11-15T08:30:00",
    ),
    ("george08", "OL3L"): _list(
        "george08", "OL3L", "Empty Shelf", "", [], "2024-01-01T00:00:00",
    ),
    ("openlibrary", "OL10L"): _list(
        "openlibrary", "OL10L", "Staff Picks", "Books the Open Library staff loved this year.",
        ["/books/OL3M", "/works/OL82563W", "/books/OL4M", "/authors/OL23919A", "/books/OL5M", "/subjects/fantasy"],
        "2024-06-20T12:00:00",
    ),
    ("mekBot", "OL20L"): _list(
        "mekBot", "OL20L", "Bot Import Queue", "Editions queued for metadata import.",
        ["/books/OL7353617M"],
        "2024-09-09T09:09:09",
    ),
}


def _edition(olid: str, title: str, authors: list[str], publish_date: str, price: float) -> dict:
    """Build an edition record in the canonical Open Library shape."""
    return {"key": f"/books/{olid}", "title": title, "authors": list(authors), "publish_date": publish_date,
            "price": price}


# Prices deliberately include float-sensitive values (0.1, 3.33) so that ``round(p * q, 2)``
# exposes any target that computes totals differently (e.g. via Decimal).
SEED_EDITIONS: dict[str, dict] = {
    "OL7353617M": _edition("OL7353617M", "Fantastic Mr. Fox", ["Roald Dahl"], "October 1, 1988", 12.99),
    "OL1M": _edition("OL1M", "Foundation", ["Isaac Asimov"], "1951", 7.49),
    "OL2M": _edition("OL2M", "Dune", ["Frank Herbert"], "1965", 0.1),
    "OL3M": _edition("OL3M", "The Left Hand of Darkness", ["Ursula K. Le Guin"], "1969", 19.95),
    "OL4M": _edition("OL4M", "Neuromancer", ["William Gibson"], "1984", 3.33),
    "OL5M": _edition("OL5M", "The Hobbit", ["J.R.R. Tolkien"], "September 21, 1937", 8.99),
}

_NATURAL_CHUNK = re.compile(r"(\d+)")


def natural_key(text: str) -> list:
    """Sort key giving natural order for ids such as ``OL1L < OL2L < OL10L``."""
    return [int(part) if part.isdigit() else part for part in _NATURAL_CHUNK.split(text)]


class Store:
    """Thread-safe, per-process mutable copy of the seed dataset.

    All public methods return deep copies, so callers can never mutate the store's
    internal state by accident (both web servers run handlers on thread pools).
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.lists: dict[tuple[str, str], dict] = {}
        self.editions: dict[str, dict] = {}
        self.reset()

    def reset(self) -> None:
        """Restore the pristine seed data (deep copy of ``SEED_LISTS`` / ``SEED_EDITIONS``)."""
        with self._lock:
            self.lists = copy.deepcopy(SEED_LISTS)
            self.editions = copy.deepcopy(SEED_EDITIONS)

    def user_exists(self, user: str) -> bool:
        """Return True if ``user`` is a known account."""
        return user in USERS

    def get_list(self, user: str, list_id: str) -> dict | None:
        """Return a copy of the list record, or None if the user or list is unknown."""
        with self._lock:
            rec = self.lists.get((user, list_id))
            return copy.deepcopy(rec) if rec is not None else None

    def user_lists(self, user: str) -> list[dict]:
        """Return copies of all of ``user``'s lists, in natural ``list_id`` order."""
        with self._lock:
            ids = sorted((lid for (owner, lid) in self.lists if owner == user), key=natural_key)
            return [copy.deepcopy(self.lists[(user, lid)]) for lid in ids]

    def apply_seed_changes(self, user: str, list_id: str, add: list[str], remove: list[str]) -> dict:
        """Add/remove seeds on an existing list and return a copy of the updated record.

        Each ``add`` seed not already present is appended (order preserved, duplicates
        within ``add`` collapse); each ``remove`` seed present is removed.  ``last_update``
        is set to the deterministic :data:`SEED_UPDATE_STAMP`.

        Raises:
            KeyError: if the list does not exist.
        """
        with self._lock:
            rec = self.lists[(user, list_id)]
            seeds = rec["seeds"]
            for seed in add:
                if seed not in seeds:
                    seeds.append(seed)
            for seed in remove:
                if seed in seeds:
                    seeds.remove(seed)
            rec["last_update"] = SEED_UPDATE_STAMP
            return copy.deepcopy(rec)

    def get_edition(self, olid: str) -> dict | None:
        """Return a copy of the edition record for ``olid``, or None if unknown."""
        with self._lock:
            rec = self.editions.get(olid)
            return copy.deepcopy(rec) if rec is not None else None
