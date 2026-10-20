import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

import collector

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)


def ev(**over):
    e = {"event_id": str(uuid.uuid4()), "event_name": "page_view", "user_id": "u1", "session_id": "s1",
         "ts": (NOW - timedelta(minutes=1)).isoformat(), "page": "/", "device": "mobile", "country": "ID"}
    e.update(over)
    return {k: v for k, v in e.items() if v is not None}


def test_good_event_passes():
    good, bad = collector.validate([ev(), ev(event_name="purchase", revenue="125000.00")], NOW)
    assert len(good) == 2 and bad == []
    assert good[0].ts.tzinfo == timezone.utc


@pytest.mark.parametrize("over, reason", [
    ({"event_name": "clicked"}, "event_name"),
    ({"event_id": "not-a-uuid"}, "event_id"),
    ({"session_id": None}, "session_id"),
    ({"device": "fridge"}, "device"),
    ({"ts": "2026-10-01T11:59:00"}, "timezone"),
    ({"ts": (NOW + timedelta(hours=1)).isoformat()}, "future"),
    ({"ts": (NOW - timedelta(days=8)).isoformat()}, "older than 7 days"),
    ({"revenue": "10.00"}, "revenue is only allowed"),
    ({"event_name": "purchase"}, "purchase without revenue"),
    ({"event_name": "purchase", "revenue": "-5"}, "revenue"),
])
def test_bad_events_are_rejected_with_a_reason(over, reason):
    good, bad = collector.validate([ev(), ev(**over)], NOW)
    assert len(good) == 1 and len(bad) == 1
    assert reason in bad[0][0]


def test_one_bad_event_does_not_sink_the_batch():
    good, bad = collector.validate([ev(), ev(event_name="nope"), ev()], NOW)
    assert (len(good), len(bad)) == (2, 1)


def test_oversized_batch_is_refused_whole():
    r = TestClient(collector.app).post("/v1/events", json={"events": [ev()] * 501})
    assert r.status_code == 422


def test_storage_down_means_503_not_success(monkeypatch):
    class Broken:
        def insert(self, *a, **k):
            raise ConnectionError("clickhouse is down")
    monkeypatch.setattr(collector, "client", lambda: Broken())
    e = ev(ts=datetime.now(timezone.utc).isoformat())
    r = TestClient(collector.app).post("/v1/events", json={"events": [e]})
    assert r.status_code == 503
