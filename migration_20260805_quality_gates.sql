BEGIN;

ALTER TABLE stock_options_pre_market ADD COLUMN IF NOT EXISTS expected_move_decision_quality text;
ALTER TABLE stock_options_pre_market ADD COLUMN IF NOT EXISTS expected_move_decision_status text;
ALTER TABLE stock_options_pre_market ADD COLUMN IF NOT EXISTS expected_move_decision_eligible boolean;
ALTER TABLE stock_options_pre_market ADD COLUMN IF NOT EXISTS expected_move_reliability_score numeric;
ALTER TABLE stock_options_pre_market ADD COLUMN IF NOT EXISTS expected_move_quality_scores jsonb;
ALTER TABLE stock_options_pre_market ADD COLUMN IF NOT EXISTS expected_move_quote_coverage_pct numeric;
ALTER TABLE stock_options_pre_market ADD COLUMN IF NOT EXISTS expected_move_quote_max_age_seconds numeric;
ALTER TABLE stock_options_pre_market ADD COLUMN IF NOT EXISTS expected_move_premarket_gap_pct numeric;
ALTER TABLE stock_options_pre_market ADD COLUMN IF NOT EXISTS expected_move_gap_consumed_pct numeric;
ALTER TABLE stock_options_pre_market ADD COLUMN IF NOT EXISTS expected_move_event_status text;
ALTER TABLE stock_options_pre_market ADD COLUMN IF NOT EXISTS gamma_qualification_coverage_pct numeric;
ALTER TABLE stock_options_pre_market ADD COLUMN IF NOT EXISTS gamma_atm_coverage_pct numeric;
ALTER TABLE stock_options_pre_market ADD COLUMN IF NOT EXISTS gamma_call_put_balance_pct numeric;
ALTER TABLE stock_options_pre_market ADD COLUMN IF NOT EXISTS gamma_input_oi_weight_coverage_pct numeric;
ALTER TABLE stock_options_pre_market ADD COLUMN IF NOT EXISTS gamma_dollar_coverage_pct numeric;
ALTER TABLE stock_options_pre_market ADD COLUMN IF NOT EXISTS gamma_dollar_coverage_status text;

ALTER TABLE option_gamma_buckets ADD COLUMN IF NOT EXISTS qualification_coverage_pct numeric;
ALTER TABLE option_gamma_buckets ADD COLUMN IF NOT EXISTS atm_coverage_pct numeric;
ALTER TABLE option_gamma_buckets ADD COLUMN IF NOT EXISTS call_put_balance_pct numeric;
ALTER TABLE option_gamma_buckets ADD COLUMN IF NOT EXISTS gamma_input_oi_weight_coverage_pct numeric;
ALTER TABLE option_gamma_buckets ADD COLUMN IF NOT EXISTS gamma_dollar_coverage_pct numeric;
ALTER TABLE option_gamma_buckets ADD COLUMN IF NOT EXISTS gamma_dollar_coverage_status text;

ALTER TABLE macro_spot_daily ADD COLUMN IF NOT EXISTS vix_curve_state text;
ALTER TABLE macro_spot_daily ADD COLUMN IF NOT EXISTS vix_curve_flat_abs_threshold numeric;
ALTER TABLE macro_spot_daily ADD COLUMN IF NOT EXISTS vix_curve_flat_pct_threshold numeric;

ALTER TABLE option_volume_anomalies ADD COLUMN IF NOT EXISTS same_snapshot_pattern text;
ALTER TABLE option_volume_anomalies ADD COLUMN IF NOT EXISTS verification_attempt_date date;
ALTER TABLE option_volume_anomalies ADD COLUMN IF NOT EXISTS verification_status text;

COMMIT;
