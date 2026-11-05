"""The room command grammar shared by every StranglerRoom agent — the single source of truth.

Each agent parses what it is told with the ``parse_*`` functions here, and every agent that
hands work to another builds the message with the matching builder, so the wire format of a
handoff cannot drift between agents.

Human commands (only accepted from a human room member):
    @translator migrate /people/{user}/lists from legacy_openlibrary to FastAPI
    @translator preserve D-1 D-2 D-4 and deprecate D-3 D-5
    @cutover-gate approve-cutover
    @cutover-gate rollback

Agent handoffs (``migration=<id>`` scopes divergence ids to one migration):
    translator   -> fuzzer        shadow-test rev=<rev> migration=<id>
    translator   -> fuzzer        re-verify rev=<rev> migration=<id> — human decisions applied: preserve D-1 … and deprecate D-3 …
    fuzzer       -> cutover-gate  benchmark rev=<rev> migration=<id>
    cutover-gate -> translator    record-cutover rev=<rev> migration=<id>

Anyone may ask any agent for ``status`` or ``help``.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

MENTION_RE = re.compile(r"(?<![A-Za-z0-9._-])@([A-Za-z0-9._-]+)")
ID_RE = re.compile(r"\bD-(\d+)\b", re.IGNORECASE)
REV_RE = re.compile(r"\brev=([0-9a-f]{6,64})\b", re.IGNORECASE)
MIGRATION_RE = re.compile(r"\bmigration=(MIG-[0-9A-Za-z-]+)\b")
MIGRATE_RE = re.compile(
    r"\bmigrate\s+(?P<path>/\S+)(?:\s+from\s+(?P<source>[A-Za-z0-9_.-]+))?(?:\s+to\s+(?P<target>[A-Za-z0-9_.-]+))?",
    re.IGNORECASE,
)
DECISION_TOKEN_RE = re.compile(r"\b(preserve|deprecate)\b|\bD-(\d+)\b", re.IGNORECASE)

MIGRATE_EXAMPLE = "@translator migrate /people/{user}/lists from legacy_openlibrary to FastAPI"
APPROVE_EXAMPLE = "@cutover-gate approve-cutover"


class CommandError(ValueError):
    """The message addressed an agent with a recognisable but malformed command."""


@dataclass(frozen=True)
class Command:
    verb: str
    args: dict = field(default_factory=dict)


def mentions(body: str) -> list[str]:
    """Lower-cased ``@names`` in ``body`` (Switch's own token-bounded mention rule)."""
    return [m.lower() for m in MENTION_RE.findall(body.replace("*", "").replace("~", ""))]


def defuse_mentions(text: str) -> str:
    """Make every ``@name`` in ``text`` inert: it still reads as ``@name`` but a U+2060 WORD JOINER after
    the ``@`` breaks Switch's (and Mattermost's) mention token, so quoting text never addresses anyone."""
    return text.replace("@", "@\u2060")


def strip_mentions(body: str) -> str:
    """``body`` without ``@name`` tokens and markdown emphasis, whitespace collapsed."""
    text = MENTION_RE.sub(" ", body.replace("*", "").replace("~", "").replace("`", ""))
    return " ".join(text.split())


def _rev(text: str) -> str | None:
    m = REV_RE.search(text)
    return m.group(1).lower() if m else None


def _migration(text: str) -> str | None:
    m = MIGRATION_RE.search(text)
    return m.group(1) if m else None


def normalize_id(raw: str) -> str:
    m = ID_RE.fullmatch(raw.strip())
    if not m:
        raise CommandError(f"not a divergence id: {raw!r}")
    return f"D-{int(m.group(1))}"


def parse_decisions(text: str) -> dict[str, str]:
    """``"preserve D-1 D-2 D-4 and deprecate D-3 D-5"`` -> ``{"D-1": "preserve", ..., "D-5": "deprecate"}``.

    Every id must follow a verb, no id may get two different verdicts, and at least one id is required."""
    decisions: dict[str, str] = {}
    verb: str | None = None
    for m in DECISION_TOKEN_RE.finditer(text):
        if m.group(1):
            verb = m.group(1).lower()
            continue
        cid = f"D-{int(m.group(2))}"
        if verb is None:
            raise CommandError(f"{cid} appears before any 'preserve'/'deprecate' verb")
        if decisions.get(cid, verb) != verb:
            raise CommandError(f"{cid} is marked both preserve and deprecate")
        decisions[cid] = verb
    if not decisions:
        raise CommandError("no divergence ids given — e.g. 'preserve D-1 D-2 and deprecate D-3'")
    return dict(sorted(decisions.items(), key=lambda kv: int(kv[0][2:])))


def format_decisions(decisions: dict[str, str]) -> str:
    """Inverse of :func:`parse_decisions` (canonical ordering)."""
    keep = [c for c, d in sorted(decisions.items(), key=lambda kv: int(kv[0][2:])) if d == "preserve"]
    drop = [c for c, d in sorted(decisions.items(), key=lambda kv: int(kv[0][2:])) if d == "deprecate"]
    parts = []
    if keep:
        parts.append("preserve " + " ".join(keep))
    if drop:
        parts.append("deprecate " + " ".join(drop))
    return " and ".join(parts)


FILLER_WORDS = frozenset({"please", "pls", "kindly", "hey", "hi", "ok", "okay"})
_PUNCT = ",.:;!?—–-"


def _verb(text: str) -> tuple[str, list[str]]:
    """The command verb is the FIRST word of the message (after @mentions and polite filler).

    Only the leading word decides the command — never a word somewhere later in the sentence —
    so "status — do NOT rollback" is a status request and "will we preserve D-1?" is not a decision.
    This matters most for the human-only governance verbs (migrate, preserve/deprecate,
    approve-cutover, rollback)."""
    words = text.split()
    while words and words[0].lower().strip(_PUNCT) in FILLER_WORDS:
        words = words[1:]
    if not words:
        return "", []
    return words[0].lower().strip(_PUNCT), words[1:]


def _common(verb: str) -> Command | None:
    if verb == "help":
        return Command("help")
    if verb == "status":
        return Command("status")
    return None


# ---------------------------------------------------------------------------------- per agent

def parse_translator(body: str) -> Command | None:
    text = strip_mentions(body)
    verb, rest = _verb(text)
    if verb == "record-cutover":
        return Command("record-cutover", {"rev": _rev(text), "migration": _migration(text)})
    if verb == "migrate":
        m = MIGRATE_RE.search(" ".join(["migrate", *rest]))
        if not m:
            raise CommandError(f"usage: `{MIGRATE_EXAMPLE}`")
        return Command("migrate", {"path": m.group("path"), "source": m.group("source") or "legacy_openlibrary",
                                   "target": m.group("target") or "FastAPI"})
    if verb in ("preserve", "deprecate"):
        return Command("decide", {"decisions": parse_decisions(" ".join([verb, *rest]))})
    return _common(verb)


def parse_fuzzer(body: str) -> Command | None:
    text = strip_mentions(body)
    verb, rest = _verb(text)
    if verb in ("re-verify", "reverify", "verify", "re-run", "rerun"):
        decisions = None
        tail = " ".join(rest)
        if re.search(r"\b(preserve|deprecate)\b", tail, re.IGNORECASE):
            decisions = parse_decisions(tail.split("applied:", 1)[-1])
        return Command("re-verify", {"rev": _rev(text), "migration": _migration(text), "decisions": decisions})
    if verb in ("shadow-test", "shadowtest", "fuzz", "run"):
        return Command("shadow-test", {"rev": _rev(text), "migration": _migration(text)})
    return _common(verb)


def parse_cutover(body: str) -> Command | None:
    text = strip_mentions(body)
    verb, rest = _verb(text)
    if verb == "approve-cutover" or (verb == "approve" and rest and rest[0].lower().strip(_PUNCT) == "cutover"):
        return Command("approve-cutover")
    if verb == "rollback":
        return Command("rollback")
    if verb in ("benchmark", "bench"):
        return Command("benchmark", {"rev": _rev(text), "migration": _migration(text)})
    return _common(verb)


# ------------------------------------------------------------------------------------ builders
# The bodies below are sent with send_targeted_message, which prefixes "@<target> " itself.

def shadow_test(rev: str, migration: str) -> str:
    return f"shadow-test rev={rev} migration={migration}"


def re_verify(rev: str, migration: str, decisions: dict[str, str]) -> str:
    return f"re-verify rev={rev} migration={migration} — human decisions applied: {format_decisions(decisions)}"


def benchmark(rev: str, migration: str) -> str:
    return f"benchmark rev={rev} migration={migration}"


def record_cutover(rev: str, migration: str) -> str:
    return f"record-cutover rev={rev} migration={migration}"


def decision_template(cluster_ids: list[str]) -> str:
    """A ready-to-edit decision line listing every open id under 'preserve'."""
    ids = " ".join(sorted(cluster_ids, key=lambda c: int(c[2:])))
    return f"@translator preserve {ids}"


# ------------------------------------------------------------------------------------ markers
# Stable prefixes of the room messages each agent posts. The e2e driver and the tests match on
# these, so an agent must start the corresponding message with exactly this text.
MARK = {
    "migration_accepted": "🧭 Migration",          # translator: "🧭 Migration MIG-… accepted from @user"
    "v1_deployed": "🛠️ v1 deployed",               # translator: "... rev `<rev>` ..."
    "decisions_applied": "🔧 Applied human decisions",  # translator: "... v2 deployed ... rev `<rev>`"
    "campaign": "🔬 Shadow campaign",               # fuzzer: "🔬 Shadow campaign #<n> on rev `<rev>` ..."
    "decision_needed": "decision needed",           # fuzzer -> human (send_targeted adds "@user ")
    "approved_parity": "✅ 100% Approved Parity",   # fuzzer: "✅ 100% Approved Parity (5/5) on rev `<rev>` ..."
    "benchmark_card": "📊 Benchmark",               # cutover-gate: card with p50/p95/p99
    "approval_needed": "approval needed",           # cutover-gate -> human
    "cutover_complete": "🚦 Cutover complete",      # cutover-gate
    "rollback_complete": "↩️ Rollback complete",    # cutover-gate
    "ledger_sealed": "🧾 Ledger sealed",            # translator
    "governance_refusal": "🛑 Governance gate",     # any agent refusing a human-only command from an agent
    "error": "⚠️",                                  # any agent: a failure it could not complete
}
