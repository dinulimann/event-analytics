"""Checks the analytics queries against the generator's ground truth (data/truth.json).
Exact numbers, not 'looks plausible'. Exits 1 on any mismatch.

    python scripts/verify.py
"""

import json
import sys
from pathlib import Path

import clickhouse_connect

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from analytics.queries import QUERIES  # noqa: E402

truth = json.loads(Path("data/truth.json").read_text())
ch = clickhouse_connect.get_client(host="localhost", port=8124, username="analytics", password="analytics")
rows = lambda name: ch.query(QUERIES[name]).named_results()
checks = []


def check(label, got, want, tol=0.0):
    ok = abs(got - want) <= tol
    checks.append(ok)
    print(f"{'ok ' if ok else 'BAD'} {label:<52} query {got:>15,.4f}   truth {want:>15,.4f}")


ev = {r["event_name"]: r["events"] for r in rows("events by type")}
for name, n in truth["by_event"].items():
    check(f"events: {name}", ev.get(name, 0), n)
check("events: total", sum(ev.values()), truth["events"])
check("unique users", ch.command("SELECT uniqExact(user_id) FROM analytics.events"), truth["users"])
check("revenue", float(sum(r["total_revenue"] for r in rows("revenue by channel"))), truth["revenue"])
funnel = list(rows("funnel by channel"))
check("funnel: sessions that purchased", sum(r["purchased"] for r in funnel), truth["by_event"]["purchase"])
check("funnel: sessions that started checkout", sum(r["started_checkout"] for r in funnel),
      truth["by_event"]["checkout_start"])
var = {r["variant"]: r["rate"] for r in rows("checkout to purchase by variant")}
for v, rate in truth["purchase_rate_after_checkout"].items():
    check(f"checkout->purchase rate, {v}", var[v], rate, tol=5e-5)
app = {r["app_version"]: r["rate"] for r in rows("mobile payment rate by app version")}
check("mobile checkout->purchase rate, app 5.2.0 (incident)", app["5.2.0"],
      truth["mobile_checkout_to_purchase"]["incident"], tol=5e-5)
print(f"\n{sum(checks)}/{len(checks)} checks match the ground truth")
sys.exit(0 if all(checks) else 1)
