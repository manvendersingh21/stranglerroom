"""Fake ``STRANGLER_LLM_CMD`` for tests of :mod:`agents.llm_synth` and @translator's LLM mode.

Usage (as an LLM command line; the prompt arrives on stdin, the answer goes to stdout)::

    python tests/fake_llm.py --mode good [--prompt-out FILE]

Modes:

* ``good``          — a chatty answer with ONE fenced ```` ```python ```` block holding
                      ``render_lists_module(policy, header_comment=FAKE_HEADER)``, where ``policy`` is parsed
                      from the first ```` ```json ```` block of the prompt (the contract of
                      :func:`agents.llm_synth.build_prompt`). The header makes its rev differ from the
                      deterministic codegen, so a test can tell which one was deployed.
* ``bad_syntax``    — a fenced block that does not parse (``def (:``).
* ``no_router``     — a fenced block that parses and compiles but defines no ``router``.
* ``compile_error`` — parses with ``ast`` but fails ``compile()`` (``return`` outside a function).
* ``runtime_error`` — parses, compiles and assigns ``router``, but raises when imported: only the
                      post-reload ``/_meta`` check can catch it.
* ``no_fence``      — prose without any fenced code block.
* ``fail``          — prints to stderr and exits with status 3.
* ``sleep``         — sleeps 30 s (to exercise the caller's timeout).

``--prompt-out FILE`` saves the received prompt, so a test can inspect what the caller sent.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

FAKE_HEADER = "Synthesised by tests/fake_llm.py (fake LLM)."
_JSON_BLOCK = re.compile(r"```json\n(.*?)\n```", re.DOTALL)


def _policy_from(prompt: str) -> dict:
    m = _JSON_BLOCK.search(prompt)
    if not m:
        raise SystemExit("fake_llm: prompt has no ```json policy block")
    return json.loads(m.group(1))


def _fenced(code: str) -> str:
    return ("Here is the FastAPI port you asked for.\n\n```python\n" + code.rstrip("\n")
            + "\n```\n\nIt preserves exactly the quirks listed in the policy.\n")


def answer(mode: str, prompt: str) -> str:
    if mode == "good":
        from agents.codegen import render_lists_module
        return _fenced(render_lists_module(_policy_from(prompt), header_comment=FAKE_HEADER))
    if mode == "bad_syntax":
        return _fenced("from fastapi import APIRouter\nrouter = APIRouter()\ndef (:\n    pass\n")
    if mode == "no_router":
        return _fenced("from fastapi import APIRouter\napp_router = APIRouter()\n")
    if mode == "compile_error":
        return _fenced("from fastapi import APIRouter\nrouter = APIRouter()\nreturn router\n")
    if mode == "runtime_error":
        return _fenced("from fastapi import APIRouter\nrouter = APIRouter()\n"
                       "raise RuntimeError('fake LLM module explodes on import')\n")
    if mode == "no_fence":
        return "I am sorry, I cannot produce code for that request.\n"
    raise SystemExit(f"fake_llm: unknown mode {mode!r}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--mode", required=True)
    parser.add_argument("--prompt-out")
    args = parser.parse_args(argv)
    prompt = sys.stdin.read()
    if args.prompt_out:
        Path(args.prompt_out).write_text(prompt, encoding="utf-8")
    if args.mode == "fail":
        print("fake_llm: simulated model outage", file=sys.stderr)
        return 3
    if args.mode == "sleep":
        time.sleep(30)
        return 0
    sys.stdout.write(answer(args.mode, prompt))
    return 0


if __name__ == "__main__":
    sys.exit(main())
