# V9.0 升级说明 — 盘后多窗口异常汇总系统

本次升级把系统从"探针文本战报"升级为"客观异常矩阵"：同一指标看 1D/5D/21D/63D
观察窗口，以 252 个交易日为统计基准，跨指标做共振判定，并按数据新鲜度加权置信度。

## 一、部署步骤（按顺序）

1. **执行数据库迁移**：在 Supabase SQL Editor 中运行 [migrations.sql](migrations.sql)。
   全部语句幂等（IF NOT EXISTS），可重复执行。新增：
   - `stock_options_pre_market` / `stock_spot_post_close`（拆表）+ `stock_options_unified` 视图
   - `data_quality`（抓取质量审计）
   - `anomaly_events`（结构化异常矩阵，可回测）
   - 现有表补 `source_date` / `as_of_time` / `ingested_at` 三列

2. **部署代码**：把以下文件同步到服务器 `~/market_dashboard/`（与 `market_config.py` 同目录）：
   - 新增：`market_utils.py`、`anomaly_engine.py`
   - 覆盖：`ultimate_dashboard.py`、`auto_review.py`、`auto_analyst.py`、
     `market_probes.py`、`daily_pre_market.py`、`daily_post_close.py`、`ib_intraday_sniper.py`

3. **无需改动 cron**：各脚本入口、时间闸门、命令行参数不变。

## 二、修复的致命问题

| # | 问题 | 修复 |
|---|------|------|
| 1 | `calc_zscore` 默认 252D 窗口但只抓 6 个月数据，`rolling(252)` 全 NaN → **信用利差Z、铜金比Z 常年静默为 0** | 数据窗口拉到 2 年 + `min_periods`，252D 基准真正生效 |
| 2 | `market_history` 靠"打印文本→50条正则"入库，文案/emoji 一变就静默丢字段 | dashboard 新增 `get_db_payload()` 结构化直写；`auto_review` 改为 import 调用，正则链路废弃 |
| 3 | FINRA DPSV 向前回溯最多 5 天找文件，却按今天日期入库 → **滞后数据冒充当日异常** | 新增 `dpsv_source_date` 记录 FINRA 真实日期；报告显示滞后交易日数；告警按新鲜度降权 |
| 4 | 盘前/盘后 upsert 同一行 `stock_options_daily`，同一行内字段属于不同时点 | 拆为盘前/盘后两表 + unified 视图；探针合并读取（旧表自动回退，迁移期不断档） |
| 5 | 广度推力历史只留 10 天，无法算分位 | 保留 500 天 |
| 6 | `timedelta(days=20)` 自然日窗口，节假日导致样本偏短 | 统一 `trading_days_back()` 交易日行数 |
| 7 | `tqqq_drag_pct` 为 0.0 时被 `if global_slippage` 误判为缺失 | 显式 `is not None` |
| 8 | dashboard `self.log()` 方法不存在，微观评分异常时会二次崩溃 | 补上 |

## 三、三层异常矩阵（anomaly_engine.py）

- **第一层 single**：绝对阈值（如 VIX 倒挂、MOVE>120、NFCI>0）+ 252D z-score/分位。
- **第二层 multiwindow**：当日冲击 + 5 日同向延续 + 21 日分位进入危险区 → severity 升 3。
- **第三层 resonance**：跨资产共振主题：
  - `high_risk_selloff` 高危下跌（VIX结构 + MOVE + HYG/TLT + 广度 + 信用利差，≥3 维命中）
  - `fake_selloff_reversal` 假摔反转（TRIN 极端 + VRP 高 + 暗池吸筹 + 广度恐慌）
  - `crowding_fragility` 抱团脆弱（Mag7-RSP 分化 + 20MA 下滑 + IVR 麻木）

指标方向语义集中在 `METRIC_REGISTRY`（唯一真相源）：DPSV 高 = 机构吸筹（托底，
不计危险分），DPSV 低 = 散户乐观（风险）——全系统口径统一。

置信度 = 样本充分度 × 0.85^滞后交易日。z-score 类告警要求基准样本充分；
绝对阈值类样本惩罚下限 0.5（数值本身有含义）。

输出结构化落库 `anomaly_events`（report_date/metric/window/zscore/percentile/
severity/confidence/lag_days/layer/...），可直接回测调参。

## 四、职责边界变化

- `anomaly_engine` 负责**判定**（客观、可回测）；
- 三大探针降级为**背景快照**（供 AI 叙述盘面色彩）；
- `auto_analyst` 的 AI prompt 明确"只叙述归因，禁止推翻或加码矩阵 severity；
  低置信/高滞后异常必须明示局限；矩阵为空时不得编造风险"。

## 五、注意事项

- `ultimate_dashboard-本地.py` 是旧版本地备份，本次未动；如仍在使用请手动同步。
- 探针读新表为空时自动回退旧表 `stock_options_daily`，因此迁移当天旧数据仍可用；
  运行数日后新表积累起历史，个股级异常（DPSV/IVR 的 z-score）置信度会逐步上升。
- 所有 Supabase 拉取改为"倒序取最近 1000 行再翻转"，规避 PostgREST 默认 1000 行
  截断丢最新数据的问题。
- yfinance 仍是宏观行情事实源（VIX/MOVE/SKEW 等）。下一步建议：关键序列用 IB
  或官方源为主、yfinance 为 fallback（本次未改动，避免一次改动面过大）。
