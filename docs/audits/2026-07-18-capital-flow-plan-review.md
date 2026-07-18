# 美股资金行为监控系统 方案审查报告

- **审查ID**: capflow-plan-2026-07-18
- **模式**: plan-review（对 codex 提出的方案做方法论与可行性审查；未修改任何应用代码）
- **审查对象**: codex《美股资金行为、配置迁移与机械交易压力系统》方案（P0–P3 路线图 + 十二节设计）
- **对照现状**: `feature/capital-flow-monitor` @ `c6b1402`（切自 `feature/env-dashboard-wip`，为 main 的严格超集）
- **核对文件**: `HANDOFF.md` / `UPGRADE_README.md` / `migrations.sql` / `liquidity_monitor.py` / `data_contracts.py`
- **时间**: 2026-07-18，本地环境 macOS / Python 3.12

---

## 总体结论

方案的**方法论正确，且与代码库既有文化高度一致**（诚实观察层、拒绝伪造总额、双时态、按信息家族去重、影子模式）。其主要价值不在"新想法"，而在把团队已经在做的事**系统化、命名规范化**。

但有三个层面的问题必须在动工前解决，否则会退化为"漂亮路线图 + 半成品的轴"：

1. 方案假装从零开始，**忽略了已经建好的一半**（融资/信用层、Gamma 层）。
2. **P0 地基依赖一个尚未落实的数据源**，而这是整个系统的单点成败。
3. 在若干关键处**自相矛盾 / 过度精确**，需按团队已有教训收敛。

---

## 一、真正做对的地方（保留）

- **拒绝造"今天流入 X 美元"的伪总额**。全篇最重要的智识诚实，与上一轮 codex 审查砍掉 credit 双计权、把 DPSV 降为中性代理是同一原则。顶部日报"配置迁移轴只显示标准化状态、只有 ETF 美元额可求和"这条界线画得准。
- **ETF 一级市场估算算法修正是真 bug 修复**（见 CAPFLOW-002）。
- **命名纪律**（`ETF_LOOKTHROUGH_FLOW_ESTIMATE`、`UNRESOLVED_COMPLEX_FLOW`、OI 只作次日留仓确认）正确。
- **双时态 + 修订追踪、频率感知新鲜度、信息家族去重**，均为既定方向的延续。

---

## 二、发现（按严重度排序）

### CAPFLOW-001 · high · fix_before_start — 与现有系统重叠，存在造两套真相的风险
- **证据**:
  - 「调节层二：融资与信用」的 reserves / TGA / RRP / SOFR-IORB / NFCI / MOVE，`liquidity_monitor.py` 的六柱 Waterline 已全部建好（`PILLARS` + `ComponentSpec`，`liquidity_monitor.py:41-109`），落库于 `liquidity_daily`。方案却新开 `macro_liquidity_observation`。
  - 机械层的 GEX/Vanna/Charm/Gamma，`option_gamma_buckets` + REGISTRY 已有，且上一轮已按教训降为中性代理（`HANDOFF.md:49-50`）。
- **影响**: 同一 funding 状态出现两个口径 → 日报自相矛盾、维护双份基线、回测无法归因。
- **建议**: 方案应**消费** `liquidity_daily` / `option_gamma_buckets`，而非重建。新增事实表收敛为四张真正缺失的：ETF 一级流、CFTC、竞价不平衡、options signed-flow。

### CAPFLOW-002 · high · fix_now — 现有 ETF 流量算法正是方案点名不能用的算法（真 bug）
- **证据**: `data_contracts.py:44` 用 `delta_shares × float(current['Price'])`（区间份额变化 × 最后一天市场价）计算 `dollar_flow_m`；来源标记 `yfinance_info_unverified` / `CONTEXT_ONLY`（`data_contracts.py:53-54`）。
- **失败情形**: 区间内价格变动或一次 ETF 拆分，会让美元流量口径失真；用末日价对全区间份额变化计价系统性偏差。
- **建议**: 改为方案给的 `Σ 每日调整后份额变化 × 每日 NAV`，且份额变化须先过公司行动调整因子（见 CAPFLOW-005）。

### CAPFLOW-003 · high · fix_before_start — P0 地基的数据源未落实，是系统单点成败
- **证据**: P0 写"接入发行商份额、NAV 和公司行动"，但当前唯一 ETF 份额来源是 yfinance，已被自己标为不可靠（`CONTEXT_ONLY`，`data_contracts.py:53-54`；`UPGRADE_README` 明确"接入基金管理人级日度来源前不参与评分"）。
- **失败情形**: 基金级日度 shares outstanding + 每日 NAV + 公司行动，免费源难拿到可靠口径。数据源不定，"P0 真实配置层"建在沙子上，旗舰指标会因一次拆分报出巨额假流入。
- **建议**: 把"40 只 ETF 的日度份额/NAV/公司行动来源与预算"提为**第一位业务决定**（codex 把它藏在"数据预算"里）。未点名可信来源前不启动 P0 编码。

### CAPFLOW-004 · high · fix_before_start — 有效权重公式与其自身论述矛盾
- **证据**: §7 开头称"不建议简单把所有质量因素塞进一个乘法公式"，结尾却给出 `有效权重 = 基础×门控×可靠度×方向×新鲜度×覆盖率` 六因子乘积。
- **失败情形**: 五个 0.8 相乘 = 0.33，权重塌陷且不可解释；与现有 `_quality_weight`（`effective_obs_count + 0.85^lag`）构成第三种加权哲学。
- **建议**: 只保留 step 1 的**离散门控**（CONFIRMED/ESTIMATED/PROXY/CONTEXT_ONLY = 满权/折扣/仅进压力面板/零权），新鲜度也做成门控（错过下一应发布时点才降级），与现有质量加权统一，不引入连乘平滑衰减。

### CAPFLOW-005 · medium · fix_before_start — 公司行动 feed 是隐形炸弹
- **证据**: "调整后份额变化 = 当日份额 − 前日份额 × 公司行动调整因子" 依赖真实 CA 源；方案将其列为普通输入。
- **失败情形**: 无 CA 源时调整因子退化为 1，ETF 拆分当天旗舰指标直接爆假流入。
- **建议**: P0 把 `corporate_actions` 当硬依赖，不是可选项；与 CAPFLOW-003 数据源一并解决。

### CAPFLOW-006 · medium · redesign — 2×2 四象限有伪精确风险
- **证据**: 两个标准化轴符号噪声大、均值回复；硬标签在阈值附近反复跳变（"下跌吸收"↔"真实去风险"）。团队已吃过此亏，最终让环境指数进 `SHADOW_MODE`、`severity=0`、不进 `ALERT_GATE`（`HANDOFF.md:18-21,42-43`）。
- **建议**: 四象限仅做叙述，加滞后带（hysteresis）+ 置信区间，**绝不驱动任何方向告警**，与现有 sentinel 去重/闸门文化一致。

### CAPFLOW-007 · medium · add_caveat — CFTC 仓位 ≠ 现金股票配置
- **证据**: Asset Manager / Leveraged Funds 的期货仓位大量是对冲与基差交易。方案将其纳入"配置迁移轴"并赋予分量。
- **影响**: 它测的是"衍生品账户持仓"，非现金配置迁移；给高权重会误导主轴。
- **建议**: 单独成面板、权重预算低。codex 已提醒合约代码去重，但未提此定性偏差。

### CAPFLOW-008 · low · set_expectation — 验证门槛意味着长期不能出分
- **证据**: 方案自述 60–120 obs 才评分、6 个月影子、2–3 年才谈预测。
- **建议**: 对决策者诚实——**P0 落地后约 6–12 个月只能产出"数据 + 状态描述"，不是可信评分**。写进给决策者的第一页。

---

## 三、"复用 vs 新建" 对照表

| 方案模块 | 现状 | 结论 | 依据 |
|---|---|---|---|
| 调节层二 · 融资与信用（reserves/TGA/RRP/SOFR-IORB/NFCI/MOVE） | 六柱 Waterline 已建，落库 `liquidity_daily` | **复用**，消费 `liquidity_daily` | `liquidity_monitor.py:41-109` |
| 机械层 · GEX/Vanna/Charm/Gamma | `option_gamma_buckets` + REGISTRY 已有，已降中性代理 | **复用** | `HANDOFF.md:49-50`、`UPGRADE_README` |
| 双时态列 source_date/as_of_time/ingested_at | 已加于多表 | **复用** | `migrations.sql:9-19,44-47` |
| 质量加权 effective_obs_count + 0.85^lag | 已实现 | **复用**（统一到此，勿另起乘法） | `HANDOFF.md:63` |
| 影子模式 / ALERT_GATE / sentinel 去重 | 已实现 | **复用**（四象限套此闸门） | `HANDOFF.md:18-21,42-43` |
| published_at / available_at / revision_id / methodology_version | 仅有 source/as_of/ingested 三列 | **新建**（扩展双时态） | `migrations.sql`（缺 revision 列） |
| instrument_master / source_master / metric_dictionary / corporate_actions | 无 | **新建**（真正缺、有价值） | grep 无命中 |
| ETF 一级市场估算净发行（Σ 日调整份额 × 日 NAV + Flow/AUM/ADV/Z/折溢价/广度/集中度） | 仅 yfinance 份额、算法错误、CONTEXT_ONLY | **新建 + 换源**（替换 `data_contracts` 现算法） | `data_contracts.py:11-57` |
| CFTC 三类账户仓位 | 无 | **新建**（单独面板、低权重） | — |
| 竞价不平衡（15:50…final_cross + 未配对/价格冲击） | 无 | **新建** | — |
| Options signed-flow（Signed Delta/Vega/Gamma，OPRA/NBBO） | 无 | **暂缓**（依赖付费/许可数据） | 现为 IBKR/yfinance |
| Flow Surprise / 状态迁移概率 / 预测模型 | 无 | **研究层**，勿进生产（与 PCA 同置影子研究层） | — |

---

## 四、实施顺序收敛建议

方案 P0–P3 过满。按"只用已到手数据"重排：

- **做（数据在手 / 有半成品）**: 四张主数据表（`instrument_master` / `source_master` / `metric_dictionary` / `corporate_actions`，确实缺且有价值）+ ETF 一级流算法修正与换源 + CFTC 三类账户 + 竞价不平衡 + 量价家族去重。
- **暂缓（依赖付费 / 许可数据）**: P2 的 OPRA/NBBO signed delta/vega/gamma。当前是 IBKR/yfinance，拿不到 OPRA 逐笔 + 历史 NBBO。标为"条件具备再做"，不写进承诺路线图。
- **研究层，勿进生产**: P3 的 Flow Surprise / 状态迁移概率 / 预测模型，与 codex 自述"PCA 只在影子研究层"并列。

---

## 五、对"四个业务决定"的修订

codex 四个默认值基本同意，但**排序须改**：真正的 #1 不是"主要用途"，而是 **P0 的 ETF 日度份额/NAV/公司行动数据源与预算**（CAPFLOW-003 / 005）——此条不落实，后面皆空中楼阁。其余三条（盘后 1–5 日用途、核心 40 只、数据不足只报告不发方向）合理；尤其"数据不足 / 层级矛盾只报告不告警"与现有闸门文化完全对齐，保留。

---

## 附：动工前必须冻结的四项（本审查建议顺序）

1. **ETF 日度份额 / NAV / 公司行动数据源与预算**（阻断 P0）。
2. 有效权重改为离散门控 + 门控式新鲜度，统一到现有质量加权。
3. 融资/信用与 Gamma 层复用现有表，不新建平行口径。
4. 四象限只叙述、套 `SHADOW_MODE` + `ALERT_GATE`，不发方向告警。
