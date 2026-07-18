# ETF 数据源与预算选型（P0A 决策）

- **决策 ID**：`capflow-etf-source-2026-07-18`
- **状态**：DRAFT — 待供应商样本、书面授权与前向 POC 验收
- **定位**：决定 ETF 一级市场配置流能否从 `CONTEXT_ONLY` 研究观察进入正式事实层
- **前置审查**：[资本流方案审查](../audits/2026-07-18-capital-flow-plan-review.md)
- **配套文件**：[供应商 RFI 与样本验收表](2026-07-18-etf-data-vendor-rfi.md)
- **首轮探针**：[10 只 ETF 基线记录](../audits/2026-07-18-etf-issuer-probe-baseline.md)
- **证据复核日**：2026-07-18

---

## 一、决策边界

旗舰指标统一命名为 `ETF_PRIMARY_MARKET_FLOW_ESTIMATE`。即使拥有日度份额和 NAV，它仍是创建/赎回单位的 NAV 价值估算，不等同于实际现金转账；实物申赎尤其不能写成“现金净流入”。

P0 需要以下字段：

| 字段 | 必要性 | 验收要求 |
|---|---|---|
| 基金级日度 shares outstanding | 必须 | 不是公司财报季度股本，也不是成分股持仓数量 |
| 同日 NAV | 必须 | 与份额使用同一基金、份额类别和估值日 |
| AUM / net assets | 强烈建议 | 用于恒等式校验与 Flow/AUM 标准化 |
| 公司行动 | 必须 | 拆分、反拆、合并、清盘、ticker/CUSIP 变更均需识别 |
| `published_at` / `available_at` | 路径 A 必须 | 能重建当时真实可见的信息集 |
| 修订 / vintage 历史 | 路径 A 必须 | 原始值不得被后值无痕覆盖 |
| 3—5 年历史 | 路径 A 必须 | 必须说明 adjusted 与 as-reported 口径 |
| 自动摄入、存储、派生、内部分发权 | 一票否决 | 以书面合同为准，不从网页可访问性推断授权 |

当前 `data_contracts.py` 的 yfinance 份额观察继续保持 `CONTEXT_ONLY`，不得因为本决策文档而升级。

---

## 二、证据等级

候选能力不再用笼统的“满足/不满足”表示，改用三种证据状态：

- **PUBLICLY_CONFIRMED**：供应商官方公开资料明确写出相应字段或能力；仍需合同和样本验收。
- **VENDOR_SAMPLE_REQUIRED**：公开资料不足，必须取得字段字典、真实样本和书面说明。
- **COMPANION_ONLY**：公开能力适合公司行动、PCF 或标识符等伴随层，不是基金份额/NAV 主源。

价格统一标记 `UNQUOTED`。公开终端席位价不能替代 API、批量存储、派生与分发授权报价。

---

## 三、候选源矩阵（修订后）

| 候选源 | 公开证据能确认的能力 | 尚未确认的关键能力 | 当前定位 |
|---|---|---|---|
| **FactSet Funds API / DataFeed** | 官方 Funds API 明确覆盖 ETF NAV、AUM、flows，并说明单日 flow 使用份额变化与 NAV 计算 | as-reported vintage、修订时间、公司行动处理、具体 ETF 历史深度与许可 | **第一轮 RFI 主候选**；`PUBLICLY_CONFIRMED` 到字段族，生产资格仍待样本 |
| **EDI Worldwide Shares Outstanding** | 官方说明含 Official 与 Daily-Adjusted shares，调整事件包含拆分、合并、回购、配股等 | 美国 ETF 基金级份额覆盖、逐基金日频、NAV、ETF vintage 与授权 | `VENDOR_SAMPLE_REQUIRED`；不得默认等同 ETF 日度基金份额源 |
| **QUODD** | 官方说明含 ETF NAV、公司行动、标识符连续性和调整历史 | 日度基金份额、flow、revision/vintage | `VENDOR_SAMPLE_REQUIRED` |
| **Databento Corporate Actions** | ETF 公司行动、调整因子、标识符变化及约六年 point-in-time 事件 | 基金级日度份额、NAV、AUM | **公司行动/点时伴随源**；`COMPANION_ONLY` |
| **DTCC ETF Portfolio Data** | 创建赎回篮子、PCF、成分构成及自 2007 年起的历史 PCF | 基金级 shares outstanding、NAV、AUM、flow vintage | **申赎篮子与 look-through 层**；`COMPANION_ONLY` |
| Bloomberg / LSEG / Morningstar | 企业级基金数据产品存在 | 本项目所需字段、频率、修订、授权与价格均需逐项证明 | `VENDOR_SAMPLE_REQUIRED`；不得写入未经证明的实时频率或预算数字 |
| 发行商官网 | SSGA 与 iShares 产品页公开展示部分基金的 NAV、AUM、shares outstanding；其他发行商字段覆盖不一 | 自动化许可、稳定接口、历史与修订、公司行动统一口径 | 地面真值和前向技术探针；不能默认可商用或可回补 |
| yfinance / 便宜基本面 API | 快照或报表口径字段 | 权威日度基金份额、点时历史、修订与公司行动闭环 | 仅 `CONTEXT_ONLY` |

### 关键纠偏

1. EDI 的公开资料不能证明其对 SPY/IVV/QQQ 等美国 ETF 提供基金级日度份额；必须先看 ETF 专项样本。
2. Databento 不承担份额/NAV 主源角色，只补公司行动、标识符和 point-in-time 事件。
3. DTCC 的核心价值是 PCF 与申赎篮子穿透，不把 PCF 误写成基金份额或 NAV。
4. FactSet 因官方资料明确出现 ETF NAV、AUM 和 flow，提升为第一轮直接询价对象；但仍不得跳过 vintage、授权和公司行动验收。
5. 所有成本在取得书面报价前均为 `UNQUOTED`，不以“中五位数”等未经报价的数字做预算基线。

---

## 四、批准的下一步：双线并行，不建设生产管道

### 线 A：统一 RFI 与历史样本回放

第一批联系顺序：

1. FactSet Funds API / DataFeed；
2. EDI（必须点名 ETF fund units/shares outstanding）；
3. QUODD；
4. Bloomberg、LSEG、Morningstar 比较组；
5. Databento 作为公司行动伴随源；
6. DTCC 作为 PCF / look-through 源。

所有供应商使用同一份 [RFI](2026-07-18-etf-data-vendor-rfi.md)，不得接受只有营销截图、没有字段样本的“满足”。

### 线 B：发行商官网前向探针

启动非生产探针，目标是验证：

- 官方页面/文件能否稳定访问；
- NAV、份额、AUM 分别属于哪个 source date；
- `ETag`、`Last-Modified` 和页面内容是否真实刷新；
- 不同发行商在周末、节假日和修订日的 cadence；
- 是否存在字段消失、页面模板变化和反爬限制。

探针只做不可覆盖的原始归档与字段覆盖检查：

- 不写 Supabase；
- 不进入 `metric_daily`、Waterline、评分、告警或日报；
- 不产生正式 flow；
- 抓取必须由操作者显式确认研究用途；
- 部署定时任务前必须完成逐发行商授权审查。

---

## 五、POC 样本与验收闸门

样本覆盖发行商、资产类别和结构：

| Ticker | 发行商 | 类型 |
|---|---|---|
| SPY | State Street | 大盘股票 |
| IVV | iShares | 大盘股票 |
| VOO | Vanguard | 大盘股票 |
| QQQ | Invesco | 科技成长 |
| VTI | Vanguard | 全市场 |
| IWM | iShares | 小盘 |
| HYG | iShares | 高收益债 |
| LQD | iShares | 投资级债 |
| XLF | State Street | 行业 |
| TQQQ | ProShares | 杠杆 |

执行窗口分三段：

1. **5—10 个交易日技术探针**：验证访问、归档、字段和发布时间，不等待 20 日才联系供应商。
2. **供应商历史事件回放**：至少回放 3—5 个拆分、反拆、代码变更或显著修订事件。
3. **20 个纽约交易日完整 POC**：评估覆盖率、发布滞后、修订行为和跨源一致性。

验收标准：

| 验收项 | 阈值 |
|---|---|
| shares/NAV 同日关键字段覆盖率 | ≥ 98% |
| 正常发布滞后 | ≤ 1 个交易日，且供应商发布计划有书面定义 |
| 路径 A 历史 | ≥ 3 年，优先 5 年，并可重建 point-in-time 信息集 |
| 公司行动识别 | 样本事件 100% 识别 |
| 修订留痕 | 原值、新值、`published_at`/`available_at`、revision 标识可追溯 |
| AUM 恒等式 | 解释 `shares × NAV` 与官方 net assets 的差异及容差 |
| 发行商抽样一致性 | 抽样值与 source date 均一致 |
| 授权 | 自动摄入、存储、派生、内部展示均有书面许可；否则一票否决 |

### Fail-closed 规则

```text
关键字段缺失或日期不一致
  → INCOMPLETE_SOURCE_SNAPSHOT
  → 不计算 flow

份额异常跳变且没有已确认公司行动
  → SUSPECT_CORPORATE_ACTION
  → 当日 ETF flow 置空并禁止进入任何聚合

授权状态未确认
  → RESEARCH_ONLY
  → 只允许隔离归档，不接生产系统
```

没有任何源通过全部硬闸门时，不启动 ETF 正式评分；现有 `CONTEXT_ONLY` 状态保持不变。

---

## 六、生产前的数据模型边界

待 POC 通过后再正式迁移，计划事实表为：

```text
etf_fund_snapshot_daily   原始/标准化份额、NAV、AUM、各字段 source date 与版本
corporate_actions         事件、调整因子、available_at 与来源
etf_primary_flow_daily    公司行动确认后产生的一级市场流量估算
```

`1/5/20D` 只由特征层从日事实聚合，不写回原始表。计算约定必须明确采用哪一天 NAV、是否使用 as-reported 或 adjusted shares，不能把供应商成品 flow 与自算 flow 混成同一指标版本。

---

## 七、公开证据来源

- [FactSet Funds API](https://developer.factset.com/api-catalog/factset-funds-api)
- [EDI Worldwide Shares Outstanding](https://developer.exchange-data.com/product/worldwide_shares_outstanding)
- [Databento Corporate Actions](https://databento.com/corporate-actions)
- [DTCC ETF Portfolio Data](https://www.dtcc.com/data-services/corporate-actions-and-reference-data/etf-portfolio-data)
- [QUODD Stock and ETF Data](https://www.quodd.com/stock-and-etf-data)
- [State Street SPY 产品页](https://www.ssga.com/us/en/individual/etfs/state-street-spdr-sp-500-etf-trust-spy)
- [iShares IVV 产品页](https://www.ishares.com/us/products/239726/ivv-ishares-core-sp-500-etf)
