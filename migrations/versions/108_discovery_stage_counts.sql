-- Migration 108: aggregate hourly discovery-stage counters for funnel intelligence
-- Domain: telemetry / funnel analytics
-- Invariant: aggregate counts only — no per-request rows, no IP addresses, no user agents.
-- Unauthenticated discovery surfaces increment in-process counters flushed here as
-- bounded (surface, hour) upserts, so public endpoints cannot drive row-amplification.

CREATE TABLE IF NOT EXISTS discovery_stage_counts (
    surface      TEXT        NOT NULL,
    bucket_hour  TIMESTAMPTZ NOT NULL,
    count        BIGINT      NOT NULL CHECK (count >= 0),
    PRIMARY KEY (surface, bucket_hour)
);

CREATE INDEX IF NOT EXISTS idx_discovery_stage_counts_hour
    ON discovery_stage_counts (bucket_hour);

COMMENT ON TABLE discovery_stage_counts IS
    'Aggregate hourly hit counts per discovery surface. No PII: no IPs, no user agents, no per-request rows.';