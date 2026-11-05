"""Async load benchmark library used by @cutover-gate.

This module provides async benchmarking utilities to measure HTTP throughput and latency
percentiles, compare legacy and modern implementations, and render comparison cards as
Markdown tables for cutover audit ledgers.

API:
    BenchResult: dataclass with performance metrics and a to_dict() method.
    percentile: compute nearest-rank percentile for a sequence of values.
    run_benchmark: execute an async benchmark against a target URL.
    compare: compare two BenchResult instances.
    render_card: generate a Markdown table comparing two BenchResult instances.
"""
from __future__ import annotations

import argparse
import asyncio
import math
import time
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from typing import Any

import httpx

__all__ = [
    "BenchResult",
    "percentile",
    "run_benchmark",
    "compare",
    "render_card",
    "main",
]


@dataclass
class BenchResult:
    """Benchmark results for a target service.

    Attributes:
        base_url: Base URL that was benchmarked.
        total: Total number of requests executed.
        concurrency: Concurrency limit used during the run.
        ok: Count of 2xx, 3xx, and 4xx responses.
        errors: Count of 5xx responses plus transport errors.
        duration_s: Total benchmark wall-clock time in seconds.
        throughput_rps: Requests per second (total / duration_s).
        p50_ms: 50th percentile per-request latency in milliseconds.
        p95_ms: 95th percentile per-request latency in milliseconds.
        p99_ms: 99th percentile per-request latency in milliseconds.
        max_ms: Maximum per-request latency in milliseconds.
        mean_ms: Arithmetic mean per-request latency in milliseconds.
        status_counts: Mapping from HTTP status code to count.
    """

    base_url: str
    total: int
    concurrency: int
    ok: int
    errors: int
    duration_s: float
    throughput_rps: float
    p50_ms: float
    p95_ms: float
    p99_ms: float
    max_ms: float
    mean_ms: float
    status_counts: dict[int, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Convert the benchmark result to a dictionary."""
        return asdict(self)


def percentile(values: Sequence[float | int], p: float) -> float:
    """Compute the p-th percentile of values using the nearest-rank method.

    The nearest-rank method sorts the sequence and selects the item at:
        index = ceil(p / 100 * n) - 1 (clamped to [0, n - 1])
    Returns 0.0 for an empty sequence.

    Args:
        values: Sequence of numerical values.
        p: Percentile rank (0.0 to 100.0).

    Returns:
        The nearest-rank percentile value, rounded to 2 decimals.
    """
    if not values:
        return 0.0
    s = sorted(values)
    n = len(s)
    idx = math.ceil((p / 100.0) * n) - 1
    idx = max(0, min(n - 1, idx))
    return round(float(s[idx]), 2)


async def run_benchmark(
    base_url: str,
    paths: list[str],
    total: int = 500,
    concurrency: int = 50,
    timeout: float = 10.0,
) -> BenchResult:
    """Run an async load benchmark against base_url cycling through paths round-robin.

    Closed-loop: ``concurrency`` virtual users, each owning one keep-alive connection (opened in an
    unmeasured warm-up), issue requests back to back until ``total`` are done. Measures each request with
    time.perf_counter.

    Args:
        base_url: Target base URL (e.g. 'http://127.0.0.1:9001').
        paths: Paths to query round-robin (e.g. ['/a', '/b']).
        total: Total number of requests to execute (default: 500).
        concurrency: Max concurrent requests (default: 50).
        timeout: Request timeout in seconds (default: 10.0).

    Returns:
        BenchResult containing aggregate latency, throughput, and error counts.
    """
    effective_concurrency = max(1, concurrency)
    query_paths = paths or ["/"]

    if total <= 0:
        return BenchResult(
            base_url=base_url,
            total=0,
            concurrency=concurrency,
            ok=0,
            errors=0,
            duration_s=0.0,
            throughput_rps=0.0,
            p50_ms=0.0,
            p95_ms=0.0,
            p99_ms=0.0,
            max_ms=0.0,
            mean_ms=0.0,
            status_counts={},
        )

    clean_base_url = base_url
    if not (clean_base_url.startswith("http://") or clean_base_url.startswith("https://")):
        clean_base_url = f"http://{clean_base_url}"

    # Closed-loop load generation with `concurrency` virtual users, each owning ONE keep-alive
    # connection (its own client) and issuing requests back to back, like wrk/locust. A single shared
    # httpx pool is NOT used on purpose: httpcore re-scans every queued request against every pooled
    # connection on each event, an O(requests x connections) cost that at concurrency 50 saturates the
    # load generator itself (profiled: ~4k is_idle() calls per request) and so measures the client, not
    # the server — penalising precisely the server that keeps every connection alive.
    results: list[tuple[int | None, float]] = [(None, 0.0)] * total
    next_index = iter(range(total))
    one_conn = httpx.Limits(max_connections=1, max_keepalive_connections=1)

    async def virtual_user(client: httpx.AsyncClient) -> None:
        for idx in next_index:  # shared iterator: each request index is taken exactly once
            path = query_paths[idx % len(query_paths)]
            t0 = time.perf_counter()
            try:
                resp = await client.get(path)
            except httpx.TransportError:
                # A timeout / refused connection is a failed request whose latency still counts:
                # dropping it would make a service that times out look *faster* (p99 = 0).
                # Anything else (e.g. an invalid URL) is a caller bug and propagates.
                results[idx] = (None, (time.perf_counter() - t0) * 1000.0)
                continue
            results[idx] = (resp.status_code, (time.perf_counter() - t0) * 1000.0)

    clients: list[httpx.AsyncClient] = []
    try:
        for _ in range(min(effective_concurrency, total)):
            clients.append(httpx.AsyncClient(base_url=clean_base_url, limits=one_conn, timeout=timeout,
                                             trust_env=False))
        # Warm-up (not measured): every virtual user opens its keep-alive connection first, so the timed
        # phase measures steady-state request latency rather than a 50-way TCP connect storm (whose SYN
        # retransmits, 1 s / 3 s on Linux, would dominate p95/p99 for any server with a short accept
        # backlog). The strangler proxy talks to its upstreams over persistent connections too.
        await asyncio.gather(*(c.get(query_paths[0]) for c in clients), return_exceptions=True)
        start_time = time.perf_counter()
        await asyncio.gather(*(virtual_user(c) for c in clients))
        raw_duration = time.perf_counter() - start_time
    finally:
        await asyncio.gather(*(c.aclose() for c in clients), return_exceptions=True)

    latencies: list[float] = []
    status_counts: dict[int, int] = {}
    ok = 0
    errors = 0

    for status_code, lat_ms in results:
        latencies.append(lat_ms)
        if status_code is None:
            errors += 1
            continue
        status_counts[status_code] = status_counts.get(status_code, 0) + 1
        if 200 <= status_code < 500:
            ok += 1
        else:
            errors += 1

    duration_s = round(raw_duration, 4)
    throughput_rps = round(total / raw_duration, 1) if raw_duration > 0 else 0.0

    p50_ms = percentile(latencies, 50)
    p95_ms = percentile(latencies, 95)
    p99_ms = percentile(latencies, 99)
    max_ms = round(max(latencies), 2) if latencies else 0.0
    mean_ms = round(sum(latencies) / len(latencies), 2) if latencies else 0.0

    return BenchResult(
        base_url=base_url,
        total=total,
        concurrency=concurrency,
        ok=ok,
        errors=errors,
        duration_s=duration_s,
        throughput_rps=throughput_rps,
        p50_ms=p50_ms,
        p95_ms=p95_ms,
        p99_ms=p99_ms,
        max_ms=max_ms,
        mean_ms=mean_ms,
        status_counts=status_counts,
    )


def _delta_pct(legacy_val: float, modern_val: float) -> float:
    """Calculate percentage delta: round((modern - legacy) / legacy * 100, 1), 0.0 if legacy is 0."""
    if legacy_val == 0.0:
        return 0.0
    return round((modern_val - legacy_val) / legacy_val * 100.0, 1)


def compare(legacy: BenchResult, modern: BenchResult) -> dict[str, Any]:
    """Compare legacy and modern benchmark results.

    Returns:
        dict with keys:
            - p50_delta_pct: float
            - p95_delta_pct: float
            - p99_delta_pct: float
            - throughput_delta_pct: float
            - modern_faster_p99: bool
            - error_rate_legacy: float
            - error_rate_modern: float
    """
    error_rate_legacy = (legacy.errors / legacy.total) if legacy.total > 0 else 0.0
    error_rate_modern = (modern.errors / modern.total) if modern.total > 0 else 0.0

    return {
        "p50_delta_pct": _delta_pct(legacy.p50_ms, modern.p50_ms),
        "p95_delta_pct": _delta_pct(legacy.p95_ms, modern.p95_ms),
        "p99_delta_pct": _delta_pct(legacy.p99_ms, modern.p99_ms),
        "throughput_delta_pct": _delta_pct(legacy.throughput_rps, modern.throughput_rps),
        "modern_faster_p99": modern.p99_ms < legacy.p99_ms,
        "error_rate_legacy": error_rate_legacy,
        "error_rate_modern": error_rate_modern,
    }


def _fmt_pct(val: float) -> str:
    """Format percentage with explicit + for positive values."""
    if abs(val) < 1e-9:
        return "0.0%"
    if val > 0:
        return f"+{val:.1f}%"
    return f"{val:.1f}%"


def render_card(legacy: BenchResult, modern: BenchResult | None = None) -> str:
    """Render a Markdown table card with latency, throughput, errors, and delta.

    Args:
        legacy: Baseline benchmark result.
        modern: Optional comparison benchmark result.

    Returns:
        Markdown table string.
    """
    if modern is None:
        lines = [
            "| Metric | Value |",
            "| --- | --- |",
            f"| p50 latency (ms) | {legacy.p50_ms:.1f} |",
            f"| p95 latency (ms) | {legacy.p95_ms:.1f} |",
            f"| p99 latency (ms) | {legacy.p99_ms:.1f} |",
            f"| max latency (ms) | {legacy.max_ms:.1f} |",
            f"| mean latency (ms) | {legacy.mean_ms:.1f} |",
            f"| throughput (req/s) | {legacy.throughput_rps:.1f} |",
            f"| errors | {legacy.errors} |",
        ]
        return "\n".join(lines)

    c = compare(legacy, modern)
    max_delta = _fmt_pct(_delta_pct(legacy.max_ms, modern.max_ms))
    mean_delta = _fmt_pct(_delta_pct(legacy.mean_ms, modern.mean_ms))
    err_diff = modern.errors - legacy.errors
    err_delta = f"+{err_diff}" if err_diff > 0 else (f"{err_diff}" if err_diff < 0 else "0")

    lines = [
        "| Metric | Legacy | Modern | Delta |",
        "| --- | --- | --- | --- |",
        f"| p50 latency (ms) | {legacy.p50_ms:.1f} | {modern.p50_ms:.1f} | {_fmt_pct(c['p50_delta_pct'])} |",
        f"| p95 latency (ms) | {legacy.p95_ms:.1f} | {modern.p95_ms:.1f} | {_fmt_pct(c['p95_delta_pct'])} |",
        f"| p99 latency (ms) | {legacy.p99_ms:.1f} | {modern.p99_ms:.1f} | {_fmt_pct(c['p99_delta_pct'])} |",
        f"| max latency (ms) | {legacy.max_ms:.1f} | {modern.max_ms:.1f} | {max_delta} |",
        f"| mean latency (ms) | {legacy.mean_ms:.1f} | {modern.mean_ms:.1f} | {mean_delta} |",
        f"| throughput (req/s) | {legacy.throughput_rps:.1f} | {modern.throughput_rps:.1f} | {_fmt_pct(c['throughput_delta_pct'])} |",
        f"| errors | {legacy.errors} | {modern.errors} | {err_delta} |",
        "",
        f"_Closed-loop load: {modern.total} requests per target from "
        f"{min(max(1, modern.concurrency), max(1, modern.total))} "
        f"virtual users, each on "
        f"one keep-alive connection opened during an unmeasured warm-up; targets benchmarked one after the "
        f"other._",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> None:
    """Command-line entry point for running benchmarks and printing cards."""
    parser = argparse.ArgumentParser(
        prog="python -m agents.bench",
        description="Async load benchmark library for @cutover-gate",
    )
    parser.add_argument("url", help="Primary / legacy URL to benchmark")
    parser.add_argument(
        "url2",
        nargs="?",
        default=None,
        help="Optional secondary / modern URL to compare against",
    )
    parser.add_argument("--total", type=int, default=500, help="Total requests (default: 500)")
    parser.add_argument("--concurrency", type=int, default=50, help="Concurrency limit (default: 50)")
    parser.add_argument(
        "--paths",
        default="/",
        help="Comma-separated paths to benchmark round-robin (e.g. /a,/b)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=10.0,
        help="Request timeout in seconds (default: 10.0)",
    )

    args = parser.parse_args(argv)
    raw_paths = [p.strip() for p in args.paths.split(",") if p.strip()]
    paths = raw_paths or ["/"]

    if args.url2:
        legacy_res = asyncio.run(
            run_benchmark(
                args.url,
                paths=paths,
                total=args.total,
                concurrency=args.concurrency,
                timeout=args.timeout,
            )
        )
        modern_res = asyncio.run(
            run_benchmark(
                args.url2,
                paths=paths,
                total=args.total,
                concurrency=args.concurrency,
                timeout=args.timeout,
            )
        )
        print(render_card(legacy_res, modern_res))
    else:
        res = asyncio.run(
            run_benchmark(
                args.url,
                paths=paths,
                total=args.total,
                concurrency=args.concurrency,
                timeout=args.timeout,
            )
        )
        print(render_card(res))


if __name__ == "__main__":
    main()
