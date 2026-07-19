#!/usr/bin/env python
"""
The distributed proof.

Launches N *separate* gateway processes (real OS processes, each with its own
memory) all pointed at ONE shared Redis, then fires concurrent traffic spread
across every instance -- exactly what a load balancer does. Because the counting
state lives in Redis and is mutated by atomic Lua, the limit holds GLOBALLY:
the total allowed equals the configured limit even though no single instance saw
all the traffic.

Contrast with the in-memory baseline: run the same load with
RATELIMIT_ALGORITHM=memory and each process enforces its own limit, so the
global allowed count balloons to (instances x limit). That difference is the
whole point of the project.

Prereqs: a real Redis reachable at REDIS_URL (the in-process fakeredis fallback
is single-process and cannot demonstrate this). Example:

  REDIS_URL=redis://127.0.0.1:6399/0 python loadtest/distributed_demo.py \\
      --instances 3 --requests 300 --concurrency 60 --api-key free-key-123
"""
import argparse
import os
import subprocess
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

BASE_DIR = Path(__file__).resolve().parent.parent


def wait_healthy(port: int, timeout: float = 40.0) -> bool:
    """Block until an instance reports Redis is up (or give up)."""
    deadline = time.time() + timeout
    url = f"http://127.0.0.1:{port}/healthz"
    while time.time() < deadline:
        try:
            r = requests.get(url, timeout=1)
            if r.status_code == 200 and r.json().get("redis") == "up":
                return True
        except requests.RequestException:
            pass
        time.sleep(0.5)
    return False


def start_instances(ports, redis_url, algorithm):
    procs = []
    for i, port in enumerate(ports):
        env = os.environ.copy()
        env["RATELIMIT_USE_FAKEREDIS"] = "0"   # force the real shared Redis
        env["REDIS_URL"] = redis_url
        env["RATELIMIT_ALGORITHM"] = algorithm
        env["INSTANCE_ID"] = f"gw-{i+1}"
        env["DJANGO_SETTINGS_MODULE"] = "gateway.settings"
        # Each instance proxies allowed requests to its OWN bundled demo
        # upstream, so an allowed request returns 200 (not a 502 to a
        # non-existent shared backend). In production this points at your API.
        env["UPSTREAM_BASE_URL"] = f"http://127.0.0.1:{port}/demo-upstream"
        proc = subprocess.Popen(
            [sys.executable, "manage.py", "runserver", f"127.0.0.1:{port}", "--noreload"],
            cwd=str(BASE_DIR),
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        procs.append((f"gw-{i+1}", port, proc))
    return procs


def fire(port, api_key):
    headers = {"X-API-Key": api_key} if api_key else {}
    r = requests.get(f"http://127.0.0.1:{port}/ping", headers=headers, timeout=10)
    return r.status_code, r.headers.get("X-Gateway-Instance", "?")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--instances", type=int, default=3)
    parser.add_argument("--base-port", type=int, default=8001)
    parser.add_argument("--requests", type=int, default=300)
    parser.add_argument("--concurrency", type=int, default=60)
    parser.add_argument("--api-key", default="free-key-123")  # free tier = 100/min
    parser.add_argument("--algorithm", default="fixed_window")
    parser.add_argument(
        "--redis-url",
        default=os.environ.get("REDIS_URL", "redis://127.0.0.1:6399/0"),
    )
    args = parser.parse_args()

    ports = [args.base_port + i for i in range(args.instances)]
    print(f"Starting {args.instances} gateway instances on ports {ports}")
    print(f"All sharing Redis at {args.redis_url} (algorithm={args.algorithm})\n")

    procs = start_instances(ports, args.redis_url, args.algorithm)
    try:
        for name, port, _ in procs:
            ok = wait_healthy(port)
            print(f"  {name} on :{port} -> {'READY (redis up)' if ok else 'FAILED to become healthy'}")
            if not ok:
                print("Aborting: an instance never connected to Redis.")
                return 1

        print(f"\nFiring {args.requests} requests, round-robin across all instances "
              f"(concurrency={args.concurrency})...\n")

        def task(n):
            return fire(ports[n % len(ports)], args.api_key)

        by_instance = Counter()
        allowed_by_instance = Counter()
        codes = Counter()
        with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
            for code, instance in pool.map(task, range(args.requests)):
                codes[code] += 1
                by_instance[instance] += 1
                if code == 200:
                    allowed_by_instance[instance] += 1

        allowed = codes[200]
        rejected = codes[429]
        print("Per-instance traffic (proves load really spread across processes):")
        for name, port, _ in procs:
            print(f"  {name}: handled {by_instance[name]:>4}  |  allowed {allowed_by_instance[name]:>3}")
        print()
        print(f"  GLOBAL allowed:  {allowed}   <- should equal the tier limit, NOT limit x {args.instances}")
        print(f"  GLOBAL rejected: {rejected}  (429)")
        print()
        if allowed == 100:
            print("PROOF: global limit held at 100 across all instances. "
                  "Distributed rate limiting works.")
        else:
            print(f"NOTE: allowed={allowed}. Expected 100 for the free tier in a fresh window "
                  "(re-run against a flushed Redis / new minute if this drifted).")
    finally:
        print("\nShutting down instances...")
        for _, _, proc in procs:
            proc.terminate()
        for _, _, proc in procs:
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
