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
