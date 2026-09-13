BEGIN;

-- Unified cross-panel time state.  Values are derived from existing daily
-- source tables; this table does not replace source history or alert events.
CREATE TABLE IF NOT EXISTS temporal_state_daily (
    report_date                    date        NOT NULL,
    panel                          text        NOT NULL,
    level_value                    numeric,
    risk_value                     numeric,
    change_1d                      numeric,
    change_5d                      numeric,
    change_21d                     numeric,
    persistence_days               int         NOT NULL DEFAULT 0,
    temporal_state                 text        NOT NULL,
    temporal_state_cn              text        NOT NULL,
    transition_type                text,
    state_changed                  boolean     NOT NULL DEFAULT false,
    coverage                       numeric     NOT NULL DEFAULT 0,
    confidence                     numeric     NOT NULL DEFAULT 0,
    independent_confirmation_count int         NOT NULL DEFAULT 0,
    quality_status                 text        NOT NULL,
    source_date                    date,
    source_state                   text,
    observation_mode               text        NOT NULL,
    details                        jsonb       NOT NULL DEFAULT '{}'::jsonb,
    shadow_mode                    boolean     NOT NULL DEFAULT true,
    calc_version                   text        NOT NULL,
    computed_at                    timestamptz,
    created_at                     timestamptz DEFAULT now(),
    PRIMARY KEY (report_date, panel),
    CHECK (panel IN (
        'environment', 'liquidity', 'tactical_stress',
        'risk_capital', 'event_pulse')),
    CHECK (risk_value IS NULL OR risk_value BETWEEN 0 AND 100),
    CHECK (persistence_days >= 0),
    CHECK (coverage BETWEEN 0 AND 1),
    CHECK (confidence BETWEEN 0 AND 1),
    CHECK (independent_confirmation_count >= 0),
    CHECK (quality_status IN ('OK', 'PARTIAL', 'INSUFFICIENT')),
    CHECK (observation_mode IN ('REPLAY', 'LIVE_SHADOW'))
);

CREATE INDEX IF NOT EXISTS idx_temporal_panel_date
    ON temporal_state_daily (panel, report_date DESC);
CREATE INDEX IF NOT EXISTS idx_temporal_state_date
    ON temporal_state_daily (temporal_state, report_date DESC);
CREATE INDEX IF NOT EXISTS idx_temporal_mode_panel_date
    ON temporal_state_daily (observation_mode, panel, report_date DESC);

-- Outcome ledger.  A row exists only after the full future trading window is
-- available, so incomplete current signals cannot acquire partial outcomes.
CREATE TABLE IF NOT EXISTS temporal_shadow_evaluation (
    signal_date          date        NOT NULL,
    panel                text        NOT NULL,
    benchmark            text        NOT NULL,
    horizon_days         int         NOT NULL,
    temporal_state       text        NOT NULL,
    observation_mode     text        NOT NULL,
    risk_value           numeric,
    benchmark_close      numeric     NOT NULL,
    outcome_end_date     date        NOT NULL,
    forward_return_pct   numeric     NOT NULL,
    max_drawdown_pct     numeric     NOT NULL,
    realized_vol_pct     numeric,
    max_abs_gap_pct      numeric,
    downside_event       boolean     NOT NULL,
    signal_calc_version  text        NOT NULL,
    eval_calc_version    text        NOT NULL,
    created_at           timestamptz DEFAULT now(),
    PRIMARY KEY (signal_date, panel, benchmark, horizon_days),
    CHECK (panel IN (
        'environment', 'liquidity', 'tactical_stress',
        'risk_capital', 'event_pulse')),
    CHECK (benchmark IN ('QQQ', 'SPY')),
    CHECK (horizon_days IN (1, 5, 21)),
    CHECK (observation_mode IN ('REPLAY', 'LIVE_SHADOW')),
    CHECK (risk_value IS NULL OR risk_value BETWEEN 0 AND 100),
    CHECK (benchmark_close > 0),
    CHECK (max_drawdown_pct <= 0),
    CHECK (realized_vol_pct IS NULL OR realized_vol_pct >= 0),
    CHECK (max_abs_gap_pct IS NULL OR max_abs_gap_pct >= 0)
);

CREATE INDEX IF NOT EXISTS idx_temporal_eval_panel_horizon
    ON temporal_shadow_evaluation (
        panel, benchmark, horizon_days, temporal_state, signal_date DESC);
CREATE INDEX IF NOT EXISTS idx_temporal_eval_mode_date
    ON temporal_shadow_evaluation (observation_mode, signal_date DESC);

COMMIT;
