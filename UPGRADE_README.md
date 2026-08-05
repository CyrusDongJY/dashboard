# V9.2 升级说明 — 异常矩阵 + 环境指数 + 流动性水位仪

本次升级把系统从"探针文本战报"升级为"客观异常矩阵"：同一指标看 1D/5D/21D/63D
观察窗口，以 252 个交易日为统计基准，跨指标做共振判定，并按数据新鲜度加权置信度。

## 2026-08-05 质量闸门迁移

部署配套 Python 文件前，先在 Supabase SQL Editor 执行
`migration_20260805_quality_gates.sql`。迁移仅增加字段，可重复执行。

Expected Move 现在将原始估计与 `expected_move_decision_eligible` 分开。
冻结、陈旧、回退、事件日复核、盘前缺口大量消耗和 ATM 报价覆盖不足仍会
保留用于审计，但不会进入盘中阈值和环境指数评分。

事件日不能从价格异动反推。如需启用事件日闸门，应在云端私有
`market_config.py` 维护经复核的日期：

```python
EXPECTED_MOVE_EVENT_DATES = {
    "ALL": ["2026-09-16"],
    "AAPL": ["2026-10-29"],
}
EXPECTED_MOVE_EVENT_CALENDAR_COMPLETE = False
```

除非该日历对全部标的和日期均完整，否则保持
`EXPECTED_MOVE_EVENT_CALENDAR_COMPLETE = False`；未配置日期会标为
`UNKNOWN`，不会被静默视为普通交易日。

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

2026-07-22 交易判断与解释口径补强：

- 关键位距离统一为 `(level - spot) / spot`，正值表示关键位在现价上方；
  新增 `LEVEL_MINUS_SPOT_V2` 版本，异常引擎不拼接旧符号历史；
- Gamma增加请求、合约资格、OI、IV、Call/Put、执行价和覆盖率闸门；
  `LOW_COVERAGE` 只保留原始诊断，不发布正式Gamma、Flip或墙位；
- QQQ存在0DTE但无法计算时，明确区分无到期日、合约资格失败、OI缺失、
  IV缺失和低覆盖；
- U/D继续fail-closed，并记录UVOL/DVOL原值、来源与IBKR抓取失败诊断；
- DIX/GEX补源日期并统一降为context-only，异常引擎不再赋予确定方向；
- QQQ−QQQE、SPY−RSP、Mag7−RSP分列，缺数写NULL，不再用0制造差值；
- HYG/TLT同时输出HYG与TLT 21日收益，净流动性明确为63业务日ROC；
- COT输出报告日期、Leveraged Money类别、净仓/OI、156周Z值和样本数；
- 新增缺口接受率、动态累计VWAP时间/成交量接受率影子指标；
- 集中度贡献采用点时权重差乘收益的互斥分组算法；权重源未通过验收时
  返回 `POINT_IN_TIME_WEIGHTS_UNAVAILABLE`，不生成伪精确归因。

2026-07-24 盘中NYSE广度合约修正：

- 云端IBKR探针确认 `ADV-NYSE`、`DECL-NYSE`、`UVOL-NYSE`、`DVOL-NYSE`
  均返回错误码200（无证券定义），并非行情权限问题；
- 净上涨−下跌家数改用有效合约 `AD-NYSE`（conId 33887584），不再拼接
  两个不存在的合约；
- IBKR没有可用的UVOL/DVOL指数合约，U/D固定返回NULL并标记
  `UNSUPPORTED_BY_IBKR_CONTRACT`，相关信号继续fail-closed；
- IB错误码、合约conId和资格状态写入盘中上下文；合法的AD/TICK零值不再被
  误判为缺失；
- 历史伪中性 `U/D=1.00` 与无效 `ADD=0` 置NULL并标记
  `LEGACY_INVALID_IBKR_CONTRACT`。

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
   - `market_history` 新增盘后跨资产战术压力值、覆盖率、置信度、版本与分项 JSON
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
   | `tactical_stress.py` | 新增（盘后压力纯计算与 fail-closed 门控，**必须**放这里） |
   | `anomaly_engine.py` | 新增（**必须**放这里） |
   | `environment_indices.py` | 新增（影子指数，**必须**放这里） |
   | `environment_report.py` | 新增（盘后PNG，**必须**放这里） |
   | `liquidity_sources.py` | 新增（官方数据源适配，**必须**放这里） |
   | `liquidity_monitor.py` | 新增（六柱评分与状态机，**必须**放这里） |
   | `liquidity_report.py` | 新增（盘后水位仪PNG，**必须**放这里） |
   | `backfill_history.py` | 新增（一次性历史回补） |
   | `backfill_option_fragility.py` | 新增（IB/ThetaData期权脆弱度一次性回补） |
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
- 环境指数采用风险方向刻度：`0` 表示历史低压，`100` 表示历史高压，数值越高越危险；
  可选面板缺数时会列出具体指标及有效观测数，不再只显示笼统的“数据不足”。
- `calc_version=env_v2.2` 后，风险压强只使用显式的 `vix_ratio_contango_pct`
  （`1 - VIX/VIX3M`），不再把它与VX期货M2/M1升贴水拼入同一历史序列。
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

### V2.2口径与可靠性修正

- `calc_version=liquidity_v2.2`。VIX期货M2/M1升贴水与现货VIX/VIX3M比率分列保存、
  分别计算分位，禁止在同一历史序列中混用。数据库中的V1记录保留，可按版本追溯。
- 水位仪采用支持方向刻度：`0` 表示流动性支持最弱，`100` 表示支持最强，数值越高
  越充足。综合分只对达到门槛的柱加权，因此必须与有效覆盖率一起解读。
- 柱内“分量覆盖”和“样本成熟度”分开显示：是否发布柱分数由覆盖决定，成熟度继续
  参与柱内权重及综合有效覆盖，避免已有三个成熟广度分量却因二次折扣显示数据不足。
- 原始业务表仍为第一优先级；表内缺日时，水位仪用 `metric_daily` 的EOD宏观历史
  补齐广度、VVIX、SKEW及VIX曲线。旧字段 `vix_contango_pct` 只有来源明确为
  `macro_spot_daily` 或 `yfinance` 才分别映射为期货曲线或VIX/VIX3M曲线，未知来源
  保持缺失。
- 净流动性20日变化改用美元金额变化，不再对可能接近零的净值计算百分比。
- “融资管道承压”和“信用收缩”头条状态必须同时满足历史分位异常和绝对压力护栏；
  只有相对异常时降级为结构提示，避免平静样本内的正常波动触发强状态。
- 历史合并使用各数据表日期索引的并集，宏观表独有交易日不再被静默丢弃；TGA五日
  变化严格按工作日回看，不再按数据行位置近似。
- 官方HTTP源增加指数退避重试，财政部DTS接口支持分页；数据库写入失败会出现在
  邮件摘要和数据质量日志中。图表缺少核心数据时明确显示“数据不足”，不再把点画在
  中性位置。
- V2.2仍为研究性影子输出。部署后至少观察3至5个完整交易日，重点检查官方源覆盖率、
  融资/信用护栏触发率、缺日合并和PNG附件，再决定是否调整绝对阈值；不得直接接入
  告警或仓位决策。

### 一次性补齐市场分配、尾部韧性和期权脆弱度

先重新运行通用回补。新版会增加 `^SKEW`，并让水位仪复用已经存在的广度、VVIX和
VIX/VIX3M历史。脚本自动把结束日限制为18:00 ET后数据源已完成的最近NYSE交易日，
盘中执行也不会写入当天未完成行情：

```bash
cd /home/winters_dong426/market_dashboard
/usr/bin/python3 backfill_history.py 2y dry
/usr/bin/python3 backfill_history.py 2y
```

期权脚本默认 dry-run，只有显式 `--write` 才会写入。IBKR阶段回建滚动IV Rank；应在
TWS/IB Gateway已连接且有相应行情权限的环境运行：

```bash
/usr/bin/python3 backfill_option_fragility.py \
  --source ib --period 2y --audit-csv /tmp/option_fragility_ib.csv
/usr/bin/python3 backfill_option_fragility.py --source ib --period 2y --write
```

IB连接只打开历史行情socket，不同步账户或订单；单个请求30秒超时。若日志显示HMDS
农场断线，脚本会保留IVR为缺失并退出，待Gateway恢复后重跑，不用降低面板门槛。

ThetaData阶段按交易日读取EOD Greeks与当日上午可知的上一收盘OI，回建ATM预期波幅和
0-7D净Gamma。新版 `thetadata` Python库要求Python 3.12+；用已安装该库且配置了
`THETADATA_API_KEY`的解释器执行。当前云端使用隔离环境
`~/market_dashboard/.venv_option_backfill`，不修改Miniconda base：

```bash
.venv_option_backfill/bin/python backfill_option_fragility.py \
  --source theta --period 6mo --symbols SPY,QQQ \
  --audit-csv option_fragility_theta_spy_qqq_6mo.csv
.venv_option_backfill/bin/python backfill_option_fragility.py \
  --source theta --period 6mo --symbols SPY,QQQ --write
```

SPY与QQQ足以让预期波幅和短Gamma两个分量越过50%发布门槛；其余标的继续通过日常
采集积累，避免一次性回填产生数千个不必要的供应商请求。

两个阶段都不会覆盖 `is_final=true` 的EOD生产快照，也不新增cron。回补后检查独立
来源日期是否达到60个，再等待下一次 `auto_analyst.py` 正常盘后运行：

```sql
SELECT metric, scope, COUNT(DISTINCT source_date) AS native_obs,
       MIN(report_date) AS earliest_date, MAX(report_date) AS latest_date
FROM public.metric_daily
WHERE session = 'EOD'
  AND metric IN ('pct_20ma','pct_50ma','pct_200ma','pct_adv',
                 'breadth_diff_pct','vvix','skew','vix_ratio_contango_pct',
                 'vix_contango_pct',
                 'ivr_pct','expected_move_pct','short_gamma_m')
GROUP BY metric, scope
ORDER BY metric, scope;
```

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
