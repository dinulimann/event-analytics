"""Event collector.  POST /v1/events  {"events": [...]}  (up to 500 per request)

Each event is validated on its own: good events are stored, bad ones go to
events_rejected with a reason, and the response says how many of each. The
response is sent only after ClickHouse has written the batch
(async_insert + wait_for_async_insert), so a 202 means the events are stored.
Clients retry on errors with the same event_id; duplicates collapse in storage.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Literal

import clickhouse_connect
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

EVENTS = Literal["page_view", "product_view", "add_to_cart", "checkout_start", "purchase", "signup", "search"]
MAX_BATCH = 500
MAX_AGE = timedelta(days=7)        # older events are refused: late data must be backfilled on purpose
MAX_SKEW = timedelta(minutes=5)    # clocks ahead by more than this are refused
COLUMNS = ["event_id", "event_name", "user_id", "session_id", "ts", "page", "channel", "device", "country",
           "product_id", "revenue", "variant", "app_version"]


class Event(BaseModel):
    event_id: uuid.UUID
    event_name: EVENTS
    user_id: str = Field(min_length=1, max_length=64)
    session_id: str = Field(min_length=1, max_length=64)
    ts: datetime
    page: str = Field(max_length=200)
    channel: str = Field(default="direct", max_length=32)
    device: Literal["desktop", "mobile", "tablet"]
    country: str = Field(min_length=2, max_length=2)
    product_id: str = Field(default="", max_length=64)
    revenue: Decimal = Field(default=Decimal(0), ge=0, max_digits=12, decimal_places=2)
    variant: str = Field(default="", max_length=32)
    app_version: str = Field(default="", max_length=32)

    @field_validator("ts")
    @classmethod
    def utc(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("timestamp needs a timezone")
        return v.astimezone(timezone.utc)

    @model_validator(mode="after")
    def revenue_only_on_purchase(self):
        if self.revenue and self.event_name != "purchase":
            raise ValueError("revenue is only allowed on purchase events")
        if self.event_name == "purchase" and not self.revenue:
            raise ValueError("purchase without revenue")
        return self


class Batch(BaseModel):
    events: list[dict] = Field(max_length=MAX_BATCH)


def check_time(e: Event, now: datetime) -> str | None:
    if e.ts > now + MAX_SKEW:
        return "timestamp in the future"
    if e.ts < now - MAX_AGE:
        return "timestamp older than 7 days"
    return None


def validate(raw: list[dict], now: datetime) -> tuple[list[Event], list[tuple[str, str]]]:
    good, bad = [], []
    for item in raw:
        try:
            e = Event.model_validate(item)
        except ValidationError as err:
            first = err.errors()[0]
            bad.append((f"{'.'.join(map(str, first['loc'])) or 'event'}: {first['msg']}"[:120],
                        json.dumps(item, default=str)[:2000]))
            continue
        reason = check_time(e, now)
        if reason:
            bad.append((reason, json.dumps(item, default=str)[:2000]))
        else:
            good.append(e)
    return good, bad


app = FastAPI(title="event collector")
_client = None


def client():
    global _client
    if _client is None:
        _client = clickhouse_connect.get_client(
            host=os.getenv("CLICKHOUSE_HOST", "localhost"), port=int(os.getenv("CLICKHOUSE_PORT", "8124")),
            username="analytics", password="analytics", database="analytics",
            autogenerate_session_id=False,   # shared across worker threads
            settings={"async_insert": 1, "wait_for_async_insert": 1})
    return _client


@app.get("/health")
def health():
    client().command("SELECT 1")
    return {"ok": True}


@app.post("/v1/events", status_code=202)
def collect(batch: Batch):
    good, bad = validate(batch.events, datetime.now(timezone.utc))
    try:
        if good:
            client().insert("events", [[getattr(e, c) for c in COLUMNS] for e in good], column_names=COLUMNS)
        if bad:
            client().insert("events_rejected", [list(b) for b in bad], column_names=["reason", "payload"])
    except Exception as err:   # storage down: tell the client to retry, never pretend success
        raise HTTPException(503, f"storage unavailable: {type(err).__name__}") from err
    return {"accepted": len(good), "rejected": len(bad)}
