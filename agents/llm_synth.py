"""Synthesize the translator's FastAPI lists module using an external LLM.

Build a prompt containing SPEC sections 2 and 3, the legacy source, and the quirk
policy. Send it on stdin to an explicitly supplied command, STRANGLER_LLM_CMD,
or DEFAULT_LLM_CMD, using shlex splitting and no shell. Extract a fenced Python
module and statically check its syntax, module-level router binding, and
compilability without importing or executing it.

Operational failures raise SynthesisError with a single-line reason. The caller
handles deployment, post-reload import and route checks, and any fallback to
deterministic code generation.
"""
from __future__ import annotations

import ast
import json
import os
import re
import shlex
import subprocess
from pathlib import Path

DEFAULT_LLM_CMD = "claude -p --model claude-sonnet-5-5 --output-format text"


class SynthesisError(RuntimeError):
    """A synthesis failure with a one-line, human-readable reason."""

    def __init__(self, reason: str) -> None:
        super().__init__(" ".join(str(reason).split()))


def build_prompt(legacy_source: str, policy: dict) -> str:
    """Include the repository's SPEC sections 2–3 and the synthesis inputs."""
    try:
        spec_path = Path(__file__).resolve().parents[1] / "docs" / "SPEC.md"
        spec = spec_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError, RuntimeError) as exc:
        raise SynthesisError(f"Cannot read docs/SPEC.md: {exc}") from exc

    lines = spec.splitlines(keepends=True)
    start = next(
        (index for index, line in enumerate(lines) if line.startswith("## 2.")),
        None,
    )
    if start is None:
        raise SynthesisError("docs/SPEC.md is missing the section 2 heading")
    end = next(
        (
            index
            for index in range(start + 1, len(lines))
            if lines[index].startswith("## 4.")
        ),
        None,
    )
    if end is None:
        raise SynthesisError("docs/SPEC.md is missing the section 4 heading")
    excerpt = "".join(lines[start:end])

    try:
        policy_json = json.dumps(policy, indent=2, sort_keys=True)
    except (TypeError, ValueError, OverflowError, RecursionError) as exc:
        raise SynthesisError(f"Cannot serialize quirk policy: {exc}") from exc

    instructions = (
        "Port the legacy web.py lists plugin into a complete FastAPI APIRouter "
        "module for targets/modern_fastapi/app/lists.py.\n"
        "Follow the endpoint contract and quirk table in SPEC sections 2 and 3 "
        "included below. For each quirk tag, 'preserve' means reproduce the "
        "legacy behaviour exactly. 'deprecate' or an absent tag means use the "
        "textbook FastAPI v1 behaviour in SPEC section 3's Modern v1 (naive) "
        "column. Everything else must produce byte-identical JSON to legacy, "
        "including keys, values, floats, status codes, and content types.\n"
        "Use `from targets.modern_fastapi.app.store import STORE` for data. "
        "Apart from this required project import, use only stdlib, fastapi, "
        "and pydantic imports.\n"
        "Define module-level `router = APIRouter()`, attach all endpoint "
        "handlers to it, and do not create an app object.\n"
        "Answer with exactly ONE ```python fenced block containing the whole "
        "module, with no other code blocks or surrounding explanation.\n"
    )
    return (
        instructions
        + "\nQuirk policy:\n```json\n"
        + policy_json
        + "\n```\n\nSPEC sections 2 and 3 (verbatim):\n"
        + excerpt
        + "\nLegacy source:\n```python\n"
        + legacy_source
        + ("" if legacy_source.endswith("\n") else "\n")
        + "```\n"
    )


def _module_body(body: str) -> str:
    """Normalize a selected fence body or report an empty block."""
    if not body.strip():
        raise SynthesisError("no fenced python code block containing code")
    return body.rstrip() + "\n"


def extract_module(answer: str) -> str:
    """Select the first Python fence, falling back to the first bare fence."""
    info: str | None = None
    body: list[str] = []
    fallback: str | None = None

    for line in answer.replace("\r\n", "\n").splitlines(keepends=True):
        marker = line.strip()
        if info is None:
            opening = re.fullmatch(r"```([^`]*)", marker)
            if opening is not None:
                info = opening.group(1).strip().lower()
                body = []
        elif marker == "```":
            contents = "".join(body)
            if info in {"python", "py"}:
                return _module_body(contents)
            if info == "" and fallback is None:
                fallback = contents
            info = None
            body = []
        else:
            body.append(line)

    if fallback is not None:
        return _module_body(fallback)
    raise SynthesisError("no fenced python code block found")


def _binds_router(target: ast.expr) -> bool:
    """Recognize router in assignment targets, including unpacking."""
    if isinstance(target, ast.Name):
        return target.id == "router"
    if isinstance(target, (ast.Tuple, ast.List)):
        return any(_binds_router(element) for element in target.elts)
    if isinstance(target, ast.Starred):
        return _binds_router(target.value)
    return False


def _defines_router(statement: ast.stmt) -> bool:
    """Recognize supported bindings directly in the module body."""
    if isinstance(statement, ast.Assign):
        return any(_binds_router(target) for target in statement.targets)
    if isinstance(statement, (ast.AnnAssign, ast.AugAssign)):
        return _binds_router(statement.target)
    if isinstance(statement, ast.Import):
        return any(
            (alias.asname or alias.name.split(".")[0]) == "router"
            for alias in statement.names
        )
    if isinstance(statement, ast.ImportFrom):
        return any(
            (alias.asname or alias.name) == "router"
            for alias in statement.names
        )
    return False


def validate_module(source: str) -> list[str]:
    """Check syntax, a top-level router binding, and compilation; never exec."""
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return [f"syntax error: {exc.msg} (line {exc.lineno})"]

    problems: list[str] = []
    if not any(_defines_router(statement) for statement in tree.body):
        problems.append("module defines no top-level 'router'")

    try:
        compile(source, "<llm-synthesised lists.py>", "exec")
    except Exception as exc:
        message = exc.msg if isinstance(exc, SyntaxError) else str(exc)
        problems.append(f"compile error: {message}")
    return problems


def synthesize(
    legacy_source: str,
    policy: dict,
    *,
    cmd: str | None = None,
    timeout: float = 300,
) -> str:
    """Run the LLM command and return its statically validated module source."""
    prompt = build_prompt(legacy_source, policy)
    command = cmd or os.environ.get("STRANGLER_LLM_CMD") or DEFAULT_LLM_CMD

    try:
        argv = shlex.split(command)
    except (TypeError, ValueError) as exc:
        raise SynthesisError(f"Invalid LLM command: {exc}") from exc
    if not argv:
        raise SynthesisError("LLM command not found: empty command")

    try:
        result = subprocess.run(
            argv,
            input=prompt,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (FileNotFoundError, PermissionError) as exc:
        raise SynthesisError(f"LLM command not found: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise SynthesisError(
            f"LLM command timed out after {timeout}s"
        ) from exc
    except Exception as exc:
        raise SynthesisError(f"LLM command failed: {exc}") from exc

    if result.returncode != 0:
        stderr_tail = " ".join((result.stderr or "")[-300:].split())
        raise SynthesisError(
            f"LLM command exited with status {result.returncode}: {stderr_tail}"
        )

    source = extract_module(result.stdout)
    try:
        problems = validate_module(source)
    except Exception as exc:
        raise SynthesisError(f"LLM module rejected: {exc}") from exc
    if problems:
        raise SynthesisError("LLM module rejected: " + "; ".join(problems))
    return source
