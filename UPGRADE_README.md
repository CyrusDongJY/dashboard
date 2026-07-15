# V9.0 升级说明 — 盘后多窗口异常汇总系统

本次升级把系统从"探针文本战报"升级为"客观异常矩阵"：同一指标看 1D/5D/21D/63D
观察窗口，以 252 个交易日为统计基准，跨指标做共振判定，并按数据新鲜度加权置信度。

## 一、部署步骤（按顺序）

1. **执行数据库迁移**：在 Supabase SQL Editor 中运行 [migrations.sql](migrations.sql)。
   全部语句幂等（IF NOT EXISTS），可重复执行。新增：
   - `stock_options_pre_market` / `stock_spot_post_close`（拆表）+ `stock_options_unified` 视图
   - `data_quality`（抓取质量审计）
   - `anomaly_events`（结构化异常矩阵，可回测）
   - `sentinel_runs`（哨兵每次评估的指纹与是否发警，用于去重防轰炸 + 回测）
   - 现有表补 `source_date` / `as_of_time` / `ingested_at` 三列

2. **部署代码（按目录放对，否则会 ImportError）**：新增的共享模块
   `market_utils.py` / `anomaly_engine.py` 靠各脚本开头的
   `sys.path.append('~/market_dashboard')` 被 import，因此 **`~/market_dashboard/`
   是全系统的共享模块中枢**。文件按运行它的 cron 所在目录分发：

   **📁 `~/market_dashboard/`（共享模块 + 本目录脚本，全部放这里）**
   | 文件 | 动作 |
   |---|---|
   | `market_utils.py` | 新增（**必须**放这里） |
   | `anomaly_engine.py` | 新增（**必须**放这里） |
   | `market_probes.py` | 覆盖 |
   | `ultimate_dashboard.py` | 覆盖 |
   | `auto_review.py` | 覆盖 |
   | `daily_pre_market.py` | 覆盖 |
   | `daily_post_close.py` | 覆盖 |
   | `market_config.py` | 不动（原有密钥文件） |

   **📁 `~/quant_bot/`**
   | 文件 | 动作 |
   |---|---|
   | `auto_analyst.py` | 覆盖 —— 从 `~/market_dashboard` 拉共享模块，**不要**把模块复制到这里 |

   **📁 `~/TradingRadar/`**
   | 文件 | 动作 |
   |---|---|
   | `ib_intraday_sniper.py` | 覆盖 —— 仅改查询表名，无新依赖 |

   新增哨兵 `market_sentinel.py` 放 **`~/market_dashboard/`**（它 import
   `anomaly_engine` / `market_utils`，与共享模块同目录最省事）。

   ⚠️ **最易犯的错**：把 `market_utils.py` / `anomaly_engine.py` 复制到了 `quant_bot`
   或 `TradingRadar`。那样 `auto_analyst` 可能侥幸能跑（同目录），但
   `daily_pre_market` / `daily_post_close` 会 `ModuleNotFoundError`。
   **这两个新模块只保留 `~/market_dashboard/` 一份。**

3. **cron 调整**：时间/路径/参数基本不变，只建议一处——
   把 `auto_analyst` 从 `16:35` 挪到 `16:45`。原因：升级后 `auto_review`（16:30）
   拉 2 年数据变慢，若它没在 16:35 前写完 `market_history`，异常引擎会拿昨天的
   基准行配今天的现货数据（时点错配）。留 15 分钟余量更稳：
   ```cron
   45 16 * * 1-5 /usr/bin/python3 /home/winters_dong426/quant_bot/auto_analyst.py >> /home/winters_dong426/quant_bot/run.log 2>&1
   ```
   其余 cron 行保持原样。**无需新装 pip 包**：三处运行环境（trading_venv 与全局
   `/usr/bin/python3`）原本就具备 pandas / numpy / pandas_market_calendars /
   supabase / yfinance / requests，`anomaly_engine` 未引入任何新外部库。

   **新增两行哨兵 cron**（全局审查 + 主动预警，与抓取/日报彻底解耦）：
   ```cron
   # 🛰️ 全局哨兵·早盘：盘前数据(08:45)落库后审查，抓早间 GEX/DPSV/Call Wall 异常
   55 8 * * 1-5 /usr/bin/python3 /home/winters_dong426/market_dashboard/market_sentinel.py >> /home/winters_dong426/market_dashboard/sentinel.log 2>&1
   # 🛰️ 全局哨兵·盘后：auto_review(16:30) 写完 market_history 后做跨源全局审查
   55 16 * * 1-5 /usr/bin/python3 /home/winters_dong426/market_dashboard/market_sentinel.py >> /home/winters_dong426/market_dashboard/sentinel.log 2>&1
   ```
   哨兵有交易日闸门（非交易日自动跳过）和当日去重（同一组触发指标只发一次），
   多跑一次不会重复轰炸。只在异常越过红线时才发邮件，平静日只入库、不打扰。

4. **部署后立即手动验证一次**（用 `now` 参数绕过时间闸门，不必等收盘）：
   ```bash
   # ① 先在 Supabase SQL Editor 执行 migrations.sql

   # ② 盘前脚本（trading_venv）
   /home/winters_dong426/trading_venv/bin/python3 \
       /home/winters_dong426/market_dashboard/daily_pre_market.py now

   # ③ 盘后脚本（全局环境）
   /usr/bin/python3 /home/winters_dong426/market_dashboard/daily_post_close.py now

   # ④ 复盘链路（最能暴露跨目录 import 问题）
   /usr/bin/python3 /home/winters_dong426/market_dashboard/auto_review.py
   /usr/bin/python3 /home/winters_dong426/quant_bot/auto_analyst.py

   # ⑤ 全局哨兵（dry=只评估打印，不发邮件、不写库）
   /usr/bin/python3 /home/winters_dong426/market_dashboard/market_sentinel.py dry
   ```
   `dry` 模式会打印完整异常矩阵和"若触发将发送的邮件正文"，但不发信、不写库，
   最适合部署当天确认红线行为是否符合预期。

   若第 ④/⑤ 步报 `ModuleNotFoundError: No module named 'market_utils'`（或
   `'anomaly_engine'`），说明这两个新文件没放进 `~/market_dashboard/`——这是
   唯一需要盯紧的点。

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

## 五、全局哨兵与预警红线（market_sentinel.py）

哨兵与抓取、日报**彻底解耦**：抓取程序只管写库，哨兵在数据落库后跨所有表跑
`anomaly_engine`，**只在异常越过红线 `ALERT_GATE` 时主动发预警邮件**，其余仅入库备查。

**唯一的"发不发邮件"判定集中在 `market_sentinel.py` 顶部的 `ALERT_GATE`**（当前=稳健档）：

| 触发规则 | 稳健档设定 | 含义 |
|---|---|---|
| 单指标极端 | `solo_severity=3`, 置信≥0.50 | 只有 sev3（VIX倒挂/z>3 等）单独触发 |
| 中度异常聚集 | `cluster_severity=2`, `cluster_count=2`, 置信≥0.50 | ≥2 条 sev≥2 同现才报（防单点误报） |
| 跨资产共振 | `resonance_always=True` | 命中任一共振主题必报（价值最高） |
| 多窗口确认 | `multiwindow_always=True` | 当日+5日+21日三窗口确认必报 |
| 硬置信度地板 | `hard_confidence_floor=0.35` | 低于此的异常永不触发邮件，只入库 |

**调灵敏度不改引擎，只改这一处配置**：
- 觉得太吵 → 调高 `hard_confidence_floor`、或把 `cluster_count` 提到 3；
- 想更敏感（激进档）→ `solo_severity=2`；
- **回测校准**：`anomaly_events` 表存了每天**所有**异常（含未触发预警的），
  可统计"若把某项调成 X，过去 N 天会发多少封"，用数据定阈值而非拍脑袋。

**防轰炸**：同一交易日、同一组触发指标只发一次（指纹记录在 `sentinel_runs`），
早晚两次运行不会重复骚扰。

**降级不静默**：引擎抛错或快照全空（上游没写库）时，哨兵发降级提示邮件，
绝不"以为今天没异常"。

运行节奏（cron 已配）：
- **盘前 08:55**：抓 GEX/DPSV/Call Wall 后立即审查，morning 异常当天早上就报，
  不用干等到收盘。
- **盘后 16:55**：`auto_review`（16:30）写完 `market_history` 后做全量跨源审查。

## 六、注意事项

- `ultimate_dashboard-本地.py` 是旧版本地备份，本次未动；如仍在使用请手动同步。
- 探针读新表为空时自动回退旧表 `stock_options_daily`，因此迁移当天旧数据仍可用；
  运行数日后新表积累起历史，个股级异常（DPSV/IVR 的 z-score）置信度会逐步上升。
- 所有 Supabase 拉取改为"倒序取最近 1000 行再翻转"，规避 PostgREST 默认 1000 行
  截断丢最新数据的问题。
- yfinance 仍是宏观行情事实源（VIX/MOVE/SKEW 等）。下一步建议：关键序列用 IB
  或官方源为主、yfinance 为 fallback（本次未改动，避免一次改动面过大）。
