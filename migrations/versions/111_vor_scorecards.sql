-- Migration 111: pre-registered public scorecard tasks.
-- Domain: labeling / verified-outcome record
-- Invariants:
--   * Definition content is immutable once inserted; only `active` may change, so a
--     pre-registered task cannot be edited after predictions are committed against it.
--   * Public scorecards are derived on read from committed, anchored, automatic results;
--     no aggregate state is stored.
-- Additive only.

CREATE OR REPLACE FUNCTION vor_guard_definition() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'labeling definition %@% is append-only', OLD.definition_key, OLD.definition_version
            USING ERRCODE = 'check_violation';
    END IF;
    IF (NEW.definition_key, NEW.definition_version, NEW.prediction_schema, NEW.target_schema,
        NEW.outcome_schema, NEW.parser_key, NEW.parser_version, NEW.provider_key, NEW.provider_version,
        NEW.scorer_key, NEW.scorer_version, NEW.config, NEW.created_at)
       IS DISTINCT FROM
       (OLD.definition_key, OLD.definition_version, OLD.prediction_schema, OLD.target_schema,
        OLD.outcome_schema, OLD.parser_key, OLD.parser_version, OLD.provider_key, OLD.provider_version,
        OLD.scorer_key, OLD.scorer_version, OLD.config, OLD.created_at) THEN
        RAISE EXCEPTION 'labeling definition %@% is immutable', OLD.definition_key, OLD.definition_version
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_vor_guard_definition ON labeling_definitions;
CREATE TRIGGER trg_vor_guard_definition
    BEFORE UPDATE OR DELETE ON labeling_definitions
    FOR EACH ROW EXECUTE FUNCTION vor_guard_definition();

CREATE OR REPLACE FUNCTION vor_guard_target() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF EXISTS (SELECT 1 FROM labeling_predictions WHERE id = OLD.prediction_id AND leaf_sha256 IS NOT NULL) THEN
        IF TG_OP = 'DELETE' THEN
            RAISE EXCEPTION 'committed labeling targets are append-only' USING ERRCODE = 'check_violation';
        END IF;
        IF (NEW.id, NEW.prediction_id, NEW.org_id, NEW.item_key, NEW.item_payload,
            NEW.window_start, NEW.window_end, NEW.created_at)
           IS DISTINCT FROM
           (OLD.id, OLD.prediction_id, OLD.org_id, OLD.item_key, OLD.item_payload,
            OLD.window_start, OLD.window_end, OLD.created_at) THEN
            RAISE EXCEPTION 'committed labeling target content is immutable' USING ERRCODE = 'check_violation';
        END IF;
    END IF;
    IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_vor_guard_target ON labeling_targets;
CREATE TRIGGER trg_vor_guard_target
    BEFORE UPDATE OR DELETE ON labeling_targets
    FOR EACH ROW EXECUTE FUNCTION vor_guard_target();

CREATE OR REPLACE FUNCTION vor_guard_result() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM labeling_targets t JOIN labeling_predictions p ON p.id = t.prediction_id
        WHERE t.id = OLD.target_id AND p.leaf_sha256 IS NOT NULL
    ) THEN
        RAISE EXCEPTION 'committed labeling results are append-only' USING ERRCODE = 'check_violation';
    END IF;
    IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_vor_guard_result ON labeling_results;
CREATE TRIGGER trg_vor_guard_result
    BEFORE UPDATE OR DELETE ON labeling_results
    FOR EACH ROW EXECUTE FUNCTION vor_guard_result();

CREATE OR REPLACE FUNCTION vor_guard_observation() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM labeling_results r
        JOIN labeling_targets t ON t.id = r.target_id
        JOIN labeling_predictions p ON p.id = t.prediction_id
        WHERE r.observation_id = OLD.id AND p.leaf_sha256 IS NOT NULL
    ) THEN
        RAISE EXCEPTION 'committed labeling evidence is immutable' USING ERRCODE = 'check_violation';
    END IF;
    IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_vor_guard_observation ON labeling_observations;
CREATE TRIGGER trg_vor_guard_observation
    BEFORE UPDATE OR DELETE ON labeling_observations
    FOR EACH ROW EXECUTE FUNCTION vor_guard_observation();

-- Chainlink ETH/USD standard proxy and the Uniswap v3 WETH/USDC 0.05% pool on Base.
-- Pool price is token1 (USDC, 6 decimals) per token0 (WETH, 18 decimals).
INSERT INTO labeling_definitions
    (definition_key, definition_version, prediction_schema, parser_key, parser_version,
     provider_key, provider_version, scorer_key, scorer_version, config)
VALUES
    (
        'oracle_deviation',
        1,
        '{
            "type": "object",
            "required": ["deviation_bps"],
            "properties": {
                "task_class": {"const": "oracle_deviation"},
                "deviation_bps": {
                    "type": "object",
                    "additionalProperties": false,
                    "required": ["lt25", "b25_50", "b50_100", "b100_200", "gt200"],
                    "properties": {
                        "lt25": {"type": "number", "minimum": 0, "maximum": 1},
                        "b25_50": {"type": "number", "minimum": 0, "maximum": 1},
                        "b50_100": {"type": "number", "minimum": 0, "maximum": 1},
                        "b100_200": {"type": "number", "minimum": 0, "maximum": 1},
                        "gt200": {"type": "number", "minimum": 0, "maximum": 1}
                    }
                }
            }
        }'::jsonb,
        'oracle_rounds', '1', 'oracle_deviation', '1', 'oracle_deviation_brier', '1',
        '{
            "public": true,
            "chain_id": 8453,
            "feed": "0x71041dddad3595f9ced3dccfbe3d1f4b0a16bb70",
            "feed_decimals": 8,
            "pool": "0xd0b53d9277642d899df5c87a3966a349a798f224",
            "token0_decimals": 18,
            "token1_decimals": 6,
            "twap_seconds": 1800,
            "round_seconds": 86400,
            "horizon_seconds": 86400,
            "finality_seconds": 1800,
            "buckets_bps": [25, 50, 100, 200],
            "max_stale_seconds": 3600,
            "min_sample": 30
        }'::jsonb
    )
ON CONFLICT (definition_key, definition_version) DO NOTHING;
