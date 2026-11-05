"""Source-code generators used by @translator.

:mod:`agents.codegen.fastapi_lists` renders the modern target's ``app/lists.py`` from a
quirk policy (docs/SPEC.md §3, §4).
"""
from agents.codegen.fastapi_lists import QUIRK_TAGS, module_rev, render_lists_module

__all__ = ["QUIRK_TAGS", "module_rev", "render_lists_module"]
