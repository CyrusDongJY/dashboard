-- ============================================================
-- migrations.sql — 盘后多窗口异常汇总系统 数据库升级
-- 在 Supabase SQL Editor 中执行。全部使用 IF NOT EXISTS，可重复运行。
-- ============================================================

-- ------------------------------------------------------------
-- 1. 数据新鲜度元数据列：给现有表补齐 source_date / as_of_time / ingested_at
-- ------------------------------------------------------------
ALTER TABLE market_history      ADD COLUMN IF NOT EXISTS source_date date;
ALTER TABLE market_history      ADD COLUMN IF NOT EXISTS as_of_time  timestamptz;
ALTER TABLE market_history      ADD COLUMN IF NOT EXISTS ingested_at timestamptz;

ALTER TABLE macro_spot_daily    ADD COLUMN IF NOT EXISTS source_date date;
ALTER TABLE macro_spot_daily    ADD COLUMN IF NOT EXISTS as_of_time  timestamptz;
ALTER TABLE macro_spot_daily    ADD COLUMN IF NOT EXISTS ingested_at timestamptz;

ALTER TABLE macro_options_daily ADD COLUMN IF NOT EXISTS source_date date;
ALTER TABLE macro_options_daily ADD COLUMN IF NOT EXISTS as_of_time  timestamptz;
ALTER TABLE macro_options_daily ADD COLUMN IF NOT EXISTS ingested_at timestamptz;

-- ------------------------------------------------------------
-- 2. 拆分 stock_options_daily -> 盘前 / 盘后 两张独立表
--    避免"同一行字段属于不同时点"的错配。
--    盘前：GEX/Gamma/Vanna/Charm/ZGL/Wall/DPSV 等（08:00 截面）
--    盘后：POC/OBV/IVR 等（16:00 截面）
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS stock_options_pre_market (
    date            date        NOT NULL,
    ticker          text        NOT NULL,
    current_price   numeric,
    zgl_price       numeric,
    call_wall       numeric,
    put_wall        numeric,
    vanna_m         numeric,
    charm_m         numeric,
    expected_move_pct numeric,
    max_oi_strike   numeric,
    max_oi_type     text,
    iv_skew         numeric,
    short_gamma_m   numeric,
    long_gamma_m    numeric,
    oi_pcr          numeric,
    dpsv_pct        numeric,
    dpsv_source_date date,          -- FINRA 文件真实日期（可能滞后于 date）
    source_date     date,
    as_of_time      timestamptz,
    ingested_at     timestamptz,
    PRIMARY KEY (date, ticker)
);

-- V9.2 盘前价格与Gamma口径：旧 current_price 仅保留兼容，禁止再解释为实时价。
ALTER TABLE stock_options_pre_market ADD COLUMN IF NOT EXISTS previous_close numeric;
ALTER TABLE stock_options_pre_market ADD COLUMN IF NOT EXISTS previous_close_date date;
ALTER TABLE stock_options_pre_market ADD COLUMN IF NOT EXISTS premarket_last numeric;
ALTER TABLE stock_options_pre_market ADD COLUMN IF NOT EXISTS premarket_bid numeric;
ALTER TABLE stock_options_pre_market ADD COLUMN IF NOT EXISTS premarket_ask numeric;
ALTER TABLE stock_options_pre_market ADD COLUMN IF NOT EXISTS premarket_mid numeric;
ALTER TABLE stock_options_pre_market ADD COLUMN IF NOT EXISTS premarket_reference_price numeric;
ALTER TABLE stock_options_pre_market ADD COLUMN IF NOT EXISTS premarket_price_source text;
ALTER TABLE stock_options_pre_market ADD COLUMN IF NOT EXISTS market_data_type smallint;
ALTER TABLE stock_options_pre_market ADD COLUMN IF NOT EXISTS quote_as_of timestamptz;
ALTER TABLE stock_options_pre_market ADD COLUMN IF NOT EXISTS futures_symbol text;
ALTER TABLE stock_options_pre_market ADD COLUMN IF NOT EXISTS futures_reference_price numeric;
ALTER TABLE stock_options_pre_market ADD COLUMN IF NOT EXISTS futures_as_of timestamptz;
ALTER TABLE stock_options_pre_market ADD COLUMN IF NOT EXISTS distance_to_call_wall_pct numeric;
ALTER TABLE stock_options_pre_market ADD COLUMN IF NOT EXISTS distance_to_put_wall_pct numeric;
ALTER TABLE stock_options_pre_market ADD COLUMN IF NOT EXISTS distance_to_zgl_pct numeric;
ALTER TABLE stock_options_pre_market ADD COLUMN IF NOT EXISTS pin_strike numeric;
ALTER TABLE stock_options_pre_market ADD COLUMN IF NOT EXISTS pin_state text;
ALTER TABLE stock_options_pre_market ADD COLUMN IF NOT EXISTS expected_move_value numeric;
ALTER TABLE stock_options_pre_market ADD COLUMN IF NOT EXISTS expected_move_source text;
ALTER TABLE stock_options_pre_market ADD COLUMN IF NOT EXISTS expected_move_dte numeric;
ALTER TABLE stock_options_pre_market ADD COLUMN IF NOT EXISTS expected_move_quality text;
ALTER TABLE stock_options_pre_market ADD COLUMN IF NOT EXISTS max_oi_expiry date;
ALTER TABLE stock_options_pre_market ADD COLUMN IF NOT EXISTS max_oi_count bigint;
ALTER TABLE stock_options_pre_market ADD COLUMN IF NOT EXISTS max_oi_delta numeric;
ALTER TABLE stock_options_pre_market ADD COLUMN IF NOT EXISTS max_oi_gamma_dollar_m numeric;
ALTER TABLE stock_options_pre_market ADD COLUMN IF NOT EXISTS max_oi_distance_pct numeric;
ALTER TABLE stock_options_pre_market ADD COLUMN IF NOT EXISTS gamma_0dte_m numeric;
ALTER TABLE stock_options_pre_market ADD COLUMN IF NOT EXISTS gamma_1_7d_m numeric;
ALTER TABLE stock_options_pre_market ADD COLUMN IF NOT EXISTS gamma_8_30d_m numeric;
ALTER TABLE stock_options_pre_market ADD COLUMN IF NOT EXISTS gamma_31_60d_m numeric;
ALTER TABLE stock_options_pre_market ADD COLUMN IF NOT EXISTS gamma_all_m numeric;
ALTER TABLE stock_options_pre_market ADD COLUMN IF NOT EXISTS gamma_flip_0dte numeric;
ALTER TABLE stock_options_pre_market ADD COLUMN IF NOT EXISTS gamma_flip_1_7d numeric;
ALTER TABLE stock_options_pre_market ADD COLUMN IF NOT EXISTS gamma_flip_8_30d numeric;
ALTER TABLE stock_options_pre_market ADD COLUMN IF NOT EXISTS gamma_flip_31_60d numeric;
ALTER TABLE stock_options_pre_market ADD COLUMN IF NOT EXISTS gamma_flip_all numeric;
ALTER TABLE stock_options_pre_market ADD COLUMN IF NOT EXISTS gamma_previous_flip numeric;
ALTER TABLE stock_options_pre_market ADD COLUMN IF NOT EXISTS gamma_flip_change_pct numeric;
ALTER TABLE stock_options_pre_market ADD COLUMN IF NOT EXISTS gamma_roll_changed boolean;
ALTER TABLE stock_options_pre_market ADD COLUMN IF NOT EXISTS gamma_flip_quality text;
ALTER TABLE stock_options_pre_market ADD COLUMN IF NOT EXISTS gamma_zeroes jsonb;
ALTER TABLE stock_options_pre_market ADD COLUMN IF NOT EXISTS gamma_expirations jsonb;
ALTER TABLE stock_options_pre_market ADD COLUMN IF NOT EXISTS gamma_curve_version text;
ALTER TABLE stock_options_pre_market ADD COLUMN IF NOT EXISTS gamma_grid_width_pct numeric;
ALTER TABLE stock_options_pre_market ADD COLUMN IF NOT EXISTS gamma_grid_points int;
ALTER TABLE stock_options_pre_market ADD COLUMN IF NOT EXISTS gamma_max_dte int;
ALTER TABLE stock_options_pre_market ADD COLUMN IF NOT EXISTS gamma_sign_model text;
ALTER TABLE stock_options_pre_market ADD COLUMN IF NOT EXISTS oi_source_date date;
ALTER TABLE stock_options_pre_market ADD COLUMN IF NOT EXISTS distance_sign_version text;
ALTER TABLE stock_options_pre_market ADD COLUMN IF NOT EXISTS gamma_quality jsonb;
ALTER TABLE stock_options_pre_market ADD COLUMN IF NOT EXISTS gamma_requested_contract_count int;
ALTER TABLE stock_options_pre_market ADD COLUMN IF NOT EXISTS gamma_qualified_contract_count int;
ALTER TABLE stock_options_pre_market ADD COLUMN IF NOT EXISTS gamma_oi_valid_contract_count int;
ALTER TABLE stock_options_pre_market ADD COLUMN IF NOT EXISTS gamma_valid_contract_count int;
ALTER TABLE stock_options_pre_market ADD COLUMN IF NOT EXISTS monthly_wall_expiry date;
ALTER TABLE stock_options_pre_market ADD COLUMN IF NOT EXISTS monthly_call_wall numeric;
ALTER TABLE stock_options_pre_market ADD COLUMN IF NOT EXISTS monthly_put_wall numeric;
ALTER TABLE stock_options_pre_market ADD COLUMN IF NOT EXISTS monthly_call_wall_oi bigint;
ALTER TABLE stock_options_pre_market ADD COLUMN IF NOT EXISTS monthly_put_wall_oi bigint;
ALTER TABLE stock_options_pre_market ADD COLUMN IF NOT EXISTS monthly_wall_oi_source_date date;
ALTER TABLE stock_options_pre_market ADD COLUMN IF NOT EXISTS monthly_wall_method text;
ALTER TABLE stock_options_pre_market ADD COLUMN IF NOT EXISTS monthly_wall_quality text;

CREATE TABLE IF NOT EXISTS option_gamma_buckets (
    date                date        NOT NULL,
    ticker              text        NOT NULL,
    bucket              text        NOT NULL
                                    CHECK (bucket IN ('0DTE','1-7D','8-30D','31-60D','ALL')),
    spot_reference      numeric,
    net_gamma_m         numeric,
    primary_flip        numeric,
    zero_points         jsonb,
    expirations         jsonb,
    contract_count      int         DEFAULT 0,
    expiration_count    int         DEFAULT 0,
    curve_version       text,
    grid_width_pct      numeric,
    grid_points         int,
    sign_model          text,
    source_date         date,
    as_of_time          timestamptz,
    ingested_at         timestamptz,
    PRIMARY KEY (date, ticker, bucket)
);
CREATE INDEX IF NOT EXISTS idx_option_gamma_buckets_ticker_date
    ON option_gamma_buckets (ticker, date DESC);
ALTER TABLE option_gamma_buckets ADD COLUMN IF NOT EXISTS curve_version text;
ALTER TABLE option_gamma_buckets ADD COLUMN IF NOT EXISTS grid_width_pct numeric;
ALTER TABLE option_gamma_buckets ADD COLUMN IF NOT EXISTS grid_points int;
ALTER TABLE option_gamma_buckets ADD COLUMN IF NOT EXISTS requested_contract_count int;
ALTER TABLE option_gamma_buckets ADD COLUMN IF NOT EXISTS qualified_contract_count int;
ALTER TABLE option_gamma_buckets ADD COLUMN IF NOT EXISTS oi_valid_contract_count int;
ALTER TABLE option_gamma_buckets ADD COLUMN IF NOT EXISTS coverage_pct numeric;
ALTER TABLE option_gamma_buckets ADD COLUMN IF NOT EXISTS call_count int;
ALTER TABLE option_gamma_buckets ADD COLUMN IF NOT EXISTS put_count int;
ALTER TABLE option_gamma_buckets ADD COLUMN IF NOT EXISTS strike_count int;
ALTER TABLE option_gamma_buckets ADD COLUMN IF NOT EXISTS quality text;
ALTER TABLE option_gamma_buckets ADD COLUMN IF NOT EXISTS raw_net_gamma_m numeric;
ALTER TABLE option_gamma_buckets ADD COLUMN IF NOT EXISTS raw_primary_flip numeric;

CREATE TABLE IF NOT EXISTS stock_spot_post_close (
    date            date        NOT NULL,
    ticker          text        NOT NULL,
    current_price   numeric,        -- 盘后收盘价
    poc_price       numeric,
    obv_status      text,
    ivr_pct         numeric,
    source_date     date,
    as_of_time      timestamptz,
    ingested_at     timestamptz,
    PRIMARY KEY (date, ticker)
);
ALTER TABLE stock_spot_post_close ADD COLUMN IF NOT EXISTS iv_rank_pct numeric;
ALTER TABLE stock_spot_post_close ADD COLUMN IF NOT EXISTS iv_percentile_pct numeric;
ALTER TABLE stock_spot_post_close ADD COLUMN IF NOT EXISTS current_iv numeric;
ALTER TABLE stock_spot_post_close ADD COLUMN IF NOT EXISTS poc_method text;
ALTER TABLE stock_spot_post_close ADD COLUMN IF NOT EXISTS poc_window text;
ALTER TABLE stock_spot_post_close ADD COLUMN IF NOT EXISTS poc_source_date date;

-- ------------------------------------------------------------
-- 3. 汇总视图：把盘前盘后按 (date,ticker) 对齐，供报告/回测统一读取
--    字段来源标注清楚，各自保留自己的 as_of_time。
-- ------------------------------------------------------------
CREATE OR REPLACE VIEW stock_options_unified AS
SELECT
    COALESCE(pre.date, post.date)     AS date,
    COALESCE(pre.ticker, post.ticker) AS ticker,
    -- 盘后现货（16:00 截面）
    post.current_price  AS close_price,
    post.poc_price,
    post.obv_status,
    post.ivr_pct,
    post.as_of_time     AS post_as_of_time,
    -- 盘前期权（08:00 截面）
    pre.current_price   AS pre_price,
    pre.zgl_price,
    pre.call_wall,
    pre.put_wall,
    pre.vanna_m,
    pre.charm_m,
    pre.expected_move_pct,
    pre.max_oi_strike,
    pre.max_oi_type,
    pre.iv_skew,
    pre.short_gamma_m,
    pre.long_gamma_m,
    pre.oi_pcr,
    pre.dpsv_pct,
    pre.dpsv_source_date,
    pre.as_of_time      AS pre_as_of_time,
    -- V9.2 明确盘前价格、报价质量和结构指标
    pre.previous_close,
    pre.previous_close_date,
    pre.premarket_last,
    pre.premarket_bid,
    pre.premarket_ask,
    pre.premarket_mid,
    pre.premarket_reference_price,
    pre.premarket_price_source,
    pre.market_data_type,
    pre.quote_as_of,
    pre.futures_symbol,
    pre.futures_reference_price,
    pre.futures_as_of,
    pre.distance_to_call_wall_pct,
    pre.distance_to_put_wall_pct,
    pre.distance_to_zgl_pct,
    pre.pin_strike,
    pre.pin_state,
    pre.expected_move_value,
    pre.expected_move_source,
    pre.expected_move_dte,
    pre.expected_move_quality,
    pre.max_oi_expiry,
    pre.max_oi_count,
    pre.max_oi_delta,
    pre.max_oi_gamma_dollar_m,
    pre.max_oi_distance_pct,
    pre.gamma_0dte_m,
    pre.gamma_1_7d_m,
    pre.gamma_8_30d_m,
    pre.gamma_31_60d_m,
    pre.gamma_all_m,
    pre.gamma_flip_0dte,
    pre.gamma_flip_1_7d,
    pre.gamma_flip_8_30d,
    pre.gamma_flip_31_60d,
    pre.gamma_flip_all,
    pre.gamma_previous_flip,
    pre.gamma_flip_change_pct,
    pre.gamma_roll_changed,
    pre.gamma_flip_quality,
    pre.gamma_zeroes,
    pre.gamma_expirations,
    pre.gamma_sign_model,
    pre.oi_source_date,
    -- 新增列只追加在视图末尾，保证 CREATE OR REPLACE 不改变旧列顺序。
    post.iv_rank_pct,
    post.iv_percentile_pct,
    post.current_iv,
    pre.gamma_curve_version,
    pre.gamma_grid_width_pct,
    pre.gamma_grid_points,
    pre.gamma_max_dte,
    pre.distance_sign_version,
    pre.gamma_quality,
    pre.gamma_requested_contract_count,
    pre.gamma_qualified_contract_count,
    pre.gamma_oi_valid_contract_count,
    pre.gamma_valid_contract_count,
    post.poc_method,
    post.poc_window,
    post.poc_source_date,
    pre.monthly_wall_expiry,
    pre.monthly_call_wall,
    pre.monthly_put_wall,
    pre.monthly_call_wall_oi,
    pre.monthly_put_wall_oi,
    pre.monthly_wall_oi_source_date,
    pre.monthly_wall_method,
    pre.monthly_wall_quality
FROM stock_options_pre_market pre
FULL OUTER JOIN stock_spot_post_close post
    ON pre.date = post.date AND pre.ticker = post.ticker;

-- 盘中高频切片的数据质量与上下文血缘。缺数必须写 NULL + 状态，禁止写中性默认值。
ALTER TABLE IF EXISTS intraday_logs ADD COLUMN IF NOT EXISTS vol_ratio_status text;
ALTER TABLE IF EXISTS intraday_logs ADD COLUMN IF NOT EXISTS trin_scope text;
ALTER TABLE IF EXISTS intraday_logs ADD COLUMN IF NOT EXISTS trin_source text;
ALTER TABLE IF EXISTS intraday_logs ADD COLUMN IF NOT EXISTS context_quality text;
ALTER TABLE IF EXISTS intraday_logs ADD COLUMN IF NOT EXISTS context_metadata jsonb;

-- 2026-07-24: ADV/DECL/UVOL/DVOL were never valid IBKR contracts.  Older
-- collectors stored their missing zeros as a neutral-looking U/D=1.00.  Null
-- the fabricated values and retain an explicit, idempotent quality marker.
DO $$
BEGIN
    IF to_regclass('public.intraday_logs') IS NOT NULL THEN
        UPDATE intraday_logs
        SET add_val = NULL,
            uvol = NULL,
            dvol = NULL,
            vol_ratio = NULL,
            vol_ratio_status = 'LEGACY_INVALID_IBKR_CONTRACT',
            context_metadata = COALESCE(context_metadata, '{}'::jsonb) ||
                jsonb_build_object(
                    'legacy_breadth_quality', 'INVALID_IBKR_CONTRACT',
                    'legacy_breadth_remediated_at', '2026-07-24'
                )
        WHERE record_time < '2026-07-24 00:00:00'
          AND COALESCE(uvol, 0) = 0
          AND COALESCE(dvol, 0) = 0
          AND (vol_ratio = 1 OR vol_ratio IS NULL)
          AND COALESCE(vol_ratio_status, '') IN
              ('', 'OK', 'MISSING_UVOL_DVOL');
    END IF;
END $$;

-- 广度字段保留数值、样本和截面，hindenburg 仅作旧字段兼容。
ALTER TABLE market_history ADD COLUMN IF NOT EXISTS new_highs numeric;
ALTER TABLE market_history ADD COLUMN IF NOT EXISTS new_lows numeric;
ALTER TABLE market_history ADD COLUMN IF NOT EXISTS net_nh_nl numeric;
ALTER TABLE market_history ADD COLUMN IF NOT EXISTS trin_scope text;
ALTER TABLE market_history ADD COLUMN IF NOT EXISTS trin_source text;
ALTER TABLE market_history ADD COLUMN IF NOT EXISTS trin_as_of timestamptz;
ALTER TABLE market_history ADD COLUMN IF NOT EXISTS trin_closing_auction_inclusion text;
ALTER TABLE market_history ADD COLUMN IF NOT EXISTS breadth_sample_size int;
ALTER TABLE market_history ADD COLUMN IF NOT EXISTS pct_adv numeric;
ALTER TABLE market_history ADD COLUMN IF NOT EXISTS up_down_volume_ratio numeric;
ALTER TABLE market_history ADD COLUMN IF NOT EXISTS qqq_qqqe_spread_pct numeric;
ALTER TABLE market_history ADD COLUMN IF NOT EXISTS spy_rsp_spread_pct numeric;
ALTER TABLE market_history ADD COLUMN IF NOT EXISTS mag7_rsp_spread_pct numeric;
ALTER TABLE market_history ADD COLUMN IF NOT EXISTS concentration_quality text;
ALTER TABLE market_history ADD COLUMN IF NOT EXISTS hyg_return_21d numeric;
ALTER TABLE market_history ADD COLUMN IF NOT EXISTS tlt_return_21d numeric;
ALTER TABLE market_history ADD COLUMN IF NOT EXISTS hyg_tlt_state text;
ALTER TABLE market_history ADD COLUMN IF NOT EXISTS liq_roc_window int;
ALTER TABLE market_history ADD COLUMN IF NOT EXISTS dix_gex_source_date date;
ALTER TABLE market_history ADD COLUMN IF NOT EXISTS cot_report_date date;
ALTER TABLE market_history ADD COLUMN IF NOT EXISTS cot_category text;
ALTER TABLE market_history ADD COLUMN IF NOT EXISTS cot_metadata jsonb;

-- EOD跨资产战术压力：稳定列名 + 独立版本字段。旧 micro_score 仅作迁移兼容。
ALTER TABLE market_history ADD COLUMN IF NOT EXISTS eod_stress_score numeric;
ALTER TABLE market_history ADD COLUMN IF NOT EXISTS stress_components jsonb;
ALTER TABLE market_history ADD COLUMN IF NOT EXISTS stress_coverage numeric;
ALTER TABLE market_history ADD COLUMN IF NOT EXISTS stress_confidence numeric;
ALTER TABLE market_history ADD COLUMN IF NOT EXISTS stress_calc_version text;
ALTER TABLE market_history ADD COLUMN IF NOT EXISTS stress_computed_at timestamptz;

-- 盘后现金市场接受度与集中度影子指标。
ALTER TABLE macro_spot_daily ADD COLUMN IF NOT EXISTS qqq_qqqe_spread_pct numeric;
ALTER TABLE macro_spot_daily ADD COLUMN IF NOT EXISTS spy_rsp_spread_pct numeric;
ALTER TABLE macro_spot_daily ADD COLUMN IF NOT EXISTS mag7_rsp_spread_pct numeric;
ALTER TABLE macro_spot_daily ADD COLUMN IF NOT EXISTS mag7_sample_count int;
ALTER TABLE macro_spot_daily ADD COLUMN IF NOT EXISTS concentration_quality text;
ALTER TABLE macro_spot_daily ADD COLUMN IF NOT EXISTS spy_gap_pct numeric;
ALTER TABLE macro_spot_daily ADD COLUMN IF NOT EXISTS spy_gap_acceptance numeric;
ALTER TABLE macro_spot_daily ADD COLUMN IF NOT EXISTS spy_gap_quality text;
ALTER TABLE macro_spot_daily ADD COLUMN IF NOT EXISTS qqq_gap_pct numeric;
ALTER TABLE macro_spot_daily ADD COLUMN IF NOT EXISTS qqq_gap_acceptance numeric;
ALTER TABLE macro_spot_daily ADD COLUMN IF NOT EXISTS qqq_gap_quality text;
ALTER TABLE macro_spot_daily ADD COLUMN IF NOT EXISTS spy_vwap_time_acceptance_pct numeric;
ALTER TABLE macro_spot_daily ADD COLUMN IF NOT EXISTS spy_vwap_volume_acceptance_pct numeric;
ALTER TABLE macro_spot_daily ADD COLUMN IF NOT EXISTS spy_vwap_sample_count int;
ALTER TABLE macro_spot_daily ADD COLUMN IF NOT EXISTS spy_vwap_quality text;
ALTER TABLE macro_spot_daily ADD COLUMN IF NOT EXISTS qqq_vwap_time_acceptance_pct numeric;
ALTER TABLE macro_spot_daily ADD COLUMN IF NOT EXISTS qqq_vwap_volume_acceptance_pct numeric;
ALTER TABLE macro_spot_daily ADD COLUMN IF NOT EXISTS qqq_vwap_sample_count int;
ALTER TABLE macro_spot_daily ADD COLUMN IF NOT EXISTS qqq_vwap_quality text;

-- ------------------------------------------------------------
-- 4. 数据质量表：每次抓取每张表一条，记录成功/缺失/滞后/行数
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS data_quality (
    id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_date        date        NOT NULL,
    job_name        text        NOT NULL,   -- daily_post_close / daily_pre_market / auto_review ...
    table_name      text,
    status          text        NOT NULL,   -- ok / partial / failed
    rows_written    int         DEFAULT 0,
    missing_fields  text,                   -- 逗号分隔
    lag_days        int         DEFAULT 0,  -- 数据滞后交易日数
    anomaly_count   int         DEFAULT 0,
    notes           text,
    logged_at       timestamptz
);
CREATE INDEX IF NOT EXISTS idx_data_quality_run_date ON data_quality (run_date DESC);

-- ------------------------------------------------------------
-- 5. 异常事件表：三层异常引擎的结构化输出（可回测、可调参）
--    每个 (报告日, 指标, 观察窗口) 一条。
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS anomaly_events (
    id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    report_date     date        NOT NULL,   -- 生成异常的交易日
    metric          text        NOT NULL,   -- vix_contango / move / credit_spread / dpsv_pct ...
    scope           text        NOT NULL DEFAULT 'MACRO', -- 标的或 MACRO（如 SPY / MACRO）
    window_scope    text        NOT NULL,   -- 1D / 5D / 21D / 63D （window 是 PG 保留字，故用 window_scope）
    value           numeric,
    change          numeric,                -- 窗口内变化
    zscore          numeric,                -- 相对 252D 基准
    percentile      numeric,                -- 252D 历史分位 0-100
    direction       text,                   -- up / down
    severity        int         DEFAULT 0,  -- 0-3
    confidence      numeric,                -- 0-1，按新鲜度+样本量加权
    source_date     date,
    lag_days        int         DEFAULT 0,
    layer           text        NOT NULL DEFAULT 'single', -- single / multiwindow / resonance
    resonance_key   text        NOT NULL DEFAULT '', -- 共振主题（非共振事件为空串）
    explanation     text,
    created_at      timestamptz
);
CREATE INDEX IF NOT EXISTS idx_anomaly_report_date ON anomaly_events (report_date DESC);
CREATE INDEX IF NOT EXISTS idx_anomaly_severity ON anomaly_events (severity DESC);

-- 旧版本唯一键不含 resonance_key，会让同日多个共振主题互相冲突。
UPDATE anomaly_events SET resonance_key = '' WHERE resonance_key IS NULL;
UPDATE anomaly_events SET scope = 'MACRO' WHERE scope IS NULL;
UPDATE anomaly_events SET layer = 'single' WHERE layer IS NULL;
ALTER TABLE anomaly_events ALTER COLUMN scope SET DEFAULT 'MACRO';
ALTER TABLE anomaly_events ALTER COLUMN scope SET NOT NULL;
ALTER TABLE anomaly_events ALTER COLUMN layer SET DEFAULT 'single';
ALTER TABLE anomaly_events ALTER COLUMN layer SET NOT NULL;
ALTER TABLE anomaly_events ALTER COLUMN resonance_key SET DEFAULT '';
ALTER TABLE anomaly_events ALTER COLUMN resonance_key SET NOT NULL;
ALTER TABLE anomaly_events
    DROP CONSTRAINT IF EXISTS anomaly_events_report_date_metric_scope_window_scope_layer_key;
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'uq_anomaly_event_identity'
    ) THEN
        ALTER TABLE anomaly_events ADD CONSTRAINT uq_anomaly_event_identity
            UNIQUE (report_date, metric, scope, window_scope, layer, resonance_key);
    END IF;
END $$;

-- ------------------------------------------------------------
-- 6. 哨兵运行台账：market_sentinel 每次评估的结果 + 预警去重指纹
--    (report_date, fingerprint) 唯一：同一交易日同一组触发指标只发一次预警。
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sentinel_runs (
    id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    report_date     date        NOT NULL,   -- 评估的交易日
    fingerprint     text        NOT NULL,   -- 预警指纹（clean=未触发预警）
    alerted         boolean     DEFAULT false, -- 是否真的发出了预警邮件
    n_events        int         DEFAULT 0,  -- 当次异常矩阵总条数
    reasons         text,                   -- 触发红线的原因摘要
    ran_at          timestamptz,
    UNIQUE (report_date, fingerprint)
);
CREATE INDEX IF NOT EXISTS idx_sentinel_report_date ON sentinel_runs (report_date DESC);

-- ------------------------------------------------------------
-- 7. 每日全指标快照：曲线 / 趋势 / 环境评估的地基。
--    与 anomaly_events 的区别：anomaly_events 只存"越线"的指标，
--    metric_daily 无条件存【每个指标每个交易日】的全派生向量，
--    不管它当天有没有越线 —— 这样才能画出连续曲线、算连续在险天数、
--    算复合环境指数。每个 (report_date, metric, scope, session) 一行。
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS metric_daily (
    id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    report_date     date        NOT NULL,   -- 快照对应的交易日
    metric          text        NOT NULL,   -- 指标键（vix / move / credit_spread ...）
    scope           text        NOT NULL DEFAULT 'MACRO', -- MACRO 或标的代码
    session         text        NOT NULL DEFAULT 'EOD', -- PRE / POST / EOD
    cn_name         text,                   -- 指标中文名（画图图例用）
    value           numeric,                -- 当日值
    chg_1d          numeric,                -- 1 交易日变化（绝对差）
    chg_5d          numeric,
    chg_21d         numeric,
    chg_63d         numeric,
    zscore          numeric,                -- 相对 252D 基准
    percentile      numeric,                -- 252D 历史分位 0-100
    severity        int         DEFAULT 0,  -- 当日该指标越线的最高 severity（0=未越线）
    dist_to_thr     numeric,                -- 距最近入场阈值的"安全余量"（正=安全，负=已越线；无绝对阈值则 NULL）
    days_in_risk    int,                    -- 连续处于风险区的交易日数（无绝对阈值则 NULL）
    sample_len      int,                    -- 参与统计的样本长度（判断 z/分位是否可信）
    effective_obs_count int,                -- 有效独立观测数（低频填充日不重复计数）
    source_date     date,                   -- 数据真实日期（区分滞后）
    source_name     text,                   -- market_history / FRED / yfinance / IBKR ...
    is_filled       boolean     DEFAULT false, -- 是否为低频数据向前填充的展示点
    is_final        boolean     DEFAULT false, -- 是否为盘后最终快照，回补不得覆盖
    lag_days        int         DEFAULT 0,
    calc_version    text        NOT NULL DEFAULT 'env_v2',
    created_at      timestamptz
);
CREATE INDEX IF NOT EXISTS idx_metric_daily_metric ON metric_daily (metric, report_date DESC);
CREATE INDEX IF NOT EXISTS idx_metric_daily_report_date ON metric_daily (report_date DESC);

-- 为已存在的 metric_daily 升级快照身份与质量字段。
ALTER TABLE metric_daily ADD COLUMN IF NOT EXISTS session text NOT NULL DEFAULT 'EOD';
ALTER TABLE metric_daily ADD COLUMN IF NOT EXISTS effective_obs_count int;
ALTER TABLE metric_daily ADD COLUMN IF NOT EXISTS source_name text;
ALTER TABLE metric_daily ADD COLUMN IF NOT EXISTS is_filled boolean DEFAULT false;
ALTER TABLE metric_daily ADD COLUMN IF NOT EXISTS is_final boolean DEFAULT false;
ALTER TABLE metric_daily ADD COLUMN IF NOT EXISTS calc_version text NOT NULL DEFAULT 'env_v2';
ALTER TABLE metric_daily ALTER COLUMN calc_version SET DEFAULT 'env_v2';
ALTER TABLE metric_daily
    DROP CONSTRAINT IF EXISTS metric_daily_report_date_metric_scope_key;
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'uq_metric_daily_identity'
    ) THEN
        ALTER TABLE metric_daily ADD CONSTRAINT uq_metric_daily_identity
            UNIQUE (report_date, metric, scope, session);
    END IF;
END $$;

-- ------------------------------------------------------------
-- 8. 每日复合环境指数：5 大面板归一化打分 + 共振计数 + 状态判定。
--    由 environment_indices.py 基于 metric_daily 计算，本身也可画曲线、可回测。
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS environment_daily (
    id                  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    report_date         date        NOT NULL,
    vol_pressure        numeric,            -- 波动率压力指数 0-100
    credit_stress       numeric,            -- 信用/流动性恶化指数 0-100
    breadth_decay       numeric,            -- 广度衰退指数 0-100
    options_fragility   numeric,            -- 期权脆弱性指数 0-100
    flow_risk           numeric,            -- 资金行为风险指数 0-100
    composite           numeric,            -- 综合环境压强 0-100
    resonance_count     int         DEFAULT 0, -- 当日跨资产共振命中数
    state               text,               -- 数据不足 / 常态观察 / 各类尾部观察
    coverage            numeric,            -- 数据覆盖度 0-1（多少指标有有效值）
    shadow_mode         boolean     NOT NULL DEFAULT true,
    calc_version        text        NOT NULL DEFAULT 'env_v2',
    created_at          timestamptz,
    UNIQUE (report_date)
);
CREATE INDEX IF NOT EXISTS idx_environment_report_date ON environment_daily (report_date DESC);
ALTER TABLE environment_daily ADD COLUMN IF NOT EXISTS shadow_mode boolean NOT NULL DEFAULT true;
ALTER TABLE environment_daily ADD COLUMN IF NOT EXISTS calc_version text NOT NULL DEFAULT 'env_v2';
ALTER TABLE environment_daily ALTER COLUMN calc_version SET DEFAULT 'env_v2';


-- ------------------------------------------------------------
-- 9. 市场流动性水位仪：六维影子评分 + 数据覆盖 + 可解释明细。
--    分值越高表示流动性支持越强；V2仍只用于观察，不接入 ALERT_GATE。
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS liquidity_daily (
    report_date          date        PRIMARY KEY,
    water_stock          numeric,
    flow_pulse           numeric,
    funding_health       numeric,
    credit_transmission  numeric,
    market_distribution  numeric,
    tail_resilience      numeric,
    composite            numeric,
    coverage             numeric,
    state                text,
    state_detail         text,
    supports             text,
    drags                text,
    warnings             text,
    details              jsonb,
    shadow_mode          boolean     NOT NULL DEFAULT true,
    calc_version         text        NOT NULL DEFAULT 'liquidity_v2',
    created_at           timestamptz
);
CREATE INDEX IF NOT EXISTS idx_liquidity_report_date
    ON liquidity_daily (report_date DESC);
ALTER TABLE liquidity_daily
    ADD COLUMN IF NOT EXISTS shadow_mode boolean NOT NULL DEFAULT true;
ALTER TABLE liquidity_daily
    ADD COLUMN IF NOT EXISTS calc_version text NOT NULL DEFAULT 'liquidity_v2';
ALTER TABLE liquidity_daily ALTER COLUMN calc_version SET DEFAULT 'liquidity_v2';
