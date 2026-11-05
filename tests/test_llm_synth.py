"""Contract tests for agents/llm_synth.py — @translator's LLM synthesis mode (docs/AGENTS_SPEC.md §2).

API:
    DEFAULT_LLM_CMD = "claude -p --model claude-sonnet-5-5 --output-format text"
    class SynthesisError(RuntimeError)            # every failure; str(exc) is a one-line human-readable reason
    def build_prompt(legacy_source: str, policy: dict) -> str
        # instructions + docs/SPEC.md §2–§3 (verbatim, from "## 2." up to but excluding "## 4.") + the legacy
        # source in a ```python fence + the policy as the FIRST ```json fence (json.dumps of the dict, so
        # json.loads of that block == policy) + the hard requirements (module-level `router = APIRouter()`,
        # answer with exactly one ```python fenced block).
    def extract_module(answer: str) -> str        # body of the first ```python / ```py fence (else the first bare
                                                  #   ``` fence), with exactly one trailing newline; SynthesisError
                                                  #   ("no fenced python code block …") when there is none or it is empty
    def validate_module(source: str) -> list[str] # [] when OK; else problems, each mentioning one of
                                                  #   "syntax error" (ast.parse fails), "router" (no module-level
                                                  #   `router` binding), "compile" (compile() fails)
    def synthesize(legacy_source, policy, *, cmd=None, timeout=300) -> str
        # cmd = cmd or $STRANGLER_LLM_CMD or DEFAULT_LLM_CMD, split with shlex (no shell); prompt on stdin;
        # SynthesisError on: command not found, timeout ("timed out"), non-zero exit (mentions the exit
        # status and the stderr tail), no fenced block, or any validate_module problem. Returns the module.
"""
from __future__ import annotations

import json
import re
import shlex
import sys
from pathlib import Path

import pytest

from agents.codegen import render_lists_module
from agents.llm_synth import (DEFAULT_LLM_CMD, SynthesisError, build_prompt, extract_module, synthesize,
                              validate_module)
from tests.fake_llm import FAKE_HEADER

ROOT = Path(__file__).resolve().parents[1]
LEGACY = ROOT / "targets/legacy_openlibrary/openlibrary/plugins/openlibrary/lists.py"
FAKE = ROOT / "tests" / "fake_llm.py"
POLICY = {"json_suffix": "preserve", "loose_query": "preserve", "missing_resource_200": "deprecate",
          "non_positive_quantity": "preserve", "empty_body_500": "deprecate"}


def fake_cmd(mode: str, *extra: str) -> str:
    return shlex.join([sys.executable, str(FAKE), "--mode", mode, *extra])


@pytest.fixture(scope="module")
def legacy_source() -> str:
    return LEGACY.read_text(encoding="utf-8")


# ------------------------------------------------------------------------------------ build_prompt

def test_default_command_is_the_spec_one():
    assert DEFAULT_LLM_CMD == "claude -p --model claude-sonnet-5-5 --output-format text"


def test_prompt_carries_source_spec_and_policy(legacy_source):
    prompt = build_prompt(legacy_source, POLICY)
    assert legacy_source.strip() in prompt
    assert "## 2. Endpoint contract" in prompt and "## 3. The five quirks" in prompt
    assert "## 4. Modern target" not in prompt
    first_json = re.search(r"```json\n(.*?)\n```", prompt, re.DOTALL)
    assert first_json and json.loads(first_json.group(1)) == POLICY
    assert "router" in prompt and "```python" in prompt


def test_prompt_with_empty_policy_is_naive_v1(legacy_source):
    prompt = build_prompt(legacy_source, {})
    assert json.loads(re.search(r"```json\n(.*?)\n```", prompt, re.DOTALL).group(1)) == {}


# ---------------------------------------------------------------------------------- extract_module

def test_extract_first_python_fence():
    answer = "Sure!\n\n```python\nx = 1\nrouter = 2\n```\n\nand also\n\n```python\ny = 2\n```\n"
    assert extract_module(answer) == "x = 1\nrouter = 2\n"


def test_extract_bare_fence_fallback():
    assert extract_module("```\nrouter = 1\n```") == "router = 1\n"


@pytest.mark.parametrize("answer", ["no code here", "```python\n\n```", ""])
def test_extract_without_code_fails(answer):
    with pytest.raises(SynthesisError, match="fenced"):
        extract_module(answer)


# --------------------------------------------------------------------------------- validate_module

def test_validate_good_module():
    assert validate_module(render_lists_module({})) == []
    assert validate_module("from x import router\n") == []
    assert validate_module("router: object = object()\n") == []


@pytest.mark.parametrize("source, needle", [
    ("def (:\n    pass\n", "syntax error"),
    ("from fastapi import APIRouter\napp_router = APIRouter()\n", "router"),
    ("def f():\n    router = 1\n", "router"),  # not module level
    ("router = 1\nreturn router\n", "compile"),
])
def test_validate_reports_problems(source, needle):
    problems = validate_module(source)
    assert problems and any(needle in p.lower() for p in problems), problems


# ------------------------------------------------------------------------------------- synthesize

def test_synthesize_good(legacy_source, tmp_path):
    out = tmp_path / "prompt.txt"
    src = synthesize(legacy_source, POLICY, cmd=fake_cmd("good", "--prompt-out", str(out)), timeout=60)
    assert src == render_lists_module(POLICY, header_comment=FAKE_HEADER)
    assert out.read_text(encoding="utf-8") == build_prompt(legacy_source, POLICY)


def test_synthesize_uses_env_command(legacy_source, monkeypatch):
    monkeypatch.setenv("STRANGLER_LLM_CMD", fake_cmd("good"))
    assert synthesize(legacy_source, {}) == render_lists_module({}, header_comment=FAKE_HEADER)


@pytest.mark.parametrize("mode, needle", [
    ("bad_syntax", "syntax error"),
    ("no_router", "router"),
    ("compile_error", "compile"),
    ("no_fence", "fenced"),
    ("fail", "simulated model outage"),
])
def test_synthesize_rejects(legacy_source, mode, needle):
    with pytest.raises(SynthesisError) as exc:
        synthesize(legacy_source, POLICY, cmd=fake_cmd(mode), timeout=60)
    assert needle in str(exc.value).lower()


def test_synthesize_reports_exit_status(legacy_source):
    with pytest.raises(SynthesisError, match="3"):
        synthesize(legacy_source, POLICY, cmd=fake_cmd("fail"), timeout=60)


def test_synthesize_timeout(legacy_source):
    with pytest.raises(SynthesisError, match="timed out"):
        synthesize(legacy_source, POLICY, cmd=fake_cmd("sleep"), timeout=1)


def test_synthesize_missing_command(legacy_source):
    with pytest.raises(SynthesisError, match="not found|No such file"):
        synthesize(legacy_source, POLICY, cmd="definitely-not-a-real-llm-binary-xyz --flag", timeout=10)


def test_runtime_error_passes_static_validation(legacy_source):
    """Only the post-reload /_meta check can catch a module that explodes on import."""
    src = synthesize(legacy_source, POLICY, cmd=fake_cmd("runtime_error"), timeout=60)
    assert "raise RuntimeError" in src and validate_module(src) == []
