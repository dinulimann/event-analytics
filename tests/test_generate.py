import numpy as np
import pytest

import generate


@pytest.fixture(scope="module")
def data():
    return generate.generate(3000, seed=1)


def test_truth_matches_the_rows(data):
    cols, truth = data
    assert truth["events"] == len(cols["ts"]) == sum(truth["by_event"].values())
    assert truth["revenue"] == cols["revenue"].sum()
    assert truth["users"] == len(np.unique(cols["user_id"]))


def test_sessions_are_ordered_funnels(data):
    cols, _ = data
    order = {"page_view": 0, "product_view": 1, "add_to_cart": 2, "checkout_start": 3, "purchase": 4}
    sid, ts, name = cols["session_id"], cols["ts"], cols["event_name"]
    starts = np.r_[True, sid[1:] != sid[:-1]]
    assert (name[starts] == "page_view").all()                 # every session lands on a page
    same = ~starts[1:]
    assert (ts[1:][same] >= ts[:-1][same]).all()               # time moves forward within a session
    rank = np.vectorize(order.get)(name)
    assert (rank[1:][same] >= rank[:-1][same]).all()           # nobody pays before checkout


def test_revenue_only_on_purchases(data):
    cols, _ = data
    buy = cols["event_name"] == "purchase"
    assert (cols["revenue"][buy] > 0).all() and (cols["revenue"][~buy] == 0).all()


def test_incident_version_only_on_mobile_during_the_incident(data):
    cols, _ = data
    bad = cols["app_version"] == "5.2.0"
    assert (cols["device"][bad] == "mobile").all()
    days = ((cols["ts"][bad] - generate.START) // np.timedelta64(1, "D")).astype(int)
    assert days.min() >= 60 and days.max() <= 65   # a session may run past midnight of day 64
