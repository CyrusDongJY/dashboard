# 市场流动性水位仪 第三方审查报告

- **审查ID**: liq-audit-2026-07-16
- **模式**: audit-only（依照 audit-risk-os 审查标准执行；未修改任何应用代码）
- **审查对象**: `feature/env-dashboard-wip` @ `08df042`（工作区脏：5 个已修改 + 4 个未跟踪文件，全部未提交）
- **范围**: liquidity_sources.py / liquidity_monitor.py / liquidity_report.py / auto_analyst.py（diff）/ ultimate_dashboard.py（diff）/ migrations.sql（diff）/ tests/test_liquidity_monitor.py
- **审查基准**: /tmp/market_liquidity_docx_render/市场流动性.pdf（方案讨论，16 页）
- **时间**: 2026-07-16（美东交易日内），本地环境 macOS / Python 3.12.13

---

## 一、发现（按严重度排序）

### LIQ-001 · high · fix_now — `vix_contango_pct` 混合两种不兼容口径进入同一打分序列
- **证据**: `liquidity_monitor.py:267-269` 用 `(1 - vix_term_ratio) × 100`（现货 VIX/VIX3M）派生该列；`daily_post_close.py:315-316` 向 `macro_spot_daily` 写入同名列但口径是期货 `(M2-M1)/M1 × 100`。`build_database_frame` 的 `combine_first`（`liquidity_monitor.py:344-350`）把两者拼进一列。
- **复现（已确认）**: market_history 有 1/8（ratio 0.88）、macro_spot 有 1/9（4.85）时，同一列相邻两天分别为 **12.0 和 4.85**。现货口径系统性偏大约 2-3 倍，来源切换日会人为制造百分位跳变。
- **影响**: 污染 `vix_curve` 分量（权重 1.20，尾部韧性柱内最高权重）的百分位基线。
- **验收标准**: 两种口径分列存储（如 `vix_contango_fut_pct` / `vix_ratio_contango_pct`），或只保留期货口径、派生口径单列另计；加一条防回归测试断言同列不混源。

### LIQ-002 · medium · fix_now — 融资/信用覆盖状态只有相对百分位，没有绝对护栏
- **证据**: `_classify`（`liquidity_monitor.py:496-499`）当 funding < 30 直接输出「融资管道承压」、credit < 25 输出「信用收缩」；而所有 funding 分量分数都是对自身约 252 个观测的百分位（`_score_component:460-464`）。
- **失败情形**: 整个基线年 SOFR-IORB 都钉在 −2~+2bp 时，一次 +3bp 的正常季末波动即可落到极端百分位，触发邮件头条级的「融资管道承压」；反之基线本身是压力年时，真实恶化会被稀释。方案文档（第 5-6 页）本来给了绝对参照（EFFR-IORB、SOFR 75th−SRF 等阈值），准备金也拿到了绝对档位（`_reserve_buffer_score`），唯独回购利差没有。
- **验收标准**: 覆盖状态在触发前叠加绝对条件（例如 SOFR-IORB ≥ +5bp 且 99th 分位利差扩大，或 SRF 用量 > 0），百分位仅用于常规计分。

### LIQ-003 · medium · backlog — macro 表数据在 market_history 缺日时被静默丢弃
- **证据+复现（已确认）**: `build_database_frame` 里 `frame[column] = extra[column]`（`liquidity_monitor.py:344-350`）按 frame 现有索引对齐。market_history 只有 1/8、macro_spot 只有 1/9 时，1/9 的 breadth/contango 整行丢失，frame 最后日期停在 1/8。
- **影响**: ultimate_dashboard 某天失败而 daily_post_close 成功时，当天广度/期限结构数据消失。失败方向保守（旧值→新鲜度衰减→质量下降），但违背合并意图。（注：market_history 完全为空时 pandas 会采用 extra 的索引，数据不丢——此路径已验证无问题。）
- **验收标准**: 合并前将 frame reindex 到两表日期并集（与 `merge_official_frame` 的做法一致），并补一条错位日期的测试。

### LIQ-004 · medium · observe_3_5_days — 数据库回退对 FRED 系列基本失效（历史行无原生日期）
- **证据**: `build_database_frame:301-320` 用 `full_metrics.fred_source_dates` 覆盖来源日期，缺失时**保持 NaT**（代码注释声明这是有意 fail-closed）。但 `fred_source_dates` 是本次 `ultimate_dashboard.py` 改动才开始写入的，部署前的全部历史行都是 NaT → `_score_component` 判 `insufficient observations`。
- **影响**: FRED API 中断当天，`auto_analyst.py` 注释宣称的「失败时回退现有数据库」对 fed_assets/nfci/利差等系列名不副实，基础水量柱会直接「数据不足」。方向安全（不会误报健康），但形成对 FRED 的单点依赖。
- **验收标准**: 观察期内确认实际覆盖率曲线；若需要真回退，为历史行回填 `fred_source_dates` 或以 `record_date` 加保守滞后惩罚作为降级来源日期。

### LIQ-005 · medium · backlog — 所有 HTTP 抓取无重试，DTS 无翻页
- **证据**: `liquidity_sources.py` 每个系列单次请求、20s 超时、失败仅 `logger.warning`（`build_frame:167-193`）；入库侧 `safe_upsert` 有 3 次退避重试，抓取侧没有。DTS `page[size]=5000` 无翻页循环（约 7 年内安全，超出会静默截断）。
- **影响**: 一次瞬时 5xx 就让当天该系列缺席，12 个串行请求最坏情况给 16:45 邮件链路追加约 4 分钟延迟。
- **验收标准**: 指数退避重试 2-3 次；DTS 按 `meta.total-pages` 翻页或断言未截断。

### LIQ-006 · medium · backlog — `net_liq_20d` 用 pct_change，量纲不稳健
- **证据**: `liquidity_monitor.py:66-68`，mode="pct_change"；同柱的 `fed_assets_20d` 却用 mode="change"。
- **影响**: 净流动性若趋近零轴（历史上 2019 回购危机前逼近过），百分比变化爆炸/变号；方案文档以「十亿美元变化量」表述边际流量。当前水平（约 5.9T）下无即时风险。
- **验收标准**: 改为 20 日差额（B），基线自动重算；或对分母加下限保护。

### LIQ-007 · low · backlog — 数据不足时象限图把「Current」画在 (50,50)
- **证据**: `liquidity_report.py:155-156`，score 为 None 时取 50。
- **影响**: 「数据不足」在图上呈现为「平衡水位」，与 fail-closed 原则相悖（文字区正确显示 --，但点具误导性）。
- **验收标准**: 缺数时不画点或标注 "insufficient data"。

### LIQ-008 · low · backlog — 持久化失败静默，邮件仍显示已计算状态
- **证据**: `safe_upsert` 最终失败返回 None 不抛出；`compute_liquidity_monitor:641-646` 不检查返回值，`liquidity_daily` 可能缺行而邮件正文照常。
- **验收标准**: 把 upsert 结果写进 `log_data_quality` 或在邮件摘要追加一行入库状态。

### LIQ-009 · low · backlog — TGA 五日预警用位置索引而非日期窗口
- **证据**: `liquidity_monitor.py:609-611`，`tga.iloc[-1] - tga.iloc[-6]` 在联合索引上是「倒数第 6 行」而非严格 5 个工作日。
- **验收标准**: 按日期切窗（`tga.loc[as_of - 5BD:]`）。

### LIQ-010 · low · backlog — 测试盲区
- 21 项测试全部通过（已复跑验证），但未覆盖：状态机覆盖阈值（<30/<25 触发与不触发的边界）、`merge_official_frame` 官方源优先级、NY Fed / DTS 解析器的录制夹具（本次靠实时 API 人工验证 schema）。LIQ-001/003 均属测试盲区放走的缺陷。

---

## 二、已通过项（附实际核验的证据）

| 项 | 证据 |
|---|---|
| 21/21 测试通过 | 本机复跑：py3.12.13 + pandas 2.2.3 + numpy 2.3.5 + matplotlib 3.11.0，`Ran 21 tests ... OK`（8.2s） |
| 静态检查 | `py_compile` 6 个文件全过；`git diff --check` 干净 |
| Fail-closed 缺数处理 | 覆盖率闸门 0.35（柱）/0.50（综合）；核心柱缺失 → 「数据不足」；有测试锚定（`test_missing_data_never_defaults_to_healthy`、`test_native_source_dates_prevent_filled_sample_inflation`） |
| 无前视 | `score_liquidity_frame` 截断至 report_date；历史曲线逐日重算（126 天重算实测 1.8s，性能无虞） |
| DIX/GEX/裸SOFR 不计分 | 测试强制（`test_raw_sofr_dix_and_gex_are_not_scoring_components`）；图表与邮件都有 context-only 免责语 |
| RRP=缓冲提示而非危机 | `warnings` 路径 + 测试锚定；与方案讨论结论一致 |
| 语义修正属实 | TOTRESNS→WRESBAL 且单位换算正确（百万→万亿）；「SOFR尾部利差」→「SOFR利率」；「OFR压力指数」→「Chicago Fed NFCI」；DIX 告警文案去除「机构吸筹」归因——均在 diff 中核实 |
| 官方 API 契约（实测 2026-07-16） | NY Fed `last/800.json` 返回 800 行，字段名逐一匹配（percentRate/percentPercentile75/99/volumeInBillions），SOFR 3.63 与文档一致；DTS 现行 schema `close_today_bal='null'`、收盘余额在 `open_today_bal`（787,814M ≈ $787.8B）——代码的回退分支是必要且正确的 |
| 单位换算 | WALCL(M→B)、WRESBAL(M→T)、RRPONTSYD(B)、DTS(M→B) 全部核对无误 |
| 迁移安全 | `liquidity_daily` 仅新增，IF NOT EXISTS，无破坏性语句；schema 与 `to_row()` 字段一一对应 |
| 影子边界 | 未接入 ALERT_GATE/仓位；邮件附件缺失时降级不假成功 |

## 三、证据限制

- 全部改动**未提交**（分支脏），审查对象以工作区快照为准。
- 未在云端/生产 Supabase 实跑；FRED 端点未实测（需 API key）；PNG 视觉仅凭 /tmp 预览图旁证。
- 观察期未开始：覆盖率的真实日常波动、FRED/DTS/NY Fed 的间歇性行为、邮件送达没有交易日证据。

## 四、建议与部署闸门（按顺序）

1. **提交前**: 修复 LIQ-001（口径分列），补一条混源防回归测试。LIQ-002 若不改逻辑，至少把覆盖状态文案降级为「观察提示」直到有绝对护栏。
2. **部署时**: 按 UPGRADE_README 执行迁移 + `matplotlib>=3.9`；用手动触发验证一次完整链路（官方源抓取→评分→PNG→邮件→liquidity_daily 落行）。
3. **观察 3-5 个交易日**: 盯覆盖率曲线（尤其 FRED 中断日，验证 LIQ-004）、状态机是否出现百分位驱动的假「承压」（LIQ-002）、macro/market_history 缺日行为（LIQ-003）、邮件附件与入库一致性（LIQ-008）。
4. **观察期后**: LIQ-005/006/007/009/010 按 backlog 消化；用收集的真实分布决定绝对护栏阈值。

## 五、结论

- **影子观察运行**: **允许**（修复 LIQ-001 后开始计观察期；系统本身 fail-closed 方向正确，无任何路径触发告警或仓位）。
- **接入告警/决策**: **禁止**，直至观察期完成 + LIQ-001/002 关闭 + 状态标签经至少一次真实压力事件或历史回放校验。
- Codex 汇报的「21 项测试、静态编译、diff --check 通过」**全部属实**（已独立复跑）；「修正 SOFR/NFCI/DIX 语义」**属实**；「保留真实观测日期、避免前向填充虚增样本」**属实且有测试**；「失败时回退现有数据库」**部分属实**（对 FRED 系列的历史行实际不可回退，见 LIQ-004）。
