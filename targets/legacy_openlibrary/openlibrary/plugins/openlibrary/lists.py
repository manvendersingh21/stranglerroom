"""Lists API -- reading and editing a patron's reading lists.

Endpoints (all JSON; every page below also answers on its ``.json`` alias):

* ``GET  /people/{user}/lists``                      -- lists_json
* ``GET  /people/{user}/lists/{list_id}/seeds``      -- list_seeds.GET
* ``POST /people/{user}/lists/{list_id}/seeds``      -- list_seeds.POST
* ``GET  /people/{user}/lists/{list_id}/editions``   -- list_editions_json
* ``GET  /books/{olid}/quote``                       -- edition_quote_json

Query parameters are parsed leniently (``legacy_int`` / ``legacy_bool``) because
years of bookmarklets and warehouse scripts send ``limit=all``, ``details=yes`` and
the like.  Some long-standing behaviours are relied upon by clients and must not be
"fixed" casually:

* a missing list answers ``200 {"error": "not_found", "entries": []}``;
* ``/books/{olid}/quote`` accepts negative quantities (credit reversals);
* the seed-churn analytics line in ``list_seeds.POST`` divides by the number of
  changes, so an empty edit request ends in web.py's stock 500 page.
"""
import json
import logging
import re

import web

from targets.legacy_openlibrary.infogami_shim import delegate

logger = logging.getLogger("openlibrary.lists")

JSON_HEADERS = {"Content-Type": "application/json"}

SEED_TYPES = {
    "/works/": "work",
    "/books/": "edition",
    "/authors/": "author",
    "/subjects/": "subject",
}

QUOTE_MIN_QUANTITY = -10
QUOTE_MAX_QUANTITY = 100


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def legacy_int(v, default):
    """Parse an integer query parameter the way old Open Library clients expect.

    ``None`` (parameter absent) gives ``default``; ``"none"``/``"all"`` mean
    unlimited and give ``None``; anything unparsable silently falls back to ``default``.
    """
    if v is None:
        return default
    if v.strip().lower() in ("none", "all"):
        return None
    try:
        return int(v.strip())
    except ValueError:
        return default


def legacy_bool(v):
    """Parse a boolean query parameter: anything not obviously false is true."""
    if v is None:
        return False
    return v.strip().lower() not in {"", "0", "false", "no", "off", "n", "f", "none"}


def slug(name):
    """URL slug for a list name: lowercase, non-alphanumeric runs become ``-``."""
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def seed_type(seed):
    """Return the seed type (work/edition/author/subject) or None if the key is invalid."""
    for prefix, kind in SEED_TYPES.items():
        if seed.startswith(prefix):
            return kind
    return None


def paginate(entries, offset, limit):
    """Plain Python slicing; ``limit=None`` means everything after ``offset``."""
    return entries[offset:(offset + limit) if limit is not None else None]


def json_error(status, **body):
    """Build an HTTPError carrying a JSON body (raise the result)."""
    return web.HTTPError(status, headers=JSON_HEADERS, data=json.dumps(body))


def list_not_found():
    """Legacy response for an unknown user or list: 200 with an error body."""
    web.header("Content-Type", "application/json")
    return json.dumps({"error": "not_found", "entries": []})


# ---------------------------------------------------------------------------
# pages
# ---------------------------------------------------------------------------

class lists_json(delegate.page):
    """All lists owned by a patron."""

    path = r"/people/([^/]+)/lists"
    encoding = "json"

    def GET(self, user):
        i = web.input(limit=None, offset=None, details=None)
        site = web.ctx.site

        if not site.user_exists(user):
            raise json_error("404 Not Found", error="not_found", key="/people/" + user)

        limit = legacy_int(i.limit, 100)
        offset = legacy_int(i.offset, 0)
        if offset is None:
            offset = 0
        details = legacy_bool(i.details)

        lists = site.user_lists(user)
        entries = []
        for lst in paginate(lists, offset, limit):
            entry = {
                "url": lst["key"],
                "full_url": lst["key"] + "/" + slug(lst["name"]),
                "name": lst["name"],
                "seed_count": len(lst["seeds"]),
                "last_update": lst["last_update"],
            }
            if details:
                entry["description"] = lst["description"]
                entry["seeds"] = lst["seeds"]
            entries.append(entry)

        web.header("Content-Type", "application/json")
        return json.dumps({
            "links": {
                "self": "/people/%s/lists.json" % user,
                "user": "/people/%s" % user,
            },
            "size": len(lists),
            "entries": entries,
        })


class list_seeds(delegate.page):
    """Seeds (works, editions, authors, subjects) of one list; POST edits them."""

    path = r"/people/([^/]+)/lists/([^/]+)/seeds"
    encoding = "json"

    def GET(self, user, list_id):
        lst = web.ctx.site.get_list(user, list_id)
        if lst is None:
            return list_not_found()

        key = lst["key"]
        web.header("Content-Type", "application/json")
        return json.dumps({
            "links": {"self": key + "/seeds.json", "list": key},
            "size": len(lst["seeds"]),
            "entries": [{"url": seed, "type": seed_type(seed)} for seed in lst["seeds"]],
        })

    def POST(self, user, list_id):
        site = web.ctx.site
        lst = site.get_list(user, list_id)
        if lst is None:
            return list_not_found()

        data = json.loads(web.data() or b"{}")
        add = data.get("add", [])
        remove = data.get("remove", [])

        for seed in add + remove:
            if seed_type(seed) is None:
                raise json_error("400 Bad Request", error="invalid_seed", seed=seed)

        # seed-churn analytics: share of the edit that removes things.
        churn = len(remove) / len(add + remove)
        logger.info("list %s seed edit: +%d -%d churn=%.2f", lst["key"], len(add), len(remove), churn)

        before = lst["seeds"]
        after = site.apply_seed_changes(user, list_id, add, remove)["seeds"]

        web.header("Content-Type", "application/json")
        return json.dumps({
            "success": True,
            "size": len(after),
            "added": len([s for s in after if s not in before]),
            "removed": len([s for s in before if s not in after]),
        })


class list_editions_json(delegate.page):
    """Editions (``/books/`` seeds) of one list, without pricing."""

    path = r"/people/([^/]+)/lists/([^/]+)/editions"
    encoding = "json"

    def GET(self, user, list_id):
        i = web.input(limit=None, offset=None)
        site = web.ctx.site

        lst = site.get_list(user, list_id)
        if lst is None:
            return list_not_found()

        limit = legacy_int(i.limit, 50)
        offset = legacy_int(i.offset, 0)
        if offset is None:
            offset = 0

        edition_keys = [seed for seed in lst["seeds"] if seed.startswith("/books/")]
        entries = []
        for seed in paginate(edition_keys, offset, limit):
            edition = site.get_edition(seed.split("/")[2])
            if edition is None:
                # seed points at an edition we have no record for; nothing to show.
                continue
            edition.pop("price", None)
            entries.append(edition)

        key = lst["key"]
        web.header("Content-Type", "application/json")
        return json.dumps({
            "links": {"self": key + "/editions.json", "list": key},
            "size": len(edition_keys),
            "entries": entries,
        })


class edition_quote_json(delegate.page):
    """Price quote for buying (or, with a negative quantity, crediting) an edition."""

    path = r"/books/([^/]+)/quote"
    encoding = "json"

    def GET(self, olid):
        i = web.input(quantity=None)

        edition = web.ctx.site.get_edition(olid)
        if edition is None:
            raise json_error("404 Not Found", error="not_found", key="/books/" + olid)

        quantity = legacy_int(i.quantity, 1)
        if quantity is None:
            quantity = 1

        # negative quantities are credit reversals issued by the warehouse scripts.
        if not QUOTE_MIN_QUANTITY <= quantity <= QUOTE_MAX_QUANTITY:
            raise json_error("400 Bad Request", error="quantity_out_of_range",
                             min=QUOTE_MIN_QUANTITY, max=QUOTE_MAX_QUANTITY)

        price = edition["price"]
        if quantity < 0:
            kind = "credit_reversal"
        elif quantity == 0:
            kind = "noop"
        else:
            kind = "sale"

        web.header("Content-Type", "application/json")
        return json.dumps({
            "key": edition["key"],
            "title": edition["title"],
            "unit_price": price,
            "quantity": quantity,
            "total": round(price * quantity, 2),
            "currency": "USD",
            "type": kind,
        })
