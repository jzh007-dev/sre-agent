-- Log storage schema for sre-agent mock environment.
--
-- Design decisions (all interview-relevant):
--
-- 1. LowCardinality(String) for repeated values (service, level, endpoint):
--    ClickHouse dictionary-encodes these — 10x storage saving vs plain String,
--    plus faster GROUP BY / WHERE on these columns.
--
-- 2. PARTITION BY toDate(ts): daily partitions.
--    Query "last 1h logs" only touches today's partition, not the whole table.
--    TTL can drop old partitions atomically in one file delete.
--
-- 3. ORDER BY (service, level, ts): defines the primary key and sort order
--    on disk. Queries filtering by service + level are near-instant because
--    of the sparse primary index. `ts` at the end enables range scans.
--
-- 4. TTL 7 days: auto-delete stale partitions. POC scale — bump for prod.
--
-- 5. `extra String`: JSON string of unrecognized fields. Keeps schema
--    stable while allowing services to add ad-hoc fields.
--    In real prod this would be a Map(String, String) or a Nested type.
--
-- 6. Nullable on optional fields: allows /metrics-scrape access logs
--    (which have method/endpoint/status) to coexist with business event
--    logs (which may not).

CREATE DATABASE IF NOT EXISTS sre;

CREATE TABLE IF NOT EXISTS sre.logs
(
    ts             DateTime64(3, 'UTC'),
    service        LowCardinality(String),
    version        LowCardinality(String),
    level          LowCardinality(String),
    correlation_id String,
    msg            String,

    -- Access-log fields (nullable so business logs without these still fit)
    endpoint       LowCardinality(Nullable(String)),
    method         LowCardinality(Nullable(String)),
    status         Nullable(UInt16),
    duration_ms    Nullable(Float32),

    -- Anything the service passes via `extra=` that we don't have a column for
    extra          String DEFAULT '',

    -- Vector fills these from the docker source, useful for debugging pipeline
    container_id   LowCardinality(String) DEFAULT '',
    _ingested_at   DateTime64(3, 'UTC') DEFAULT now64(3)
)
ENGINE = MergeTree
PARTITION BY toDate(ts)
ORDER BY (service, level, ts)
TTL toDateTime(ts) + INTERVAL 7 DAY
SETTINGS index_granularity = 8192;
