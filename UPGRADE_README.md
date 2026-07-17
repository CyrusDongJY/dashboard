# V9.2 升级说明 — 异常矩阵 + 环境指数 + 流动性水位仪

本次升级把系统从"探针文本战报"升级为"客观异常矩阵"：同一指标看 1D/5D/21D/63D
观察窗口，以 252 个交易日为统计基准，跨指标做共振判定，并按数据新鲜度加权置信度。

盘前期权模块同时升级为明确的数据契约：

- 昨收、盘前成交、Bid/Ask/Mid、期货映射参考价分别存储，禁止混称"当前现价"；
- Expected Move 优先ATM跨式双边中间价，回退ATM IV公式，失败写NULL并登记质量告警；
- Gamma按0DTE、1—7日、8—30日、31—60日和采样期限合计（默认≤60日）分别计算；
  为满足IBKR行情订阅节流，每个桶选择代表性到期日和现价附近31档，
  报告与数据库会保存真实采样到期日/合约数，不把采样结果冒充完整全链；
- Gamma Flip来自现货情景网格的真实零交叉，不再用单执行价差额最小点冒充；
- 最大OI附到期日、OI、Delta、美元Gamma和距参考价百分比；
- 同一Call/Put墙改为Pin候选位或负Gamma突破枢轴；
- OI PCR统一命名为"Put/Call持仓结构比"，不再解释为真实多空。

2026-07-17 数据契约补强：

- 盘中 POC 改从 `stock_spot_post_close` 读取，盘前表缺字段不再显示为 0；
- U/D、TRIN、PCR、Gamma 缺失或过期时写 NULL/NA 与质量状态，不再填 1.00/0；
- 盘中 Gamma 坐标输出 ticker、截面、期限、曲线版本、现价参考和零点数；
- TRIN 分离 `NYSE TRIN` 与 `前500大市值样本TRIN`；
- `净新高-新低` 不再命名为黑天鹅预警；
- IV Rank 与 IV Percentile 分列存储；
- ETF 份额缓存开始按日推进并输出 1/5/20 日变化，但 yfinance 份额仍标为
  `CONTEXT_ONLY`，在接入基金管理人级日度来源前不参与 Waterline 评分；
- Waterline 数据不足时显示各组件有效观测数/最低要求，不再只显示覆盖率 0。

## 一、部署步骤（按顺序）

1. **执行数据库迁移**：在 Supabase SQL Editor 中运行 [migrations.sql](migrations.sql)。
   全部语句幂等（IF NOT EXISTS），可重复执行。新增：
   - `stock_options_pre_market` / `stock_spot_post_close`（拆表）+ `stock_options_unified` 视图
   - `data_quality`（抓取质量审计）
   - `anomaly_events`（结构化异常矩阵，可回测）
   - `sentinel_runs`（哨兵每次评估的指纹与是否发警，用于去重防轰炸 + 回测）
   - `metric_daily`（按 PRE/INTRADAY/EOD 分会话的每日全指标快照）
   - `environment_daily`（环境指数影子台账，不参与正式预警）
   - `liquidity_daily`（六柱流动性水位、覆盖率、状态和可解释明细）
   - `option_gamma_buckets`（盘前Gamma分期限净值、全部零点与实际采样到期日）
   - 现有表补 `source_date` / `as_of_time` / `ingested_at` 三列

2. **部署代码（按目录放对，否则会 ImportError）**：新增的共享模块
   `market_utils.py` / `anomaly_engine.py` 靠各脚本开头的
   `sys.path.append('~/market_dashboard')` 被 import，因此 **`~/market_dashboard/`
   是全系统的共享模块中枢**。文件按运行它的 cron 所在目录分发：

   **📁 `~/market_dashboard/`（共享模块 + 本目录脚本，全部放这里）**
   | 文件 | 动作 |
   |---|---|
   | `market_utils.py` | 新增（**必须**放这里） |
   | `data_contracts.py` | 新增（纯数据契约与 fail-closed 计算，**必须**放这里） |
   | `pre_market_metrics.py` | 新增（盘前纯计算引擎，**必须**放这里） |
   | `anomaly_engine.py` | 新增（**必须**放这里） |
   | `environment_indices.py` | 新增（影子指数，**必须**放这里） |
   | `environment_report.py` | 新增（盘后PNG，**必须**放这里） |
   | `liquidity_sources.py` | 新增（官方数据源适配，**必须**放这里） |
   | `liquidity_monitor.py` | 新增（六柱评分与状态机，**必须**放这里） |
   | `liquidity_report.py` | 新增（盘后水位仪PNG，**必须**放这里） |
   | `backfill_history.py` | 新增（一次性历史回补） |
   | `requirements-env-dashboard.txt` | 新增（PNG依赖清单） |
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
   | `ib_intraday_sniper.py` | 覆盖 —— 读取明确盘前/盘后字段；依赖共享的 `data_contracts.py` |

   新增哨兵 `market_sentinel.py` 放 **`~/market_dashboard/`**（它 import
   `anomaly_engine` / `market_utils`，与共享模块同目录最省事）。

   ⚠️ **最易犯的错**：把 `market_utils.py` / `anomaly_engine.py` 复制到了 `quant_bot`
   或 `TradingRadar`。那样 `auto_analyst` 可能侥幸能跑（同目录），但
   `daily_pre_market` / `daily_post_close` 会 `ModuleNotFoundError`。
   `pre_market_metrics.py` 与 `data_contracts.py` 同样只保留在
   `~/market_dashboard/`。

3. **cron 调整**：时间/路径/参数基本不变，只建议一处——
   把 `auto_analyst` 从 `16:35` 挪到 `16:45`。原因：升级后 `auto_review`（16:30）
   拉 2 年数据变慢，若它没在 16:35 前写完 `market_history`，异常引擎会拿昨天的
   基准行配今天的现货数据（时点错配）。留 15 分钟余量更稳：
   ```cron
   45 16 * * 1-5 /usr/bin/python3 /home/winters_dong426/quant_bot/auto_analyst.py >> /home/winters_dong426/quant_bot/run.log 2>&1
   ```
   PNG 报告新增 `matplotlib` 依赖。在运行 `auto_analyst.py` 的全局环境安装：
   ```bash
   /usr/bin/python3 -m pip install -r /home/winters_dong426/market_dashboard/requirements-env-dashboard.txt
   ```
   其他依赖仍为 pandas / numpy / pandas_market_calendars / supabase / yfinance / requests。

   **新增两行哨兵 cron**（全局审查 + 主动预警，与抓取/日报彻底解耦）：
   ```cron
   # 🛰️ 全局哨兵·早盘：盘前数据(08:45)落库后刷新结构快照并审查已验证指标
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
   PYTHONPATH=/home/winters_dong426/market_dashboard \
       /home/winters_dong426/trading_venv/bin/python3 \
       /home/winters_dong426/market_dashboard/tests/test_pre_market_metrics.py -v

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
  - `fake_selloff_reversal` 反转代理观察（TRIN 极端 + VRP 高 + 广度恐慌）
  - `crowding_fragility` 抱团脆弱（Mag7-RSP 分化 + 20MA 下滑 + IVR 麻木）

指标方向语义集中在 `METRIC_REGISTRY`。DPSV 仅是 FINRA 场外短售成交量代理，
不等同于空头持仓或机构吸筹；相关方向假设只用于影子观察，必须通过回测验证。

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
- **盘前 08:55**：刷新 GEX/DPSV/Call Wall 曲线，并审查方向已验证的指标，
  不用干等到收盘。
- **盘后 16:55**：`auto_review`（16:30）写完 `market_history` 后做全量跨源审查。

## 六、注意事项

- `ultimate_dashboard-本地.py` 是旧版本地备份，本次未动；如仍在使用请手动同步。
- 探针读新表为空时自动回退旧表 `stock_options_daily`，因此迁移当天旧数据仍可用；
  运行数日后新表积累起历史；DPSV只积累中性代理曲线，IVR等已定义方向的指标
  才参与异常判断。
- 所有 Supabase 拉取改为"倒序取最近 1000 行再翻转"，规避 PostgREST 默认 1000 行
  截断丢最新数据的问题。
- yfinance 仍是宏观行情事实源（VIX/MOVE/SKEW 等）。下一步建议：关键序列用 IB
  或官方源为主、yfinance 为 fallback（本次未改动，避免一次改动面过大）。

## 七、环境指数影子模式

- `auto_analyst.py` 盘后写入 `session=EOD, is_final=true`；哨兵按运行时间写入
  `session=PRE/POST`，三个截面不再互相覆盖。
- 指数按有效独立观测数和数据滞后加权。核心覆盖不足时状态固定为“数据不足”，
  不会默认显示健康。
- 五类指数与综合值全部是研究性影子输出：只进入邮件附录、PNG和数据库，明确不接入
  `ALERT_GATE`、不改变 severity、不用于仓位。
- `backfill_history.py` 对低频FRED序列使用原生观测计算统计量，再映射到NYSE交易日；
  填充日保留真实 `source_date` 且不增加 `effective_obs_count`。
- 回补前必须先执行最新版 `migrations.sql`。正式执行前建议先运行：
  ```bash
  /usr/bin/python3 /home/winters_dong426/market_dashboard/backfill_history.py 2y dry
  ```
  确认覆盖范围后去掉 `dry`。脚本不会覆盖 `is_final=true` 的EOD生产快照。
- 默认回补FRED、yfinance以及现有DIX.csv中的DIX/GEX历史。FINRA个股DPSV需要逐日请求
  Consolidated NMS文件，为避免默认产生数百次请求，使用显式参数：
  ```bash
  /usr/bin/python3 /home/winters_dong426/market_dashboard/backfill_history.py 2y finra dry
  ```
  先观察请求成功率和行数，再去掉 `dry`。
- 环境指数至少影子运行一个完整季度，再以未来5D/21D回撤、波动率和误报率做走步回测；
  未完成校准前，不得把状态标签接入正式预警。


## 八、市场流动性水位仪（V9.2影子模式）

- 新增 liquidity_sources.py：从官方源读取日度TGA、H.4.1准备金、FRED宏观序列，
  以及纽约联储SOFR/TGCR/EFFR分位与成交量。所有序列保留真实观测日期。
- 新增 liquidity_monitor.py：分为基础水量、边际水流、融资管道、信用传导、
  市场分配和尾部韧性六维。分值越高表示流动性支持越强，同时输出质量覆盖率。
- 新增 liquidity_report.py：盘后生成PNG，显示六维水位、流量四象限、历史轨迹、
  主要支撑/拖累与结构提示，并作为第二张附件挂入 auto_analyst.py。
- SOFR原值只标为“SOFR利率”，不再冒充尾部利差；NFCI明确标为Chicago Fed NFCI，
  不再与OFR FSI混称。DIX、FINRA短售量和GEX仅作上下文，不直接改变水位分。
- 执行新版 migrations.sql 创建 liquidity_daily 后，再部署三个新增模块和
  auto_analyst.py。第一版保持 shadow_mode=true，不接入 ALERT_GATE 或仓位。

### V2口径与可靠性修正

- `calc_version=liquidity_v2`。VIX期货M2/M1升贴水与现货VIX/VIX3M比率分列保存、
  分别计算分位，禁止在同一历史序列中混用。数据库中的V1记录保留，可按版本追溯。
- 净流动性20日变化改用美元金额变化，不再对可能接近零的净值计算百分比。
- “融资管道承压”和“信用收缩”头条状态必须同时满足历史分位异常和绝对压力护栏；
  只有相对异常时降级为结构提示，避免平静样本内的正常波动触发强状态。
- 历史合并使用各数据表日期索引的并集，宏观表独有交易日不再被静默丢弃；TGA五日
  变化严格按工作日回看，不再按数据行位置近似。
- 官方HTTP源增加指数退避重试，财政部DTS接口支持分页；数据库写入失败会出现在
  邮件摘要和数据质量日志中。图表缺少核心数据时明确显示“数据不足”，不再把点画在
  中性位置。
- V2仍为研究性影子输出。部署后至少观察3至5个完整交易日，重点检查官方源覆盖率、
  融资/信用护栏触发率、缺日合并和PNG附件，再决定是否调整绝对阈值；不得直接接入
  告警或仓位决策。

### 云端验证

水位仪复用现有 `16:45 auto_analyst.py`，不新增 cron。部署后先确认依赖和导入：

```bash
/usr/bin/python3 -m pip install -r /home/winters_dong426/market_dashboard/requirements-env-dashboard.txt
cd /home/winters_dong426/market_dashboard
/usr/bin/python3 -m py_compile liquidity_sources.py liquidity_monitor.py liquidity_report.py
```

下一交易日运行后，在 Supabase SQL Editor 检查：

```sql
SELECT report_date, composite, coverage, state, shadow_mode, calc_version
FROM public.liquidity_daily
ORDER BY report_date DESC
LIMIT 5;
```

邮件应包含环境图和 `liquidity_waterline_YYYY-MM-DD.png` 两个 PNG 附件；日志应出现
`流动性水位仪生成完成`。没有异常也会生成水位仪，因为它是每日影子报告，不受
`market_sentinel.py` 的告警门槛控制。
