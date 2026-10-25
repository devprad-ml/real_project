''' Closed-loop load test: N concurrent clients, each sending its next request as soon
as the last one returns. httpx + asyncio, no new dependency. See m13.md §4.

    python -m bench.load --key dk_... --path search --levels 1,5,10,20,40,80
    python -m bench.load --key dk_... --path ask    --levels 1,5,10,20,40,80

Per level it prints requests/s (successful only), p50/p95/p99 latency, and the status
codes seen. The knee is where requests/s stops rising while latency keeps climbing.
'''

import argparse
import asyncio
import itertools
import time
from collections import Counter

import httpx

QUESTIONS = [
    "Was claim C-77 denied, and why?",
    "How much was billed on claim C-77?",
    "What evidence did the member submit with the appeal?",
    "Was prior authorization on file for the MRI?",
    "What is the reason code on the denial notice?",
    "When was the Level 1 appeal received?",
]


def pct(sorted_values: list[float], p: float) -> float:
    if not sorted_values:
        return float("nan")
    return sorted_values[min(len(sorted_values) - 1, int(len(sorted_values) * p))]


async def one_level(args, level: int) -> dict:
    latencies: list[float] = []
    statuses: Counter = Counter()
    questions = itertools.cycle(QUESTIONS)
    deadline = time.monotonic() + args.seconds
    headers = {"Authorization": f"Bearer {args.key}"}
    limits = httpx.Limits(max_connections=level + 5, max_keepalive_connections=level + 5)

    async with httpx.AsyncClient(
        base_url=args.url, headers=headers, timeout=args.timeout, limits=limits
    ) as client:

        async def user() -> None:
            while time.monotonic() < deadline:
                question = next(questions)
                started = time.perf_counter()
                try:
                    if args.path == "search":
                        r = await client.get("/search", params={"q": question})
                    else:
                        r = await client.post("/ask", json={"question": question})
                    statuses[r.status_code] += 1
                    if r.is_success:
                        latencies.append(time.perf_counter() - started)
                except httpx.HTTPError as exc:
                    statuses[type(exc).__name__] += 1

        started = time.monotonic()
        await asyncio.gather(*(user() for _ in range(level)))
        elapsed = time.monotonic() - started

    latencies.sort()
    return {
        "level": level,
        "rps": len(latencies) / elapsed,
        "p50": pct(latencies, 0.50),
        "p95": pct(latencies, 0.95),
        "p99": pct(latencies, 0.99),
        "statuses": dict(statuses),
    }


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--url", default="http://localhost:8000")
    ap.add_argument("--key", required=True)
    ap.add_argument("--path", choices=["search", "ask"], required=True)
    ap.add_argument("--levels", default="1,5,10,20,40,80")
    ap.add_argument("--seconds", type=float, default=20)
    ap.add_argument("--timeout", type=float, default=120)
    args = ap.parse_args()

    print(f"{args.path} against {args.url}, {args.seconds:.0f}s per level")
    print(f"{'users':>6} {'req/s':>8} {'p50':>8} {'p95':>8} {'p99':>8}  statuses")
    for level in (int(x) for x in args.levels.split(",")):
        r = await one_level(args, level)
        print(
            f"{r['level']:>6} {r['rps']:>8.1f} {r['p50']:>7.2f}s {r['p95']:>7.2f}s "
            f"{r['p99']:>7.2f}s  {r['statuses']}"
        )


if __name__ == "__main__":
    asyncio.run(main())
