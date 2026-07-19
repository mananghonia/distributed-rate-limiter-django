#!/usr/bin/env python
"""
Phase 6 -- concurrent load test.

Fires many requests in parallel at the gateway and proves two things an
interviewer will ask about:

  1. Accuracy under contention -- the number of ALLOWED requests equals the
     configured limit (not more), even though requests race each other and,
     in the docker-compose cluster, land on different instances. This is the
     payoff of the atomic Lua scripts: no read-then-write overcount.
  2. Overhead -- the added latency of the gateway hop is small.

Usage:
  # Point at a single dev server or the load-balanced cluster (:8080).
  python loadtest/loadtest.py --url http://127.0.0.1:8000/ping \\
      --requests 300 --concurrency 50 --api-key free-key-123

The free tier's limit is 100/min, so ~100 of 300 should be allowed and ~200
rejected with 429 -- run it and watch the numbers line up.
"""
import argparse
import statistics
import time
from concurrent.futures import ThreadPoolExecutor

import requests


def fire(url: str, api_key: str | None) -> tuple[int, float]:
    headers = {"X-API-Key": api_key} if api_key else {}
    start = time.perf_counter()
    resp = requests.get(url, headers=headers, timeout=10)
    elapsed_ms = (time.perf_counter() - start) * 1000
    return resp.status_code, elapsed_ms


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8000/ping")
    parser.add_argument("--requests", type=int, default=300)
    parser.add_argument("--concurrency", type=int, default=50)
    parser.add_argument("--api-key", default=None)
    args = parser.parse_args()

    print(f"Firing {args.requests} requests at {args.url} "
          f"(concurrency={args.concurrency}, api_key={args.api_key})\n")

    results: list[tuple[int, float]] = []
    wall_start = time.perf_counter()
    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futures = [pool.submit(fire, args.url, args.api_key) for _ in range(args.requests)]
        for f in futures:
            results.append(f.result())
    wall = time.perf_counter() - wall_start

    allowed = sum(1 for code, _ in results if code == 200)
    rejected = sum(1 for code, _ in results if code == 429)
    other = len(results) - allowed - rejected
    latencies = sorted(ms for _, ms in results)

    def pct(p):
        return latencies[min(len(latencies) - 1, int(len(latencies) * p))]

    print(f"  total:     {len(results)}")
    print(f"  allowed:   {allowed}   <- should equal the configured limit")
    print(f"  rejected:  {rejected}  (429 Too Many Requests)")
    print(f"  other:     {other}")
    print(f"  wall time: {wall:.2f}s   throughput: {len(results)/wall:,.0f} req/s")
    print(f"  latency ms: p50={pct(0.50):.1f}  p95={pct(0.95):.1f}  "
          f"p99={pct(0.99):.1f}  max={latencies[-1]:.1f}  "
          f"mean={statistics.mean(latencies):.1f}")


if __name__ == "__main__":
    main()
