# ETF 发行商前向探针：首轮基线

- **日期**：2026-07-18
- **模式**：`NON_PRODUCTION_RESEARCH_ONLY`
- **探针版本**：`etf_issuer_probe_v1`
- **样本**：10 只 ETF，一次公开产品页抓取
- **原始归档**：本地 `.probe_data/etf_issuer_verified/`，已从 Git 排除
- **生产写入**：无
- **资金流计算**：无

---

## 一、结论

10 个目标页面全部返回 HTTP 200，但只有 6 个页面在首轮解析中同时得到同日 NAV 与 shares outstanding。页面访问成功率是 100%，关键字段覆盖率只有 60%，因此不能把“官网可访问”当成“数据源合格”。

所有完整记录仍标记 `RESEARCH_ONLY`，原因是发行商自动摄入、存储与派生授权尚未书面确认；所有不完整记录均为 `INCOMPLETE_SOURCE_SNAPSHOT`，不会计算或聚合 flow。

---

## 二、首轮观测

| Ticker | 发行商 | 页面结果 | NAV / 日期 | Shares outstanding / 日期 | AUM | 判定 |
|---|---|---|---|---|---|---|
| SPY | State Street | HTTP 200 | 750.83 / 2026-07-16 | 1,052,330,000 / 2026-07-16 | 790.12112B | `RESEARCH_ONLY` |
| IVV | iShares | HTTP 200 | 746.73 / 2026-07-17 | 1,178,900,000 / 2026-07-17 | 880.32389B | `RESEARCH_ONLY` |
| VOO | Vanguard | HTTP 200 | 未解析 | 未解析 | 未解析 | `INCOMPLETE_SOURCE_SNAPSHOT` |
| QQQ | Invesco | HTTP 200，重定向至 QQQ 专页 | 未解析 | 未解析 | 未解析 | `INCOMPLETE_SOURCE_SNAPSHOT` |
| VTI | Vanguard | HTTP 200 | 未解析 | 未解析 | 未解析 | `INCOMPLETE_SOURCE_SNAPSHOT` |
| IWM | iShares | HTTP 200 | 294.23 / 2026-07-17 | 279,100,000 / 2026-07-17 | 82.11832B | `RESEARCH_ONLY` |
| HYG | iShares | HTTP 200 | 79.63 / 2026-07-17 | 207,400,000 / 2026-07-17 | 16.51529B | `RESEARCH_ONLY` |
| LQD | iShares | HTTP 200 | 107.53 / 2026-07-17 | 328,100,000 / 2026-07-17 | 35.27967B | `RESEARCH_ONLY` |
| XLF | State Street | HTTP 200 | 56.76 / 2026-07-16 | 987,150,000 / 2026-07-16 | 56.03114B | `RESEARCH_ONLY` |
| TQQQ | ProShares | HTTP 200，响应为内容 JSON | 未解析 | 未解析 | 未解析 | `INCOMPLETE_SOURCE_SNAPSHOT` |

“未解析”只说明当前公开页面响应没有形成合格标准化记录，不等同于发行商完全不提供该字段。可能原因包括动态接口、独立下载文件、地理页面差异或页面模板尚未适配，后续必须寻找官方结构化端点或由发行商确认。

---

## 三、质量检查发现

1. iShares 4/4 样本在页面中公开给出同日 NAV、份额和 net assets，首轮字段最完整。
2. State Street 2/2 样本字段完整，但本次 source date 为 2026-07-16，比 iShares 样本的 2026-07-17 早一个交易日；需要连续观测其发布时间，而不能只按抓取时间判断新鲜度。
3. Vanguard、Invesco 和 ProShares 的普通产品页不能直接满足当前标准化解析；应优先寻找官方 JSON、CSV/XLSX 或下载接口，而不是扩大脆弱的 HTML 正则。
4. 首次实抓暴露过一次 SSGA 文本歧义：NAV 释义中的 “shares outstanding” 被误识别为字段标签。探针现已增加：
   - 标签上下文约束；
   - `shares × NAV ≈ AUM` 恒等式闸门；
   - 对应回归测试。
5. 即使字段完整，探针仍强制 `eligible_for_flow=false`；当前没有任何记录进入正式资金流事实层。

---

## 四、接下来的 POC 动作

1. 连续运行 5—10 个纽约交易日，记录每家发行商的 source date、抓取时间、内容哈希、ETag/Last-Modified 与字段覆盖变化。
2. 为 VOO、VTI、QQQ、TQQQ 核实官方结构化下载/API；未确认前保持 fail-closed。
3. 向 FactSet、EDI、QUODD 等候选方发送统一 RFI，索取相同十只 ETF 的字段样本、历史事件与书面授权。
4. 收到供应商样本后，与发行商观测逐值核对，并单独回放拆分、反拆和修订事件。
5. 在 20 个交易日覆盖率、公司行动和授权闸门通过前，不建生产表、不升级质量等级、不对外输出方向性资金流。
