"""Process-wide data store for the modern FastAPI target (docs/SPEC.md §4).

The generated ``app/lists.py`` imports :data:`STORE` from here, and ``app/main.py``
resets it via ``POST /_admin/reset``.  Keeping the store in its own module means a
regenerated ``lists.py`` never owns (or re-creates) the mutable state.
"""
from __future__ import annotations

from targets.common.dataset import Store

__all__ = ["STORE"]

#: The single mutable copy of the seed dataset owned by this process.
STORE = Store()
