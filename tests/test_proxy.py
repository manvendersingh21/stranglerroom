"""Contract tests for the strangler reverse proxy (docs/SPEC.md §6)."""
import json
import os

import httpx
import pytest

from tests.procs import ROOT, free_port, service


@pytest.fixture()
def stack(tmp_path):
    legacy_port, modern_port, proxy_port = free_port(), free_port(), free_port()
    state = tmp_path / "router.json"
    with service("tests.fake_upstream", legacy_port, health="/", args=["--name", "legacy"]) as lu, \
         service("tests.fake_upstream", modern_port, health="/", args=["--name", "modern"]) as mu, \
         service("targets.proxy.router", proxy_port, health="/_router/status",
                 env={"LEGACY_URL": lu, "MODERN_URL": mu, "ROUTER_STATE_FILE": str(state),
                      "ROUTER_ADMIN_TOKEN": "t0k"}) as pu:
        yield pu, state


def test_defaults_to_legacy_and_forwards(stack):
    pu, _ = stack
    r = httpx.get(pu + "/people/george08/lists.json?limit=none&x=1")
    assert r.status_code == 200
    assert r.headers["x-strangler-upstream"] == "legacy"
    body = r.json()
    assert body["served_by"] == "legacy" and body["path"] == "/people/george08/lists.json"
    assert body["query"] == "limit=none&x=1" and body["method"] == "GET"
    st = httpx.get(pu + "/_router/status").json()
    assert st["target"] == "legacy" and st["upstream_url"].endswith(str(httpx.URL(st["upstream_url"]).port))


def test_post_body_and_status_forwarded(stack):
    pu, _ = stack
    r = httpx.post(pu + "/echo/status/418", json={"add": ["/works/OL1W"]})
    assert r.status_code == 418
    assert r.json()["body"] == json.dumps({"add": ["/works/OL1W"]}, separators=(",", ":")) or json.loads(r.json()["body"]) == {"add": ["/works/OL1W"]}


def test_flip_requires_token(stack):
    pu, _ = stack
    assert httpx.post(pu + "/_router/flip", json={"target": "modern", "approved_by": "user", "evidence": "x"}).status_code == 403
    assert httpx.post(pu + "/_router/flip", headers={"X-Router-Token": "nope"},
                      json={"target": "modern", "approved_by": "user", "evidence": "x"}).status_code == 403
    assert httpx.post(pu + "/_router/flip", headers={"X-Router-Token": "t0k"},
                      json={"target": "mars", "approved_by": "user", "evidence": "x"}).status_code == 400


def test_flip_to_modern_persists(stack):
    pu, state = stack
    r = httpx.post(pu + "/_router/flip", headers={"X-Router-Token": "t0k"},
                   json={"target": "modern", "approved_by": "user", "evidence": "parity 5/5"})
    assert r.status_code == 200
    s = r.json()
    assert s["target"] == "modern" and s["approved_by"] == "user" and s["evidence"] == "parity 5/5" and s["flipped_at"]
    g = httpx.get(pu + "/anything")
    assert g.headers["x-strangler-upstream"] == "modern" and g.json()["served_by"] == "modern"
    assert json.loads(state.read_text())["target"] == "modern"


def test_upstream_down_502(tmp_path):
    proxy_port = free_port()
    with service("targets.proxy.router", proxy_port, health="/_router/status",
                 env={"LEGACY_URL": f"http://127.0.0.1:{free_port()}", "MODERN_URL": "http://127.0.0.1:1",
                      "ROUTER_STATE_FILE": str(tmp_path / "r.json"),
                      "ROUTER_TOKEN_FILE": str(tmp_path / "router.token")}) as pu:
        r = httpx.get(pu + "/people/george08/lists")
        assert r.status_code == 502 and r.json() == {"error": "upstream_unavailable"}


def _flip(pu, **body):
    return httpx.post(pu + "/_router/flip", headers={"X-Router-Token": "t0k"}, json=body)


def test_shared_client_never_leaks_cookies_between_callers(stack):
    pu, _ = stack
    first = httpx.get(pu + "/echo/setcookie")
    assert "session=secret-of-first-caller" in first.headers.get("set-cookie", "")
    second = httpx.get(pu + "/people/george08/lists")          # a different caller, no cookies
    assert second.json()["cookie"] is None
    assert _flip(pu, target="modern", approved_by="user", evidence="x").status_code == 200
    third = httpx.get(pu + "/people/george08/lists")           # and none crosses to the other upstream
    assert third.json()["served_by"] == "modern" and third.json()["cookie"] is None
    own = httpx.get(pu + "/x", headers={"Cookie": "mine=1"})   # a caller's own cookie still travels
    assert own.json()["cookie"] == "mine=1"


@pytest.mark.parametrize("sent, forwarded", [
    ("a=1; MMAUTHTOKEN=human-session; b=2", "a=1; b=2"),
    ("MMAUTHTOKEN=s; MMUSERID=u; MMCSRF=c; switch_auth=admin", None),   # nothing left: no Cookie header at all
    ("switch_auth=admin; keep=yes", "keep=yes"),
    ("mmauthtoken=s;x=1", "x=1"),                                      # names compared case-insensitively
    ("a=1; b=MMAUTHTOKEN", "a=1; b=MMAUTHTOKEN"),                      # only the cookie NAME matters
])
def test_credential_cookies_never_reach_an_upstream(stack, sent, forwarded):
    """Browsers scope cookies by host, not port: the human's Mattermost session and the Gateway admin's cookie
    arrive at the proxy too, and must not reach the upstream (which serves translator-generated code)."""
    pu, _ = stack
    for target in ("legacy", "modern"):
        if target == "modern":
            assert _flip(pu, target="modern", approved_by="user", evidence="x").status_code == 200
        body = httpx.get(pu + "/x", headers={"Cookie": sent}).json()
        assert body["served_by"] == target and body["cookie"] == forwarded, (target, body)


@pytest.mark.parametrize("target", [[], {}, 1, None])
def test_flip_rejects_non_string_target(stack, target):
    pu, _ = stack
    assert _flip(pu, target=target, approved_by="user", evidence="x").status_code == 400


@pytest.mark.parametrize("body", [{"target": "modern"}, {"target": "modern", "approved_by": None, "evidence": "e"},
                                  {"target": "modern", "approved_by": "user", "evidence": ""},
                                  {"target": "modern", "approved_by": 3, "evidence": "e"}])
def test_flip_requires_approver_and_evidence(stack, body):
    pu, _ = stack
    r = _flip(pu, **body)
    assert r.status_code == 400 and r.json()["error"] == "approved_by_and_evidence_required"
    assert httpx.get(pu + "/_router/status").json()["target"] == "legacy"


def test_corrupt_persisted_target_falls_back_to_legacy(tmp_path):
    state = tmp_path / "r.json"
    state.write_text(json.dumps({"target": ["modern"]}))
    with service("targets.proxy.router", free_port(), health="/_router/status",
                 env={"ROUTER_STATE_FILE": str(state), "LEGACY_URL": "http://127.0.0.1:1",
                      "MODERN_URL": "http://127.0.0.1:1", "ROUTER_TOKEN_FILE": str(tmp_path / "router.token")}) as pu:
        assert httpx.get(pu + "/_router/status").json()["target"] == "legacy"


def test_non_ascii_header_bytes_are_forwarded(stack):
    pu, _ = stack
    r = httpx.get(pu + "/x", headers={"X-Probe": "caf\xe9".encode("latin-1")})
    assert r.status_code == 200 and r.json()["probe"] == "café"


def test_encoded_body_passes_through_untouched(stack):
    pu, _ = stack
    with httpx.Client() as c:
        r = c.get(pu + "/echo/gzip", headers={"Accept-Encoding": "gzip"})
    assert r.headers["content-encoding"] == "gzip"
    assert r.json()["path"] == "/echo/gzip"                    # the client decoded the still-gzipped bytes
    raw = httpx.get(pu + "/echo/gzip", headers={"Accept-Encoding": "identity"})
    assert raw.headers["content-encoding"] == "gzip" and int(raw.headers["content-length"]) > 0


def test_no_public_default_token(tmp_path):
    """Without ROUTER_ADMIN_TOKEN the proxy uses a random per-deployment token (0600) that the gate resolves too."""
    import os

    from targets.common.router_token import resolve_router_token

    env = {k: v for k, v in os.environ.items() if k not in ("ROUTER_ADMIN_TOKEN", "ROUTER_TOKEN_FILE")}
    env.update({"STRANGLER_STATE_DIR": str(tmp_path), "ROUTER_STATE_FILE": str(tmp_path / "elsewhere" / "r.json"),
                "LEGACY_URL": "http://127.0.0.1:1", "MODERN_URL": "http://127.0.0.1:1"})
    with service("targets.proxy.router", free_port(), health="/_router/status", env=env) as pu:
        body = {"target": "modern", "approved_by": "user", "evidence": "e"}
        assert httpx.post(pu + "/_router/flip", headers={"X-Router-Token": "strangler-local-token"},
                          json=body).status_code == 403
        token_file = tmp_path / "router.token"          # the state dir, NOT the router state file's dir
        assert token_file.stat().st_mode & 0o777 == 0o600
        token = resolve_router_token({"STRANGLER_STATE_DIR": str(tmp_path)})   # what @cutover-gate resolves
        assert len(token) == 48 and token == token_file.read_text().strip()
        assert httpx.post(pu + "/_router/flip", headers={"X-Router-Token": token}, json=body).status_code == 200


def test_router_token_resolution(tmp_path):
    import threading

    from targets.common.router_token import resolve_router_token, token_path

    assert resolve_router_token({"ROUTER_ADMIN_TOKEN": " explicit ", "STRANGLER_STATE_DIR": str(tmp_path)}) == "explicit"
    assert not (tmp_path / "router.token").exists()             # an explicit token never writes the file
    assert token_path({"ROUTER_TOKEN_FILE": str(tmp_path / "t")}) == tmp_path / "t"
    got: list[str] = []
    env = {"STRANGLER_STATE_DIR": str(tmp_path / "s")}
    threads = [threading.Thread(target=lambda: got.append(resolve_router_token(env))) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(set(got)) == 1 and len(got[0]) == 48            # racing creators agree on one token


def test_proxy_and_gate_resolve_the_same_token_under_every_override(tmp_path, monkeypatch):
    """codex lead-final-delta #1/#3: the proxy (RouterConfig) and @cutover-gate must never disagree."""
    from targets.common.router_token import resolve_router_token
    from targets.proxy.router import RouterConfig

    variants = [
        {"STRANGLER_STATE_DIR": str(tmp_path / "a")},
        {"STRANGLER_STATE_DIR": str(tmp_path / "b"), "ROUTER_STATE_FILE": str(tmp_path / "other" / "r.json")},
        {"ROUTER_TOKEN_FILE": str(tmp_path / "c" / "tok"), "ROUTER_STATE_FILE": str(tmp_path / "x" / "r.json")},
        {"ROUTER_ADMIN_TOKEN": "  padded-token \n", "STRANGLER_STATE_DIR": str(tmp_path / "d")},
    ]
    for env in variants:
        proxy_side = RouterConfig.from_env(env).admin_token
        assert proxy_side == resolve_router_token(env), env
    assert RouterConfig.from_env(variants[-1]).admin_token == "padded-token"


def test_cutover_gate_uses_the_shared_resolver(tmp_path, monkeypatch):
    from agents.cutover_agent import CutoverAgent
    from agents.provision import RoomInfo
    from agents.switch_client import Settings
    from tests.fakes import FakeRoom

    room = FakeRoom()
    info = RoomInfo(room_id=room.room_id, name="strangler-migration", channel_id="c", channel_url=None,
                    agents=room.agent_ids, human=room.human)
    settings = Settings(*(["unused"] * 8))
    monkeypatch.setenv("ROUTER_ADMIN_TOKEN", "  padded \t")
    gate = CutoverAgent(settings=settings, room=info, switch=room.agent("cutover-gate"))
    assert gate._router_token == "padded"
    monkeypatch.delenv("ROUTER_ADMIN_TOKEN")
    monkeypatch.setenv("STRANGLER_STATE_DIR", str(tmp_path))
    gate = CutoverAgent(settings=settings, room=info, switch=room.agent("cutover-gate"))
    assert gate._router_token == (tmp_path / "router.token").read_text().strip()


def test_token_creation_leaves_no_temp_files(tmp_path):
    from targets.common.router_token import resolve_router_token

    token = resolve_router_token({"STRANGLER_STATE_DIR": str(tmp_path)})
    assert sorted(p.name for p in tmp_path.iterdir()) == ["router.token"] and len(token) == 48
    assert resolve_router_token({"STRANGLER_STATE_DIR": str(tmp_path)}) == token     # stable once published


def test_an_unsafe_token_file_is_refused(tmp_path):
    """codex re-review #3: a token others can read (or a symlink / non-file) must never be used."""
    import os

    import pytest

    from targets.common.router_token import resolve_router_token

    loose = tmp_path / "loose.token"
    loose.write_text("abc\n")
    os.chmod(loose, 0o644)
    with pytest.raises(RuntimeError, match="group/others"):
        resolve_router_token({"ROUTER_TOKEN_FILE": str(loose)})
    target = tmp_path / "private.token"
    target.write_text("abc\n")
    os.chmod(target, 0o600)
    link = tmp_path / "link.token"
    link.symlink_to(target)
    with pytest.raises(RuntimeError, match="symlinks are refused"):
        resolve_router_token({"ROUTER_TOKEN_FILE": str(link)})
    (tmp_path / "dir.token").mkdir()
    with pytest.raises(RuntimeError, match="not a regular file"):
        resolve_router_token({"ROUTER_TOKEN_FILE": str(tmp_path / "dir.token")})
    assert resolve_router_token({"ROUTER_TOKEN_FILE": str(target)}) == "abc"


def test_a_fifo_token_path_is_refused_without_hanging(tmp_path):
    """codex final re-review #2: opening a FIFO with no writer must not block startup."""
    import os
    import threading

    import pytest

    from targets.common.router_token import resolve_router_token

    fifo = tmp_path / "fifo.token"
    os.mkfifo(fifo, 0o600)
    outcome: list = []

    def run():
        try:
            resolve_router_token({"ROUTER_TOKEN_FILE": str(fifo)})
        except RuntimeError as exc:
            outcome.append(str(exc))

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(5)
    assert not t.is_alive(), "resolving a FIFO token path hung"
    assert outcome and "not a regular file" in outcome[0]
