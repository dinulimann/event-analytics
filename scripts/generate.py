"""Generates 90 days of a fictional online shop's events, fully vectorised, and
loads them straight into ClickHouse (a backfill, so the collector's 7-day rule
does not apply). Also writes the true numbers it generated, so every analytics
query can be checked against a known answer.

    python scripts/generate.py               # 2.3M users, about 20M events

Patterns planted on purpose (the dashboard should reveal them):
- returning visits depend on the channel: email users come back most, social users least
- variant "new_checkout" converts checkout -> purchase better (+6 points)
- incident: mobile app 5.2.0, released on day 60, breaks payment for most users
  until 5.2.1 ships on day 65
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import clickhouse_connect
import numpy as np
import pyarrow as pa

START = np.datetime64("2026-07-01T00:00:00", "ms")
DAYS = 90
CHANNELS = np.array(["organic", "paid_search", "social", "email", "referral"])
CH_SHARE = [0.35, 0.25, 0.20, 0.10, 0.10]
CH_RETURN = np.array([0.55, 0.45, 0.30, 0.70, 0.50])     # chance of each further visit
CH_ATC = np.array([0.30, 0.34, 0.22, 0.40, 0.33])        # product view -> add to cart
DEVICES = np.array(["mobile", "desktop", "tablet"])
COUNTRIES = np.array(["ID", "MY", "SG", "PH"])
INCIDENT = (60, 65)                                       # days with app 5.2.0 on mobile


def segment_cumsum(values: np.ndarray, starts: np.ndarray) -> np.ndarray:
    """Cumulative sum that restarts at every index in `starts`."""
    c = np.cumsum(values)
    offset = np.repeat(np.r_[0, c[starts[1:] - 1]], np.diff(np.r_[starts, len(values)]))
    return c - offset


def generate(n_users: int, seed: int, user0: int = 0, sess0: int = 0) -> tuple[dict[str, np.ndarray], dict]:
    """One chunk of independent users; ids start at user0 / sess0."""
    rng = np.random.default_rng(seed)
    # users: acquisition grows over the period, weekends a bit busier
    day_w = np.linspace(1, 1.6, DAYS) * np.where((np.arange(DAYS) + 2) % 7 >= 5, 1.15, 1.0)
    u_day = rng.choice(DAYS, n_users, p=day_w / day_w.sum())
    u_ch = rng.choice(len(CHANNELS), n_users, p=CH_SHARE)
    u_dev = rng.choice(3, n_users, p=[0.62, 0.33, 0.05])
    u_cty = rng.choice(4, n_users, p=[0.70, 0.12, 0.08, 0.10])
    u_var = rng.integers(0, 2, n_users)                    # 1 = new_checkout

    # sessions: first on the acquisition day, then geometric number of returns
    n_sess = 1 + rng.geometric(1 - CH_RETURN[u_ch]) - 1
    s_user = np.repeat(np.arange(n_users), n_sess)
    s_first = np.r_[0, np.cumsum(n_sess)[:-1]]
    gaps = rng.exponential(6.0, len(s_user))
    gaps[s_first] = 0
    s_dayf = u_day[s_user] + segment_cumsum(gaps, s_first)
    keep = s_dayf < DAYS
    s_user, s_dayf = s_user[keep], s_dayf[keep]
    s_day = s_dayf.astype(int)
    n_s = len(s_user)
    # local evening peak (UTC+7): hours drawn around 20:00 local = 13:00 UTC
    s_start = START + (s_day * 86_400_000 + (rng.normal(13, 4, n_s) % 24) * 3_600_000).astype("timedelta64[ms]")

    ch, dev = u_ch[s_user], u_dev[s_user]
    mobile = dev == 0
    app_bad = mobile & (s_day >= INCIDENT[0]) & (s_day < INCIDENT[1])
    app_ver = np.where(mobile, np.where(s_day < INCIDENT[0], "5.1.4", np.where(s_day < INCIDENT[1], "5.2.0", "5.2.1")), "")

    extra_pv = rng.poisson(1.2, n_s)
    browses = rng.random(n_s) < 0.75
    n_prod = np.where(browses, 1 + rng.poisson(1.8, n_s), 0)
    atc = browses & (rng.random(n_s) < CH_ATC[ch] * np.where(mobile, 0.9, 1.0))
    checkout = atc & (rng.random(n_s) < 0.55)
    p_buy = 0.62 + 0.06 * u_var[s_user] - 0.05 * mobile
    p_buy = np.where(app_bad, 0.15, p_buy)
    buy = checkout & (rng.random(n_s) < p_buy)

    n_ev = 1 + extra_pv + n_prod + atc + checkout + buy
    e_sess = np.repeat(np.arange(n_s), n_ev)
    first = np.r_[0, np.cumsum(n_ev)[:-1]]
    pos = np.arange(len(e_sess)) - np.repeat(first, n_ev)
    pv_end = 1 + extra_pv[e_sess]
    prod_end = pv_end + n_prod[e_sess]
    k = pos - prod_end                                     # 0, 1, 2 after the product views
    name = np.select(
        [pos < pv_end, pos < prod_end, (k == 0) & atc[e_sess], (k == 1) & checkout[e_sess]],
        ["page_view", "product_view", "add_to_cart", "checkout_start"], "purchase")
    step = rng.exponential(35_000, len(e_sess))
    step[first] = 0
    ts = s_start[e_sess] + segment_cumsum(step, first).astype("timedelta64[ms]")

    products = 1 + (rng.zipf(1.3, len(e_sess)) - 1) % 2000
    session_product = products[first][e_sess]             # the item added to cart / bought
    is_prod = name == "product_view"
    pid = np.where(is_prod, products, np.where(np.isin(name, ["add_to_cart", "checkout_start", "purchase"]),
                                               session_product, 0))
    page = np.select([name == "page_view", is_prod, name == "add_to_cart", name == "checkout_start"],
                     [np.where(pos == 0, "/", "/category"), "/product", "/cart", "/checkout"], "/thank-you")
    rev = np.where(name == "purchase", np.round(rng.lognormal(12.3, 0.7, len(e_sess)), -3), 0)

    u = s_user[e_sess]
    cols = {
        "event_name": name, "user_id": np.char.add("u", (u + user0).astype(str)),
        "session_id": np.char.add("s", (e_sess + sess0).astype(str)), "ts": ts, "page": page,
        "channel": CHANNELS[u_ch[u]], "device": DEVICES[u_dev[u]], "country": COUNTRIES[u_cty[u]],
        "product_id": np.where(pid > 0, np.char.add("p", pid.astype(str)), ""),
        "revenue": rev, "variant": np.where(u_var[u] == 1, "new_checkout", "control"),
        "app_version": app_ver[e_sess],
    }
    var = u_var[s_user]
    truth = {   # raw counts, so chunks can be added up
        "events": int(len(e_sess)), "users": int(len(np.unique(s_user))), "sessions": int(n_s),
        "by_event": {k: int(v) for k, v in zip(*np.unique(name, return_counts=True))},
        "revenue": float(rev.sum()),
        "checkout_control": int((checkout & (var == 0)).sum()), "buy_control": int((buy & (var == 0)).sum()),
        "checkout_new": int((checkout & (var == 1)).sum()), "buy_new": int((buy & (var == 1)).sum()),
        "checkout_incident": int((checkout & app_bad).sum()), "buy_incident": int((buy & app_bad).sum()),
        "checkout_mobile_ok": int((checkout & mobile & ~app_bad).sum()),
        "buy_mobile_ok": int((buy & mobile & ~app_bad).sum()),
    }
    return cols, truth


def load(cols: dict[str, np.ndarray], chunk: int = 2_000_000) -> None:
    client = clickhouse_connect.get_client(host="localhost", port=8124, username="analytics",
                                           password="analytics", database="analytics")
    n = len(cols["ts"])
    for i in range(0, n, chunk):
        table = pa.table({k: pa.array(v[i:i + chunk]) if k != "ts" else
                          pa.array(v[i:i + chunk], type=pa.timestamp("ms", tz="UTC")) for k, v in cols.items()})
        client.insert_arrow("events", table)


def add(total: dict, part: dict) -> dict:
    for k, v in part.items():
        total[k] = add(total.get(k, {}), v) if isinstance(v, dict) else total.get(k, 0) + v
    return total


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--users", type=int, default=2_300_000)
    ap.add_argument("--chunk-users", type=int, default=250_000)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--no-load", action="store_true")
    a = ap.parse_args()
    t, truth, user0, sess0 = time.monotonic(), {}, 0, 0
    for i, start in enumerate(range(0, a.users, a.chunk_users)):
        n = min(a.chunk_users, a.users - start)
        cols, part = generate(n, a.seed + i, user0, sess0)
        user0, sess0 = user0 + n, sess0 + part["sessions"]
        add(truth, part)
        if not a.no_load:
            load(cols)
        print(f"chunk {i + 1}: {truth['events']:,} events so far ({time.monotonic() - t:.0f}s)", flush=True)
    truth["purchase_rate_after_checkout"] = {"control": truth["buy_control"] / truth["checkout_control"],
                                             "new_checkout": truth["buy_new"] / truth["checkout_new"]}
    truth["mobile_checkout_to_purchase"] = {
        "incident": truth["buy_incident"] / truth["checkout_incident"],
        "otherwise": truth["buy_mobile_ok"] / truth["checkout_mobile_ok"]}
    Path("data").mkdir(exist_ok=True)
    Path("data/truth.json").write_text(json.dumps(truth, indent=2))
    print(f"done: {truth['events']:,} events, {truth['sessions']:,} sessions, {truth['users']:,} users "
          f"in {time.monotonic() - t:.0f}s")


if __name__ == "__main__":
    main()
