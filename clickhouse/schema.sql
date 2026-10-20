-- Raw events. One row per event, exactly as received (plus server receive time).
-- ReplacingMergeTree: an event sent twice (client retry) has the same event_id and the
-- same sorting key, so ClickHouse collapses the copies when it merges parts.
-- Until then, exact counts use FINAL or uniqExact(event_id).
CREATE TABLE IF NOT EXISTS analytics.events
(
    event_id     UUID DEFAULT generateUUIDv4(),
    event_name   LowCardinality(String),
    user_id      String CODEC(ZSTD(3)),
    session_id   String CODEC(ZSTD(3)),
    ts           DateTime64(3, 'UTC') CODEC(Delta, ZSTD(3)),
    received_at  DateTime64(3, 'UTC') DEFAULT now64(3) CODEC(Delta, ZSTD(3)),
    page         LowCardinality(String),
    channel      LowCardinality(String),
    device       LowCardinality(String),
    country      LowCardinality(String),
    product_id   String DEFAULT '' CODEC(ZSTD(3)),
    revenue      Decimal(12, 2) DEFAULT 0,
    variant      LowCardinality(String) DEFAULT '',
    app_version  LowCardinality(String) DEFAULT ''
)
ENGINE = ReplacingMergeTree(received_at)
PARTITION BY toYYYYMM(ts)
ORDER BY (toDate(ts), event_name, user_id, event_id);

-- Events the collector refused, with the reason. Nothing is silently dropped.
CREATE TABLE IF NOT EXISTS analytics.events_rejected
(
    received_at DateTime64(3, 'UTC') DEFAULT now64(3),
    reason      LowCardinality(String),
    payload     String
)
ENGINE = MergeTree
ORDER BY received_at
TTL toDateTime(received_at) + INTERVAL 30 DAY;

-- Daily unique users per channel and device, kept up to date on every insert.
-- uniqState is a sketch, so duplicates and late events are handled correctly.
CREATE TABLE IF NOT EXISTS analytics.daily_users
(
    day      Date,
    channel  LowCardinality(String),
    device   LowCardinality(String),
    users    AggregateFunction(uniq, String),
    sessions AggregateFunction(uniq, String)
)
ENGINE = AggregatingMergeTree
ORDER BY (day, channel, device);

CREATE MATERIALIZED VIEW IF NOT EXISTS analytics.daily_users_mv TO analytics.daily_users AS
SELECT toDate(ts) AS day, channel, device, uniqState(user_id) AS users, uniqState(session_id) AS sessions
FROM analytics.events
GROUP BY day, channel, device;
