"""Load test of the collector, with the failures real clients produce:
retried batches (duplicates) and malformed events. Then checks ClickHouse:
every valid event stored exactly once, every bad one in events_rejected.

    python scripts/loadtest.py --events 200000 --concurrency 32
"""

from __future__ import annotations

import argparse
import asyncio
import random
import time
import uuid
from datetime import datetime, timedelta, timezone

import clickhouse_connect
import httpx

URL = "http://localhost:8130/v1/events"
NAMES = ["page_view", "product_view", "add_to_cart", "checkout_start"]


def make_event(run: str, i: int, rng: random.Random) -> dict:
    now = datetime.now(timezone.utc)
    return {"event_id": str(uuid.uuid4()), "event_name": rng.choice(NAMES), "user_id": f"{run}-u{i % 5000}",
            "session_id": f"{run}-s{i // 8}", "ts": (now - timedelta(seconds=rng.randint(0, 600))).isoformat(),
            "page": "/", "channel": "loadtest", "device": rng.choice(["mobile", "desktop"]), "country": "ID"}


def make_bad(run: str, i: int, rng: random.Random) -> dict:
    e = make_event(run, i, rng)
    kind = rng.randrange(4)
    if kind == 0:
        e["event_name"] = "clicked_something"
    elif kind == 1:
        e["ts"] = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
    elif kind == 2:
        e["revenue"] = "10.00"                      # revenue on a non-purchase event
    else:
        del e["session_id"]
    return e


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--events", type=int, default=200_000)
    ap.add_argument("--batch", type=int, default=200)
    ap.add_argument("--concurrency", type=int, default=32)
    ap.add_argument("--bad-share", type=float, default=0.02)
    ap.add_argument("--retry-share", type=float, default=0.05)
    ap.add_argument("--keep", action="store_true", help="leave the test events in the tables")
    a = ap.parse_args()
    rng = random.Random(1)
    run = f"lt{int(time.time())}"
    batches, valid, bad_ids = [], 0, set()
    for start in range(0, a.events, a.batch):
        batch = []
        for i in range(start, min(start + a.batch, a.events)):
            if rng.random() < a.bad_share:
                e = make_bad(run, i, rng)
                batch.append(e); bad_ids.add(e["event_id"])
            else:
                batch.append(make_event(run, i, rng)); valid += 1
        batches.append(batch)
    # a client that timed out sends the same batch again: same event_ids
    retries = [b for b in batches if rng.random() < a.retry_share]
    sends = batches + retries
    rng.shuffle(sends)

    queue: asyncio.Queue = asyncio.Queue()
    for b in sends:
        queue.put_nowait(b)
    latencies, accepted, rejected = [], 0, 0

    async def worker(client: httpx.AsyncClient) -> None:
        nonlocal accepted, rejected
        while not queue.empty():
            b = queue.get_nowait()
            t = time.monotonic()
            r = await client.post(URL, json={"events": b})
            latencies.append(time.monotonic() - t)
            r.raise_for_status()
            accepted += r.json()["accepted"]; rejected += r.json()["rejected"]

    t0 = time.monotonic()
    async with httpx.AsyncClient(timeout=60, limits=httpx.Limits(max_connections=a.concurrency)) as client:
        await asyncio.gather(*(worker(client) for _ in range(a.concurrency)))
    took = time.monotonic() - t0
    sent = sum(len(b) for b in sends)
    latencies.sort()
    p = lambda q: 1000 * latencies[int(q * (len(latencies) - 1))]
    print(f"sent {sent:,} events in {len(sends):,} requests ({len(retries)} retried batches) in {took:.1f}s: "
          f"{sent / took:,.0f} events/s; request latency p50 {p(0.5):.0f} ms, p95 {p(0.95):.0f} ms, p99 {p(0.99):.0f} ms")

    ch = clickhouse_connect.get_client(host="localhost", port=8124, username="analytics", password="analytics",
                                       database="analytics")
    raw = ch.command(f"SELECT count() FROM events WHERE startsWith(user_id, '{run}-')")
    unique = ch.command(f"SELECT uniqExact(event_id) FROM events WHERE startsWith(user_id, '{run}-')")
    final = ch.command(f"SELECT count() FROM events FINAL WHERE startsWith(user_id, '{run}-')")
    stored_bad = ch.command(f"SELECT count() FROM events_rejected WHERE position(payload, '{run}-') > 0")
    bad_sent = sum(1 for b in sends for e in b if e["event_id"] in bad_ids)   # incl. retried copies
    print(f"valid events sent: {valid:,} unique; stored rows {raw:,} (before merge), unique ids {unique:,}, "
          f"rows with FINAL {final:,}")
    print(f"invalid events sent: {bad_sent:,} (including copies in retried batches); "
          f"in events_rejected: {stored_bad:,}")
    ok = unique == valid and final == valid and stored_bad == bad_sent
    print("RESULT:", "every valid event stored exactly once, every invalid one rejected" if ok else "MISMATCH")
    if not a.keep:   # keep the analytics data clean: remove what this run wrote
        ch.command(f"DELETE FROM events WHERE channel = 'loadtest'")
        ch.command(f"DELETE FROM daily_users WHERE channel = 'loadtest'")
        ch.command(f"DELETE FROM events_rejected WHERE position(payload, '\"channel\": \"loadtest\"') > 0")
    raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    asyncio.run(main())
