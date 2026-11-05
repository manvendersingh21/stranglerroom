"""Contract tests for the async load benchmark library used by @cutover-gate.

API (agents/bench.py):
    async def run_benchmark(base_url: str, paths: list[str], total: int = 500, concurrency: int = 50,
                            timeout: float = 10.0) -> BenchResult
    def compare(legacy: BenchResult, modern: BenchResult) -> dict
    def render_card(legacy: BenchResult, modern: BenchResult) -> str   # Markdown table

BenchResult is a dataclass with fields: base_url, total, concurrency, ok (2xx/3xx/4xx count), errors
(5xx + transport errors), duration_s, throughput_rps, p50_ms, p95_ms, p99_ms, max_ms, mean_ms,
status_counts (dict[int,int]) and a to_dict() method. Percentiles use nearest-rank on per-request
latency in milliseconds, rounded to 2 decimals. Requests cycle through `paths` round-robin.
compare() returns {"p50_delta_pct", "p95_delta_pct", "p99_delta_pct", "throughput_delta_pct",
"modern_faster_p99": bool, "error_rate_legacy", "error_rate_modern"} where delta_pct =
round((modern - legacy) / legacy * 100, 1) (0.0 when legacy is 0).
"""
import asyncio
import math

import httpx
import pytest

from agents.bench import BenchResult, compare, render_card, run_benchmark, percentile
from tests.procs import free_port, service


def test_percentile_nearest_rank():
    data = list(range(1, 101))
    assert percentile(data, 50) == 50
    assert percentile(data, 95) == 95
    assert percentile(data, 99) == 99
    assert percentile([7.0], 99) == 7.0
    assert percentile([], 50) == 0.0


@pytest.fixture(scope="module")
def upstream():
    with service("tests.fake_upstream", free_port(), health="/", args=["--name", "x"]) as u:
        yield u


def test_run_benchmark_counts(upstream):
    res = asyncio.run(run_benchmark(upstream, ["/a", "/b", "/echo/status/503"], total=60, concurrency=10))
    assert isinstance(res, BenchResult)
    assert res.total == 60 and res.concurrency == 10
    assert res.ok == 40 and res.errors == 20
    assert res.status_counts == {200: 40, 503: 20}
    assert 0 < res.p50_ms <= res.p95_ms <= res.p99_ms <= res.max_ms
    assert res.throughput_rps > 0 and res.duration_s > 0
    d = res.to_dict()
    assert d["p99_ms"] == res.p99_ms and d["status_counts"] == {200: 40, 503: 20} or d["status_counts"] == {"200": 40, "503": 20}


def test_transport_errors_counted():
    res = asyncio.run(run_benchmark(f"http://127.0.0.1:{free_port()}", ["/"], total=5, concurrency=2, timeout=1))
    assert res.errors == 5 and res.ok == 0


def _r(**kw):
    base = dict(base_url="u", total=100, concurrency=10, ok=100, errors=0, duration_s=1.0, throughput_rps=100.0,
                p50_ms=10.0, p95_ms=20.0, p99_ms=40.0, max_ms=50.0, mean_ms=12.0, status_counts={200: 100})
    base.update(kw)
    return BenchResult(**base)


def test_compare_and_card():
    legacy = _r()
    modern = _r(p50_ms=5.0, p95_ms=10.0, p99_ms=10.0, throughput_rps=250.0, errors=1)
    c = compare(legacy, modern)
    assert c["p50_delta_pct"] == -50.0 and c["p99_delta_pct"] == -75.0
    assert c["throughput_delta_pct"] == 150.0 and c["modern_faster_p99"] is True
    assert math.isclose(c["error_rate_modern"], 0.01) and c["error_rate_legacy"] == 0.0
    card = render_card(legacy, modern)
    assert "p99" in card and "| " in card and "40.0" in card and "10.0" in card
    assert compare(_r(p50_ms=0.0), _r())["p50_delta_pct"] == 0.0


def test_transport_failures_count_toward_latency():
    """Timeouts must not vanish from the percentiles (a timing-out service is not 'faster')."""
    res = asyncio.run(run_benchmark(f"http://127.0.0.1:{free_port()}", ["/"], total=4, concurrency=2, timeout=1))
    assert res.errors == 4 and res.p99_ms > 0 and res.max_ms >= res.p99_ms


def test_caller_bugs_propagate():
    """A malformed request is a bug in the caller, not a transport error to be counted."""
    with pytest.raises(httpx.InvalidURL):
        asyncio.run(run_benchmark("http://127.0.0.1:1", ["/a\x00b"], total=1, concurrency=1, timeout=1))


def test_card_counts_the_virtual_users_actually_used():
    card = render_card(_r(total=8, concurrency=50), _r(total=8, concurrency=50))
    assert "from 8 virtual users" in card


def test_card_never_reports_fewer_than_one_virtual_user():
    card = render_card(_r(total=8, concurrency=0), _r(total=8, concurrency=0))
    assert "from 1 virtual users" in card
