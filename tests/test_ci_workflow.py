"""Static contract checks for .github/workflows/e2e.yml (docs/AGENTS_SPEC.md section 6).

Actions cannot run here, so the workflow is verified structurally with ``yaml.safe_load``:

* triggers: push (main/master only, PRs run via pull_request) + pull_request; least-privilege token; one concurrency group per ref with cancel-in-progress;
* job ``unit``: checkout, setup-python 3.12, setup-uv, ``make install`` then ``make test``;
* job ``e2e`` (needs ``unit``, timeout 45 min): the docker-mode Switch bootstrap, ``make install``, ``make e2e`` in that
  order (``make e2e`` runs the driver with ``--fresh`` which provisions agents + room itself), and a
  ``failure() || cancelled()`` artifact upload (a job timeout reports as cancelled) carrying ``.state/run/*.log`` plus the ``docker compose logs`` output
  (collected with the same compose file / profiles / project directory the bootstrap used) but never ``switch.env``;
* every third-party action is pinned to a major version; every ``make <target>`` it calls exists in the Makefile.
"""
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "e2e.yml"
COMPOSE = "deploy/local/standalone-docker-compose.yml"


@pytest.fixture(scope="module")
def wf():
    return yaml.safe_load(WORKFLOW.read_text())


def steps(wf, job):
    return wf["jobs"][job]["steps"]


def run_text(step):
    return step.get("run", "")


def index_of(job_steps, predicate, what):
    for i, s in enumerate(job_steps):
        if predicate(s):
            return i
    raise AssertionError(f"no step for: {what}")


def has_cmd(step, cmd):
    return any(line.strip() == cmd or line.strip().endswith(" " + cmd) for line in run_text(step).splitlines())


def test_triggers_permissions_concurrency(wf):
    on = wf.get("on", wf.get(True))  # PyYAML (YAML 1.1) parses the bare key `on` as boolean True
    assert on is not None and "push" in on and "pull_request" in on
    assert set(on["push"]) == {"branches"} and set(on["push"]["branches"]) <= {"main", "master"}
    assert not on["pull_request"], "pull_request must not be filtered by branches/paths"
    assert wf["permissions"] == {"contents": "read"}
    conc = wf["concurrency"]
    assert "${{ github.ref }}" in conc["group"]
    assert conc["cancel-in-progress"] is True


def test_jobs_present_and_ordered(wf):
    assert set(wf["jobs"]) == {"unit", "e2e"}
    needs = wf["jobs"]["e2e"]["needs"]
    assert (needs if isinstance(needs, list) else [needs]) == ["unit"]
    assert wf["jobs"]["e2e"]["timeout-minutes"] == 45
    assert isinstance(wf["jobs"]["unit"].get("timeout-minutes"), int)
    for job in wf["jobs"].values():
        assert job["runs-on"] == "ubuntu-latest"


PINNED = {"actions/checkout": "v4", "actions/setup-python": "v5", "astral-sh/setup-uv": "v6",
          "actions/upload-artifact": "v4"}


def test_actions_pinned_to_major_versions(wf):
    for name, job in wf["jobs"].items():
        for s in job["steps"]:
            if "uses" in s:
                action, _, ver = s["uses"].partition("@")
                assert PINNED.get(action) == ver, (name, s["uses"])


FAILURE_IF = {"failure()", "failure() || cancelled()"}


def test_nothing_masks_or_skips_the_gates(wf):
    """No continue-on-error / if / odd shell / working-directory on the jobs or on the gating steps."""
    for name, job in wf["jobs"].items():
        assert "continue-on-error" not in job and "if" not in job, name
        for s in job["steps"]:
            for key in ("shell", "working-directory", "continue-on-error"):
                assert key not in s, (name, s)
            gating = any(c in run_text(s) for c in ("make ", "bootstrap_switch"))
            if gating:
                assert "if" not in s, (name, s)
                assert "||" not in run_text(s) and "&&" not in run_text(s), (name, s)
    for job in wf["jobs"].values():
        job_steps = job["steps"]
        for s in job_steps:
            if "if" in s:
                assert str(s["if"]).split("#")[0].strip() in FAILURE_IF, s
        assert sum(s.get("timeout-minutes", 0) for s in job_steps) < job["timeout-minutes"]


def test_no_secret_exposure_in_run_steps(wf):
    for job in wf["jobs"].values():
        for s in job["steps"]:
            assert not re.search(r"switch\.env|\.env\b|cat \.state|printenv|\benv\b\s*$", run_text(s), re.M), s


@pytest.mark.parametrize("job", ["unit", "e2e"])
def test_toolchain_steps(wf, job):
    st = steps(wf, job)
    assert st[0]["uses"].startswith("actions/checkout@v")
    py = [s for s in st if s.get("uses", "").startswith("actions/setup-python@v")]
    assert len(py) == 1 and str(py[0]["with"]["python-version"]) == "3.12"
    assert any(s.get("uses", "").startswith("astral-sh/setup-uv@v") for s in st)
    i_py = st.index(py[0])
    i_install = index_of(st, lambda s: has_cmd(s, "make install"), "make install")
    assert i_py < i_install


def test_unit_job_runs_install_then_test(wf):
    st = steps(wf, "unit")
    i_install = index_of(st, lambda s: has_cmd(s, "make install"), "make install")
    i_test = index_of(st, lambda s: has_cmd(s, "make test"), "make test")
    assert i_install < i_test
    assert not any("bootstrap_switch" in run_text(s) for s in st), "unit job must not need Docker/Switch"


def bootstrap_step(st):
    # `bash` rather than relying on the exec bit surviving checkout
    return index_of(st, lambda s: run_text(s).strip() == "bash scripts/bootstrap_switch.sh", "bootstrap_switch.sh")


def test_e2e_bootstraps_docker_switch_before_e2e(wf):
    st = steps(wf, "e2e")
    i_boot = bootstrap_step(st)
    step = st[i_boot]
    assert step.get("env") == {"SWITCH_MODE": "docker"}
    i_install = index_of(st, lambda s: has_cmd(s, "make install"), "make install")
    i_e2e = index_of(st, lambda s: has_cmd(s, "make e2e"), "make e2e")
    assert i_boot < i_e2e and i_install < i_e2e
    assert "if" not in st[i_e2e] or "failure()" not in str(st[i_e2e]["if"])
    assert isinstance(step.get("timeout-minutes"), int) and step["timeout-minutes"] <= 30


def test_e2e_driver_provisions_itself():
    # CI does not run `make provision`: `make e2e` must run the driver with --fresh (provision --fresh-room).
    mk = (ROOT / "Makefile").read_text() if (ROOT / "Makefile").exists() else ""
    if not mk:
        pytest.skip("Makefile not present yet")
    recipe = re.search(r"^e2e:.*\n((?:\t.*\n?)+)", mk, re.M).group(1)
    assert "run_e2e_demo.py" in recipe and "--fresh" in recipe
    assert (ROOT / "scripts" / "run_e2e_demo.py").exists()


def test_failure_artifacts(wf):
    st = steps(wf, "e2e")
    logs = [s for s in st if "docker compose" in run_text(s) and " logs " in run_text(s)]
    assert len(logs) == 1
    s = logs[0]
    assert str(s["if"]).split("#")[0].strip() in FAILURE_IF
    cmd = run_text(s)
    compose_lines = [line for line in cmd.splitlines() if "docker compose" in line]
    assert compose_lines
    for line in compose_lines:  # without the profiles, collab/gateway services are not listed
        assert f"-f .switch-infra/{COMPOSE}" in line and "--project-directory .switch-infra" in line, line
        assert "--profile collab" in line and "--profile gateway" in line, line
    ups = [x for x in st if x.get("uses", "").startswith("actions/upload-artifact@v")]
    assert len(ups) == 1
    up = ups[0]
    assert str(up["if"]).split("#")[0].strip() in FAILURE_IF
    paths = up["with"]["path"]
    assert ".state/run/*.log" in paths
    out = re.search(r"(\.state/[\w./-]+\.log)", cmd.split(" logs ", 1)[1])
    assert out and out.group(1) in paths, "compose log output file must be part of the uploaded artifact"
    listed = [line.strip() for line in paths.splitlines() if line.strip()]  # allow-list: no credentials possible
    assert all(re.fullmatch(r"\.state/(run|ci)/[\w.*-]+\.log", p) for p in listed), listed
    # upload-artifact@v4 silently skips hidden files/dirs (".state") unless told otherwise
    assert up["with"].get("include-hidden-files") is True
    assert up["with"].get("if-no-files-found") in ("warn", "ignore")
    assert st.index(s) < st.index(up)
    # the log collection must not mask the original failure nor abort before upload
    assert "|| true" in cmd or s.get("continue-on-error") is True


def test_make_targets_exist(wf):
    if not (ROOT / "Makefile").exists() or shutil.which("make") is None:
        pytest.skip("Makefile not present yet")
    targets = set()
    for job in wf["jobs"].values():
        for s in job["steps"]:
            targets |= set(re.findall(r"^\s*make (\S+)\s*$", run_text(s), re.M))
    assert {"install", "test", "e2e"} <= targets
    for t in sorted(targets):
        r = subprocess.run(["make", "-n", t], cwd=ROOT, capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, (t, r.stderr)
