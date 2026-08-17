-- Migration 091: generalized prediction labeling data plane.
-- Domain: non-financial ML telemetry; all definitions and result versions are immutable.
-- Invariant: labeling failures never gate billing, settlement, usage, or scheduled runs.

CREATE TABLE IF NOT EXISTS labeling_definitions (
    definition_key TEXT NOT NULL,
    definition_version INTEGER NOT NULL CHECK (definition_version > 0),
    prediction_schema JSONB NOT NULL DEFAULT '{}'::jsonb,
    target_schema JSONB NOT NULL DEFAULT '{}'::jsonb,
    outcome_schema JSONB NOT NULL DEFAULT '{}'::jsonb,
    parser_key TEXT NOT NULL,
    parser_version TEXT NOT NULL DEFAULT '1',
    provider_key TEXT NOT NULL,
    provider_version TEXT NOT NULL DEFAULT '1',
    scorer_key TEXT NOT NULL,
    scorer_version TEXT NOT NULL DEFAULT '1',
    config JSONB NOT NULL DEFAULT '{}'::jsonb,
    active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (definition_key, definition_version),
    CONSTRAINT labeling_definitions_key_chk
        CHECK (definition_key ~ '^[a-z0-9][a-z0-9_.-]{0,127}$'),
    CONSTRAINT labeling_definitions_parser_chk
        CHECK (parser_key ~ '^[a-z0-9][a-z0-9_.-]{0,127}$'),
    CONSTRAINT labeling_definitions_provider_chk
        CHECK (provider_key ~ '^[a-z0-9][a-z0-9_.-]{0,127}$'),
    CONSTRAINT labeling_definitions_scorer_chk
        CHECK (scorer_key ~ '^[a-z0-9][a-z0-9_.-]{0,127}$')
);

CREATE TABLE IF NOT EXISTS labeling_bindings (
    id TEXT PRIMARY KEY,
    org_id TEXT NOT NULL,
    source_kind TEXT NOT NULL,
    source_id TEXT NOT NULL,
    definition_key TEXT NOT NULL,
    definition_version INTEGER NOT NULL,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (org_id, source_kind, source_id),
    FOREIGN KEY (definition_key, definition_version)
        REFERENCES labeling_definitions (definition_key, definition_version),
    CONSTRAINT labeling_bindings_source_kind_chk
        CHECK (source_kind IN ('scheduled_run', 'external')),
    CONSTRAINT labeling_bindings_source_id_chk CHECK (length(source_id) BETWEEN 1 AND 256)
);

CREATE INDEX IF NOT EXISTS idx_labeling_bindings_org_enabled
    ON labeling_bindings (org_id, enabled);

CREATE TABLE IF NOT EXISTS labeling_predictions (
    id TEXT PRIMARY KEY,
    org_id TEXT NOT NULL,
    source_kind TEXT NOT NULL,
    source_id TEXT NOT NULL,
    run_id TEXT NOT NULL DEFAULT '',
    schedule_id TEXT NOT NULL DEFAULT '',
    binding_id TEXT,
    definition_key TEXT NOT NULL,
    definition_version INTEGER NOT NULL,
    predictions JSONB NOT NULL,
    payload_sha256 TEXT NOT NULL,
    prediction_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    status TEXT NOT NULL DEFAULT 'accepted',
    parse_error TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (org_id, source_kind, source_id, definition_key, definition_version),
    UNIQUE (id, org_id),
    FOREIGN KEY (definition_key, definition_version)
        REFERENCES labeling_definitions (definition_key, definition_version),
    FOREIGN KEY (binding_id)
        REFERENCES labeling_bindings (id) ON DELETE SET NULL,
    CONSTRAINT labeling_predictions_source_kind_chk
        CHECK (source_kind IN ('scheduled_run', 'external')),
    CONSTRAINT labeling_predictions_status_chk
        CHECK (status IN ('accepted', 'invalid')),
    CONSTRAINT labeling_predictions_hash_chk
        CHECK (payload_sha256 ~ '^[0-9a-f]{64}$'),
    CONSTRAINT labeling_predictions_run_id_chk
        CHECK (source_kind <> 'scheduled_run' OR (run_id <> '' AND schedule_id <> ''))
);

CREATE INDEX IF NOT EXISTS idx_labeling_predictions_org_created
    ON labeling_predictions (org_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_labeling_predictions_definition_created
    ON labeling_predictions (definition_key, definition_version, created_at DESC);

CREATE TABLE IF NOT EXISTS labeling_targets (
    id TEXT PRIMARY KEY,
    prediction_id TEXT NOT NULL,
    org_id TEXT NOT NULL,
    item_key TEXT NOT NULL,
    item_payload JSONB NOT NULL,
    window_start TIMESTAMPTZ NOT NULL,
    window_end TIMESTAMPTZ NOT NULL,
    due_at TIMESTAMPTZ NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    lease_token TEXT,
    lease_expires_at TIMESTAMPTZ,
    last_error TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (prediction_id, item_key),
    UNIQUE (id, org_id),
    FOREIGN KEY (prediction_id, org_id)
        REFERENCES labeling_predictions (id, org_id) ON DELETE CASCADE,
    CONSTRAINT labeling_targets_item_key_chk CHECK (length(item_key) BETWEEN 1 AND 256),
    CONSTRAINT labeling_targets_window_chk CHECK (window_start < window_end),
    CONSTRAINT labeling_targets_status_chk
        CHECK (status IN ('pending', 'leased', 'scored', 'unavailable', 'invalid')),
    CONSTRAINT labeling_targets_lease_chk
        CHECK (
            (status = 'leased' AND lease_token IS NOT NULL AND lease_expires_at IS NOT NULL)
            OR (status <> 'leased' AND lease_token IS NULL AND lease_expires_at IS NULL)
        )
);

CREATE INDEX IF NOT EXISTS idx_labeling_targets_due
    ON labeling_targets (status, due_at, id);

CREATE INDEX IF NOT EXISTS idx_labeling_targets_org_due
    ON labeling_targets (org_id, status, due_at, id);

CREATE TABLE IF NOT EXISTS labeling_observations (
    id TEXT PRIMARY KEY,
    scope_key TEXT NOT NULL DEFAULT 'public',
    provider_key TEXT NOT NULL,
    provider_version TEXT NOT NULL,
    request_sha256 TEXT NOT NULL,
    as_of TIMESTAMPTZ NOT NULL,
    payload JSONB,
    status TEXT NOT NULL,
    error TEXT NOT NULL DEFAULT '',
    fetched_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (scope_key, provider_key, provider_version, request_sha256),
    CONSTRAINT labeling_observations_scope_chk CHECK (length(scope_key) BETWEEN 1 AND 256),
    CONSTRAINT labeling_observations_status_chk
        CHECK (status IN ('ready', 'unavailable', 'invalid')),
    CONSTRAINT labeling_observations_hash_chk
        CHECK (request_sha256 ~ '^[0-9a-f]{64}$')
);

CREATE INDEX IF NOT EXISTS idx_labeling_observations_as_of
    ON labeling_observations (provider_key, as_of DESC);

CREATE TABLE IF NOT EXISTS labeling_results (
    id TEXT PRIMARY KEY,
    target_id TEXT NOT NULL,
    scorer_key TEXT NOT NULL,
    scorer_version TEXT NOT NULL,
    observation_id TEXT,
    actual JSONB,
    label TEXT NOT NULL,
    score NUMERIC,
    status TEXT NOT NULL,
    source TEXT NOT NULL,
    rationale TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    FOREIGN KEY (target_id) REFERENCES labeling_targets (id) ON DELETE CASCADE,
    FOREIGN KEY (observation_id) REFERENCES labeling_observations (id) ON DELETE RESTRICT,
    CONSTRAINT labeling_results_status_chk
        CHECK (status IN ('correct', 'incorrect', 'neutral', 'inconclusive', 'unavailable', 'invalid')),
    CONSTRAINT labeling_results_source_chk
        CHECK (source IN ('automatic', 'external', 'manual')),
    CONSTRAINT labeling_results_score_chk
        CHECK (
            (status IN ('correct', 'incorrect', 'neutral') AND score IS NOT NULL)
            OR (status IN ('inconclusive', 'unavailable', 'invalid') AND score IS NULL)
        )
);

CREATE INDEX IF NOT EXISTS idx_labeling_results_target_created
    ON labeling_results (target_id, created_at DESC, id DESC);

CREATE INDEX IF NOT EXISTS idx_labeling_results_org_created
    ON labeling_results (created_at DESC, id DESC);

INSERT INTO tool_pricing_overrides (tool_name, cost_usdc, description)
VALUES ('record_predictions', 0, 'In-process structured prediction capture; zero marginal cost')
ON CONFLICT (tool_name) DO UPDATE
SET cost_usdc = EXCLUDED.cost_usdc,
    description = EXCLUDED.description,
    updated_at = NOW();

INSERT INTO labeling_definitions
    (definition_key, definition_version, prediction_schema, parser_key, parser_version,
     provider_key, provider_version, scorer_key, scorer_version, config)
VALUES
    (
        'entry_timing',
        1,
        '{"type":"object","required":["task_class"],"properties":{"task_class":{"const":"entry_timing"}}}'::jsonb,
        'entry_timing', '1', 'token_price', '1', 'entry_return', '1',
        '{"horizon_seconds":1209600}'::jsonb
    ),
    (
        'eth_primitive_fees',
        1,
        '{"type":"object","required":["task_class"],"properties":{"task_class":{"const":"eth_primitive_fees"}}}'::jsonb,
        'eth_protocols', '1', 'protocol_fees', '1', 'fee_direction', '1',
        '{"horizon_seconds":604800}'::jsonb
    ),
    (
        'stablecoin_yield_compare',
        1,
        '{"type":"object","required":["task_class"],"properties":{"task_class":{"const":"stablecoin_yield_compare"}}}'::jsonb,
        'stablecoin_root', '1', 'stablecoin_market', '1', 'stablecoin_spread', '1',
        '{"horizon_seconds":604800}'::jsonb
    )
ON CONFLICT (definition_key, definition_version) DO NOTHING;
