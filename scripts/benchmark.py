"""Times every analytics query on the full dataset (median of 5 runs, after a warm-up)
and reads how much data ClickHouse scanned for it.

    python scripts/benchmark.py > docs/benchmark.md
"""

import statistics
import sys
import time
from pathlib import Path

import clickhouse_connect

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from analytics.queries import QUERIES  # noqa: E402

ch = clickhouse_connect.get_client(host="localhost", port=8124, username="analytics", password="analytics")
n = ch.command("SELECT count() FROM analytics.events")
size = ch.command("SELECT formatReadableSize(sum(data_compressed_bytes)) FROM system.parts "
                  "WHERE database = 'analytics' AND table = 'events' AND active")
print(f"{n:,} events, {size} on disk. Median of 5 runs after one warm-up.\n")
print("| Query | Median | Rows read | Data read |\n|---|---:|---:|---:|")
for name, sql in QUERIES.items():
    ch.query(sql)
    times = []
    for _ in range(5):
        t = time.monotonic()
        r = ch.query(sql)
        times.append(time.monotonic() - t)
    s = r.summary
    print(f"| {name} | {1000 * statistics.median(times):,.0f} ms | {int(s.get('read_rows', 0)):,} "
          f"| {int(s.get('read_bytes', 0)) / 2**20:,.0f} MB |")
