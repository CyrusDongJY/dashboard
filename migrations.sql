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
    pre.as_of_time      AS pre_as_of_time
FROM stock_options_pre_market pre
FULL OUTER JOIN stock_spot_post_close post
    ON pre.date = post.date AND pre.ticker = post.ticker;

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
    scope           text,                   -- 标的或 MACRO（如 SPY / MACRO）
    window          text        NOT NULL,   -- 1D / 5D / 21D / 63D
    value           numeric,
    change          numeric,                -- 窗口内变化
    zscore          numeric,                -- 相对 252D 基准
    percentile      numeric,                -- 252D 历史分位 0-100
    direction       text,                   -- up / down
    severity        int         DEFAULT 0,  -- 0-3
    confidence      numeric,                -- 0-1，按新鲜度+样本量加权
    source_date     date,
    lag_days        int         DEFAULT 0,
    layer           text,                   -- single / multiwindow / resonance
    resonance_key   text,                   -- 共振主题（如 high_risk_selloff）
    explanation     text,
    created_at      timestamptz,
    UNIQUE (report_date, metric, scope, window, layer)
);
CREATE INDEX IF NOT EXISTS idx_anomaly_report_date ON anomaly_events (report_date DESC);
CREATE INDEX IF NOT EXISTS idx_anomaly_severity ON anomaly_events (severity DESC);
