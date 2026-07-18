# ETF 数据源与预算选型表（P0A 决策）

- **决策ID**: capflow-etf-source-2026-07-18
- **状态**: DRAFT — 待 POC 验证与预算拍板
- **定位**: P0A 第一优先级决策。此表决定"美股资金行为监控系统"能否从研究概念进入生产。
- **前置**: [2026-07-18-capital-flow-plan-review.md](../audits/2026-07-18-capital-flow-plan-review.md)（CAPFLOW-003 / 005：ETF 权威数据源是第一阻断条件）
- **分支**: `feature/capital-flow-monitor`
- **核查时间**: 2026-07-18（供应商能力/授权会变动，价格档位为指示性，务必 POC 期间询价复核）

---

## 一、P0 真正需要的字段（难点在最后四项）

事实层一「基金配置流」的旗舰指标 `etf_primary_flow_daily` 要求：

| # | 字段 | 难度 | 说明 |
|---|---|---|---|
| 1 | 日度基金份额（shares outstanding） | ★★★ | 计算净发行的核心；多数便宜源只有季度报表口径 |
| 2 | 同日 NAV | ★★ | 必须与份额同日对齐，否则口径错位 |
| 3 | AUM | ★ | 用于 Flow/AUM bps |
| 4 | **公司行动（拆分/反向拆分/合并/清盘/ticker 变更）** | ★★★ | 无此源则拆分日爆假申购（CAPFLOW-005） |
| 5 | **点时 / 修订历史（point-in-time）** | ★★★ | 决定能否提前回测（你的修订 #2） |
| 6 | **历史深度 3–5 年** | ★★★ | 同上 |
| 7 | **授权允许自动抓取 + 存储 + 派生 + 内部分发** | ★★★ | 发行商直连的真正风险点 |

> 关键判断：**便宜的 API 档在 #1/#4/#5 上普遍不合格**（份额多为季度报表口径、公司行动不带、无点时）。真正能过 P0 的只有"发行商直连"和"专用参考/点时数据商"两条路。

---

## 二、候选源矩阵

评级：✅ 满足 ／ ⚠️ 部分/需确认 ／ ❌ 不满足 ／ 💲 指示性成本（务必询价）

### A 档 · 发行商直连（免费，权威，但授权与工程成本在你这边）

| 源 | 日度份额 | 同日NAV | 公司行动 | 点时/修订 | 历史深度 | 自动抓取授权 | 成本 |
|---|---|---|---|---|---|---|---|
| SSGA/SPDR（daily holdings xlsx） | ✅ | ✅ | ❌ 需自建 | ❌ 只能自采向前攒 | ❌ 仅当前 | ⚠️ 个人/非商用，二次分发/派生需另签，且含指数商 IP（S&P/MSCI/FTSE/CRSP） | 免费 |
| iShares/BlackRock | ✅ | ✅ | ❌ | ❌ | ❌ | ⚠️ 同上；**非交易日不出持仓**（cadence 差异） | 免费 |
| Vanguard | ✅ | ✅ | ❌ | ❌ | ❌ | ⚠️ 同上；**用日历月末日期**（与 iShares 口径不一致） | 免费 |
| Invesco | ✅ | ✅ | ❌ | ❌ | ❌ | ⚠️ 同上 | 免费 |

- 社区库 `etf-scraper` 覆盖以上四家，但作者明确免责"不保证符合各基金 T&C"——**授权是灰区，不能默认可商用/可派生**。
- **定位**：最佳的**地面真值校验**源 + **向前自建点时归档**的起点；**不能**当历史回补源，**不能**当干净公司行动源。选它 = 走"路径 B"（见第四节）。

### B 档 · 专用参考 / 点时数据商（付费，直击难点字段）

| 源 | 命中的难点 | 说明 | 成本 |
|---|---|---|---|
| **EDI（Exchange Data International）** | #1 #4 | Official + **Daily-Adjusted Shares Outstanding** 双数据集，专为"官方更新发布前公司行动导致份额突变"设计（含 bonus/buyback/consolidation/rights）。**最直接命中"CA 调整后份额"**。 | 💲询价（中档） |
| **DTCC ETF Portfolio Data** | #4 #7 | ETF 原生的 announcement & security reference，美国 ETF 最权威。 | 💲询价（企业） |
| **Databento** | #4 #5 #6 | 点时 instrument definition、公司行动按"如同当时"打时间戳、价格回溯调整、无幸存者/前视偏差；310k+ 工具。**点时回测最强**。 | 💲较可及（有 $125 试用额度） |
| **QUODD** | #4 #5 | 跨 identifier 变更自动缝合、调整因子、任意时点任意 identifier、无幸存者偏差。 | 💲询价 |

### C 档 · 企业级基金流平台（贵，含成品 flow，但授权不透明）

| 源 | flow 频率 | 定位 | 指示性价格 |
|---|---|---|---|
| **Bloomberg Data License** | 亚秒/实时推送 | 频率最高、跨资产最广；redistribution 权限单独谈 | 💲议价，按数据类目/量/分发权，无公开价 |
| **FactSet** | 机构实时 flow alert | 建模集成强 | 💲~$12–20k/席/年（席位价；企业馈送另议） |
| **Morningstar Direct** | 日频 + 深历史 | 基金流专家，日频足够时性价比最好 | 💲~$10k+/席/年 |
| **LSEG/Refinitiv（Lipper）** | EOD 净流入/出 | 多数美国 ETF 的 EOD flow | 💲询价 |

> 注意：C 档给的是**成品 flow**，会绕过你自建"Σ 日调整份额 × 日 NAV"的算法透明度；且席位价 ≠ 可编程摄入的企业馈送价（后者三家都要销售报价）。若买 C 档，需确认**授权允许把 flow 派生进你自己的评分并内部分发**。

### D 档 · 便宜 API（P0 不合格，仅可做 AUM/价格 context）

| 源 | 份额口径 | 判定 |
|---|---|---|
| EODHD | 季度报表 + 点时指数成分 | ❌ 非日度份额 |
| Barchart OnDemand `getETFDetails` | 份额+NAV 同对象但**快照** | ⚠️ 需自己每日轮询存档才成序列 |
| Finnworlds | NAV 日更（SEC+基金源） | ⚠️ 值得单独问是否有日度份额历史序列 |
| Twelve Data / Alpha Vantage / FMP / Intrinio / Polygon | 多为报表口径份额 | ❌ 日度净发行不可用 |
| ICI 周度 | 行业总量、估算、可修订 | ✅ 仅作行业总量校验（方案已定位） |

---

## 三、结论：难点字段把选择压到两条路

- **份额 + NAV + AUM** → A 档发行商直连即可拿到（免费）。
- **公司行动 + 点时/修订历史 + 3–5 年深度 + 派生授权** → **只有 B/C 档能给**。

因此 P0A 的真问题不是"选哪个 API"，而是**"要不要买点时历史"**——这正好对应你修订 #2 的分叉。

---

## 四、两条采购路径（对应你的修订 #2）

### 路径 A：买点时历史（B 档为主）
```
EDI 日调整份额  +  Databento 点时/公司行动  +  NAV（发行商或 EDI）
```
- **优点**：可回放历史 → **提前回测**，不必干等 6–12 个月；公司行动、点时、修订开箱即用。
- **代价**：真金白银（询价，估中五位数/年级别）+ 授权谈判周期。
- **仍需**：≥3 个月真实在线影子运行，验证**你自己的**抓取链路与修订行为（你的修订 #2 结论）。

### 路径 B：发行商直连 + 向前自建点时（A 档为主）
```
SSGA/iShares/Vanguard/Invesco 日度持仓&份额&NAV  →  每日快照落 etf_fund_snapshot_daily  →  自建点时归档
公司行动：单独接一个 CA 源（否则 fail-closed）
```
- **优点**：数据成本近零。
- **代价**：(a) 授权灰区，商用/派生需逐家确认；(b) **无历史 → 6–12 个月才有足够样本**；(c) 公司行动要么单买要么 fail-closed；(d) 三家 cadence/口径不一，工程量在你这边。
- **定位**：即使走路径 A，也建议**同时**用 A 档做地面真值校验。

> 混合建议：**B 档买点时历史用于回测 + A 档发行商做每日真值校验 + 便宜源仅补 AUM/价格 context**。是否值得，取决于第六节的预算上限。

---

## 五、数据源验收 POC（把你的 §五 形式化，动工前必做）

**不写生产功能，先做 POC。** 选 10 只代表 ETF，连续 20 个纽约交易日：

样本（覆盖发行商 × 资产类别 × 结构）：

| Ticker | 发行商 | 类别 |
|---|---|---|
| SPY | SSGA | 大盘股票 |
| IVV | iShares | 大盘股票 |
| VOO | Vanguard | 大盘股票 |
| QQQ | Invesco | 科技成长 |
| VTI | Vanguard | 全市场 |
| IWM | iShares | 小盘 |
| HYG | iShares | 高收益债 |
| LQD | iShares | IG 债 |
| XLF | SSGA | 行业 |
| TQQQ | ProShares | 杠杆 |

**逐源打分（每源填一列，POC 期间实测，不靠供应商话术）：**

| 验收项 | 阈值 | 权重 |
|---|---|---|
| 份额/NAV/AUM 同日齐备 | 关键字段覆盖率 ≥ 98% | 必过 |
| 正常发布滞后 | ≤ 1 交易日 | 必过 |
| 历史可回补 | ≥ 3 年（最好 5 年）且为点时版本 | 路径 A 必过 |
| 公司行动识别 | 拆分/反拆/合并识别率 = 100% | 必过 |
| 无法确认 CA 时 | **必须 fail-closed**（见下） | 必过 |
| 修订是否留痕 | 有 revision_id/published_at | 加分 |
| 拆分前后 AUM 连续 | 无断崖 | 必过 |
| 与发行商官网一致 | 抽样 100% 一致 | 必过 |
| 授权 | 允许自动抓取 + 存储 + 派生 + 内部分发 | **一票否决** |

**fail-closed 规则（写进事实层）：**
```
无法确认公司行动
  → 标记 SUSPECT_CORPORATE_ACTION
  → 该 ETF 当日 etf_primary_flow_daily 置空
  → 禁止进入任何聚合
  # 绝不假定调整因子 = 1
```

**闸门**：无任何源通过 → **不启动 ETF 正式评分**，仅保留现有 `CONTEXT_ONLY` 观察（`data_contracts.py:53-54` 现状不变）。

---

## 六、待你拍板的业务决定（这张表框定，但只有你能定）

1. **预算上限**：接受路径 A 的付费点时历史（估中五位数/年，需询价），还是先走零成本路径 B 干等样本？
2. **回测时点**：要"尽快能回测"（→ 必须买点时历史，路径 A），还是可接受 6–12 个月在线积累（→ 路径 B）？
3. **授权范围**：系统产出是否会对外/跨团队分发？这决定发行商直连是否可用、以及 C 档 redistribution 条款是否要谈。
4. **成品 flow vs 自算**：接受 C 档现成 flow（算法不透明、但省事），还是坚持自建"Σ 日调整份额 × 日 NAV"（透明可审计、但要 B 档原料）？

---

## 七、落库映射（与审查 §四 一致）

```
etf_fund_snapshot_daily   -- 原始：份额/NAV/AUM/来源/版本（raw，单日事实）
etf_primary_flow_daily    -- 派生：经公司行动处理后的净发行（CA-adjusted）
corporate_actions         -- 硬依赖，缺失即 fail-closed
1/5/20 日窗口             -- 由 SQL/特征层算，不进原始表
```

---

## 附：核查来源

- [SSGA SPY daily holdings（xlsx 直连示例）](https://www.ssga.com/library-content/products/fund-data/etfs/us/holdings-daily-us-en-spy.xlsx)
- [etf-scraper（社区库，作者免责 T&C）](https://pypi.org/project/etf-scraper/)
- [Exchange Data International — Shares Outstanding（Official + Daily-Adjusted）](https://datarade.ai/data-products/shares-outstanding)
- [DTCC ETF Portfolio Data](https://www.dtcc.com/data-services/corporate-actions-and-reference-data/etf-portfolio-data)
- [Databento — Corporate Actions（点时）](https://databento.com/corporate-actions)
- [QUODD — Stock & ETF Data](https://www.quodd.com/stock-and-etf-data)
- [EODHD — Fundamentals（份额季度口径）](https://eodhd.com/financial-apis/stock-etfs-fundamental-data-feeds)
- [Barchart OnDemand — getETFDetails](https://www.barchart.com/ondemand/api/getETFDetails)
- [Bloomberg Professional — Funds Data](https://professional.bloomberg.com/products/data/enterprise-catalog/funds/)
- [AlphaEx Capital — Where to Find ETF Flow Data](https://www.alphaexcapital.com/etfs/etf-analysis-and-research/fund-flows-and-market-sentiment-in-etfs/where-to-find-etf-flow-data)
