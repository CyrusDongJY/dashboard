BEGIN;

ALTER TABLE stock_options_pre_market ADD COLUMN IF NOT EXISTS expected_move_event_name text;
ALTER TABLE stock_options_pre_market ADD COLUMN IF NOT EXISTS expected_move_event_at timestamptz;
ALTER TABLE stock_options_pre_market ADD COLUMN IF NOT EXISTS expected_move_event_trading_days int;
ALTER TABLE stock_options_pre_market ADD COLUMN IF NOT EXISTS expected_move_event_risk text;
ALTER TABLE stock_options_pre_market ADD COLUMN IF NOT EXISTS expected_move_event_source text;
ALTER TABLE stock_options_pre_market ADD COLUMN IF NOT EXISTS expected_move_event_source_status text;
ALTER TABLE stock_options_pre_market ADD COLUMN IF NOT EXISTS gamma_decision_eligible boolean;
ALTER TABLE stock_options_pre_market ADD COLUMN IF NOT EXISTS gamma_tactical_weight numeric;
ALTER TABLE stock_options_pre_market ADD COLUMN IF NOT EXISTS gamma_usage text;
ALTER TABLE stock_options_pre_market ADD COLUMN IF NOT EXISTS monthly_wall_usage text;

ALTER TABLE option_gamma_buckets ADD COLUMN IF NOT EXISTS decision_eligible boolean;
ALTER TABLE option_gamma_buckets ADD COLUMN IF NOT EXISTS tactical_weight numeric;

ALTER TABLE IF EXISTS intraday_logs ADD COLUMN IF NOT EXISTS breadth_state text;
ALTER TABLE IF EXISTS intraday_logs ADD COLUMN IF NOT EXISTS decision_framework text;

UPDATE stock_options_pre_market
SET expected_move_event_status = 'EVENT_CALENDAR_UNAVAILABLE',
    expected_move_event_source_status = 'LEGACY_UNVERIFIED',
    expected_move_decision_eligible = false,
    expected_move_reliability_score = 0,
    expected_move_quality_scores = jsonb_set(
        COALESCE(expected_move_quality_scores, '{}'::jsonb),
        '{event}', '0'::jsonb, true),
    expected_move_decision_quality = CASE
        WHEN expected_move_decision_quality IS NULL
          OR expected_move_decision_quality = 'OK'
        THEN 'EVENT_CALENDAR_UNAVAILABLE'
        ELSE expected_move_decision_quality || '|EVENT_CALENDAR_UNAVAILABLE'
    END,
    expected_move_decision_status = CASE
        WHEN expected_move_decision_status IS NULL
          OR expected_move_decision_status IN ('OK', 'REVIEW')
        THEN 'EVENT_CALENDAR_UNAVAILABLE'
        ELSE expected_move_decision_status
    END
WHERE expected_move_event_status IS NULL
   OR expected_move_event_status = 'UNKNOWN';

UPDATE stock_options_pre_market
SET gamma_decision_eligible = COALESCE(gamma_quality ->> 'ALL' = 'OK', false),
    gamma_tactical_weight = CASE
        WHEN gamma_quality ->> 'ALL' = 'OK' THEN 1 ELSE 0 END,
    gamma_usage = CASE
        WHEN gamma_quality ->> 'ALL' = 'OK'
        THEN 'GAMMA_DIRECTIONAL_STRUCTURE'
        ELSE 'PRICE_ACTIVITY_ZONE_ONLY'
    END,
    monthly_wall_usage = 'PRICE_ACTIVITY_ZONE_ONLY'
WHERE gamma_decision_eligible IS NULL
   OR gamma_tactical_weight IS NULL
   OR gamma_usage IS NULL
   OR monthly_wall_usage IS NULL;

UPDATE option_gamma_buckets
SET decision_eligible = (quality = 'OK'),
    tactical_weight = CASE WHEN quality = 'OK' THEN 1 ELSE 0 END
WHERE decision_eligible IS NULL OR tactical_weight IS NULL;

DO $$
BEGIN
    IF to_regclass('public.intraday_logs') IS NOT NULL THEN
        UPDATE intraday_logs
        SET breadth_state = CASE
                WHEN add_status = 'OK' THEN 'AVAILABLE' ELSE 'UNAVAILABLE' END,
            decision_framework = CASE
                WHEN add_status = 'OK' THEN 'NYSE_AD_PRICE_VWAP_TRIN_CTICK'
                ELSE 'PRICE_VWAP_TRIN_CTICK' END
        WHERE breadth_state IS NULL OR decision_framework IS NULL;
    END IF;
END $$;

COMMIT;
