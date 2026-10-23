# 独立核心资金充裕度指数 v1

实现目录：`dashboard_temporal_state`。本次发布版本：`core_funding_v1.2.1_m2lag3`。
2026-10-23发布说明、验收与回滚边界见 [两指数更新说明](funding_waterline_update_20261023.md)。

## 范围与边界

本模块与六维 `liquidity_v3.0_fixed_native` 水位仪并行，不替换该指标，不修改哨兵、
期权/Gamma、交易资格、战术评分或预警门控。所有结果均为影子观察，
`shadow_mode=true`、`decision_eligible=false`。

0-100 越高表示模型衡量的相对资金条件越充裕，不代表现金比例或上涨概率。
20/40/60/80 分段、融资压力绝对护栏和趋势变化5分门槛是未校准的研究规则。
不把 SPY、QQQ、VIX、广度、M1、G3 或 NFCI 放入评分，避免重复计分和价格自我解释。
海外央行、RDE、应急融资和国债结算留待独立数据契约验收后扩展，v1不伪造这些数据。

## 固定模型

四组各占25%，组内权重固定。每组必须有全部合格分量才有组分，总分要求四组完整；
不因缺数重新分配权重，也不按数据成熟度改变权重。缺数、过期或样本不足显示NA。

| 组 | 分量 | 组内权重 | 原生历史最低样本/基线最大样本 |
| --- | --- | --- | --- |
| 存量与缓冲 | 准备金/银行总资产 | 60% | 104/260 周 |
| 存量与缓冲 | 美国M2同比，观测月末延后3个月 | 40% | 36/120 月 |
| 边际净供给 | 净流动性4周变化/前期Fed资产 | 70% | 104/260 周 |
| 边际净供给 | 净流动性13周变化/前期Fed资产 | 30% | 104/260 周 |
| 融资松紧 | SOFR-IORB | 50% | 252/1260 日 |
| 融资松紧 | EFFR-IORB | 30% | 252/1260 日 |
| 融资松紧 | SOFR第99百分位-中位数 | 20% | 252/1260 日 |
| 银行信用活动（供需混合） | 银行贷款13周增长 | 60% | 104/260 周 |
| 银行信用活动（供需混合） | 大中型企业贷款标准收紧净比例 | 40% | 20/40 季 |

每个分量对“当前点之前”的原生历史计算中秩百分位，负方向分量取100减百分位。
这里的分量分数不是总分历史百分位。总分历史百分位另外按同版本、同模式的历史
周样本计算，至少52周；没有足够实测发布历史时为NA，回放参考另行标注。

准备金来自周均WRESBAL，银行资产来自H.8季调周数据，比例是规模调整的代理，
不是纽约联储RDE或精确准备金需求量。M2、银行存款高度重叠，所以存款只作背景。

净流动性 = WALCL - WDTGAL - RRPONTSYD，统一为十亿美元。三者只在同一观测日
完整时计算；WDTGAL是周三余额，不能替换为周均WTREGEN或把DTS日余额静默拼接。
只对净变化计分，Fed/TGA/RRP单项用于1/4/13周贡献拆解，不重复加权。
以Fed资产规模归一化，避免净流动性接近0时百分比爆炸。4周、13周不是严格20、60交易日。

融资压力独立于总分。观察护栏需要利差达到绝对值且落入历史弱20分位：
SOFR-IORB与EFFR-IORB各5bp，SOFR尾部25bp。显著压力硬护栏分别50/25/75bp，
即使总分缺失也可显示压力。它们不是已验证的交易阈值；NOT_TRIGGERED不代表市场健康。

20交易日变化按NYSE交易日历精确定位，需要窗口内完整发布记录，缺一天就不计算。
变化至少5分且至少两个分组同方向变化5分，才标记趋势候选；不声称已经确认股市转折。
组贡献变化包括历史标准化基线的变化，不可解释为每一分对应固定美元流入。

## 数据与时间契约

新模块直接使用FRED/ALFRED的首次发布历史，不读取没有发布时间的旧宽表来冒充PIT数据。
原生输入保存 series、observation_date、release_date、available_at、value、source/version。
使用 `output_type=4`，`realtime_start=1776-07-04`，分页读取；不把今日修订值放回过去。
FRED对JSON另有每次2000个历史版本日期的限制，超过时按实时版本日期区间从旧到新分批，
同一观测日只保留最早版本；同版本冲突则拒绝，不把后一区间首日的修订值冒充初值。

FRED只提供首发日期，未提供精确时刻时，保守地从**下一纽约自然日00:00**开始使用；
若实际完成抓取的时间更早，则可从该抓取时间使用。始终同时检查观测日和可用时间。
这不是精确到分钟的官方发布时间回测，不应据此评估盘中领先性。

v1冻结首次发布值，后续修订不混入；这使历史可重放，但也保留初值误差、口径调整和
银行重分类噪声。完整“逐次修订版本”模型是后续版本，不在本版伪装实现。
IORB没有自动接IOER，故包含全部分量的总分不会人为延伸到IORB存在之前。
即便从2008年下载，也不意味着2008年起都有可评分结果。

`LIVE_SHADOW`：实际运行发布值，只在纽约16:45或之后生成。
`PIT_REPLAY`：按每天纽约16:45回放，可供研究，不计入真实运行观察期。
同一日期/版本/模式只插入一次，并读取数据库确认；重跑返回原已存结果，
不能悄悄刷新历史。故同日低覆盖快照也不会自动覆盖，修复后需明确的新版本或更正流程。
实际发布值的20日变化不会用回放数据凑数；邮件另外给出明确标记的回放参考。

## M2三个月延后计分

最初只增加了不计分的传导观察。用户随后明确要求调整核心指数的M2计分时间轴，
因此新增公式版本 `core_funding_v1.1_m2lag3`，**真正改变M2对总分的作用日期**，
不是只把图上的线移动，也不是把整个资金指数延后。

增速仍是美国M2SL同比 `100 * (M2_m / M2_(m-12) - 1)`，不改成环比或全球M2。
M2权重保持存量组的40%、总指数的10%，其余分量、权重、原生日期均不变。
先按观测月末延后3个自然月，再选择当日已生效值及其之前的36至120个原生月度同比样本计分。
尚未生效的已公布数据也不得进入百分位基线。

例：2026年1月同比从2026-04-30纽约时间00:00起生效，不是4月1日；
2023年11月对应2024-02-29。月末为非交易日时，在之后首次交易日报告体现，
不提前到上一个交易日。不是90天、63个交易日或抓取日加3个月。
可用起点取对齐日期与当前值/同比基期实际可用时间的较晚者，不能提前使用未公布数据。

对应观测月或去年同月缺失时，M2分量及总分为NA，不拿相邻月份或当前已公布值替代，
不重新分配权重。保留原始 `source_date` 与来源年龄，并新增 `effective_date`、
`effective_age_days`、`expected_observation_month`、`source_available_at`、`eligible_from`；
对齐后的新鲜度门槛为45天，不能把人为延后三个月误判为原始数据失效。
`validation=USER_SPECIFIED_UNVALIDATED_HYPOTHESIS`，全系统仍是影子观察，不能产生交易资格。

原公式v1.0的数据库行保留，新版历史按同一新公式重算，不拼接旧版分数。
数据库主键已包含计算版本，不需迁移。新版实际发布历史积累满20个交易日前，
其20日变化可能为NA；回放参考另列，不能用旧版本实测数据补齐。

独立传导时钟 `m2_transmission_shadow_v1` 继续提供当前已公布值作对照，
其JSON顶层对象 `score_eligible=false` 表示该附加对象不额外加分；
真正计分的分量保存在 `result.components.m2_yoy`。正文明确展示已经计入总分的滞后分量，
附加时钟生成失败不影响主计算，但主M2分量缺数仍会阻止输出总分。

另有美/欧/中/日固定四货币区代理的历史研究与右移时间轴图，不冒充完整世界M2。
最新修订值回溯统计及停止更新的数据，不可用于宣称已完成实时全球M2预测回测。
详细方法和结果见 [M2滞后研究](m2_lag_research_20260928.md)。

该改动需要独立发布才会出现在云端邮件：发布包必须包含 `core_funding_m2.py`，
同步 `core_funding_index.py`、`core_funding_report.py`、`core_funding_pipeline.py`。
研究脚本、统计依赖与长历史抓取不接入16:50定时任务。
不需要数据库迁移。部署时继续遵守独立目录、原生产文件及其他cron不变的边界。

## 交付文件与输出

- `core_funding_sources.py`：首发数据、分页、重试、校验和缓存。
- `core_funding_index.py`：独立评分、覆盖率、压力、归因和回放。
- `core_funding_report.py`：中文摘要和两层共轴PNG，主图为价格与指数双纵轴叠加。
- `core_funding_pipeline.py`：独立命令行、写库读回验证、价格对比。
- `core_funding_job.py`：独立配置、交易日检查、独立邮件和防重复投递回执。
- `core_funding_m2.py`：美国M2延后计分的时间约定与附加传导观察，不重复计分。
- `core_funding_diagnostics.py`：多周期变化、分数贡献、同日资金明细与独立信用观察，不改变总分。
- `core_funding_context.py`：贷款需求、SOFR成交量和Fed资产构成补充观察，不计分。
- `scripts/run_core_funding.sh`：单实例、低内存保护、单线程、限时和低优先级入口。
- `migration_20260925_core_funding.sql`：仅新增 `core_funding_daily`。

PNG上层叠加SPY/QQQ复权收盘价（左轴，共同起点100）与资金指数红色虚线（右轴，固定0-100）；
下层保留20交易日变化与压力/缺数底色。双轴用于对照变化时点，曲线交叉或视觉斜率不代表
相同数量、相关性或因果关系。基准价格来自yfinance，仅用于展示，缺失时不回退为未复权价格。
当前实际发布分数以单点标出。此图不是收益预测验证，也没有拟合股价权重。

独立CLI默认输出到 `reports/`；云端固定在新目录的 `reports/`，不使用旧程序REPORT_DIR：

- `core_funding_YYYY-MM-DD.png`、同名 `.json`：图表和本次结果/回放。
- `core_funding_diagnostics_YYYY-MM-DD.png`：变化、分组贡献及信用观察附图，邮件的第二张可选附件。
- `core_funding_cache/`：每个数据源的缓存，6小时复用，失败保留原时间的缓存。
- `core_funding_inputs/<sha256>.json`：不可变内容寻址输入档案，结果保存对应哈希和路径。
- `core_funding_credit_cache/`、`core_funding_credit_inputs/<sha256>.json`：独立信用源缓存与档案。

需备份输入档案以及数据库；Git忽略reports，不是把运行数据上传GitHub。
本地同日输出文件可以重新生成，但数据库实际发布快照不会被重写。

### 2026-10-05 诊断增强

按外部GLI材料审查增加报告解释，不改核心公式、权重或M2滞后规则。
JSON顶层 `diagnostics` 使用独立版本 `core_funding_diagnostics_v1`，不额外写入核心快照表。
1/5/20/63交易日变化严格使用同版本同模式的完整NYSE日期窗口，实时报告不拼接回放记录。
不足时为NA；方向标签以0.5点为暂定描述阈值，不等同于原20日趋势候选门槛。
最新公布值、用于净流动性的同日值分别列示，金额一律B，即十亿美元。

信用旁证为HY、IG、BBB OAS，percent乘100转bp，单独抓取ALFRED首次发布历史；
不引入原水位仪/旧数据库运行依赖。最多延迟2个NYSE交易日且不超过7个自然日才算新鲜；
5日变化需要连续6个日期，缺任何点不压缩为“最近6条”。
新鲜水平覆盖与变化窗口是否完整分别记录，部分可用不得报告为完整正常。
高位门槛500/150/200bp、5日走阔门槛30/10/15bp均未校准，只作影子观察。
公开ICE历史窗口目前受3年限制，原始数据只作私人研究，不发布到GitHub。

抓取采用单独缓存和25秒尽力而为的I/O预算，附加错误不阻断原分数和主图；
外层8分钟硬超时和16:50运行设置不变。尚未云端部署或完成真实邮件/自然cron验收。
完整审查见 [外部GLI审查](audits/2026-10-05-external-gli-review.md)。

## 本地验证与运行

建议先建独立虚拟环境，不直接升级云端全局Python：

```bash
python3 -m venv /tmp/core-funding-venv
/tmp/core-funding-venv/bin/pip install -r requirements-core-funding.txt
/tmp/core-funding-venv/bin/python -m unittest discover -s tests -p test_core_funding.py -v
```

独立CLI通过环境变量提供FRED_API_KEY，不读取任何旧程序的 `market_config.py`。
定时任务则只读取自己的600权限JSON配置，不把密钥写进代码或Git。
独立配置是部署时副本，不会自动跟随旧配置变化；以后轮换API或SMTP凭证时，
也需更新独立目录的 `config.json`，不要重新引入对旧程序配置模块的运行时依赖。
第一次会抓取13个官方序列的首发历史，可能需要数分钟；后续使用缓存。

```bash
# 纽约16:45之后；默认只生成本地报告，不写数据库、不发邮件。
python3 core_funding_pipeline.py --output-dir reports --sessions 300

# 历史预览，明确带时区。该日期应替换为实际已结束的NYSE交易日。
python3 core_funding_pipeline.py --as-of 2026-09-24T16:45:00-04:00 --no-prices

# 已保存输入可断网重放；路径替换为实际档案。
python3 core_funding_pipeline.py --archive reports/core_funding_inputs/HASH.json \
  --as-of 2026-09-24T16:45:00-04:00 --no-prices
```

## 云端接入顺序

按2026-09-25最新隔离要求，**不再接入auto_analyst，不向market_dashboard/quant_bot/TradingRadar上传新模块**。

1. 只读核查纽约时区、原cron、可用内存；备份原cron与三处生产目录Python文件哈希。
2. 新建 `/home/winters_dong426/core_funding_monitor/`，内部独立 `.venv`、`config.json`、
   `releases/`、`current`、`cache/`、`reports/`、`logs/`、`backups/`。
   只上传独立 `core_funding_*.py`（新增诊断模块后共七个）、专用依赖、脚本、测试和迁移文件。
3. 仅安装到新虚拟环境。配置复制必要FRED/Supabase/SMTP字段到新600权限文件，
   不改源配置，不带入IBKR、AI、Bark或交易参数。旧程序不加载任何新模块。
4. 在Supabase SQL Editor单独执行 `migration_20260925_core_funding.sql`，
   只创建新表与该表索引/权限，不改原表。新表开启RLS，禁止anon/authenticated访问；
   后端使用service_role或Supabase secret key，不能使用公开key。
   清除该表默认授权后，仅授予后端SELECT/INSERT，禁止UPDATE/DELETE/TRUNCATE。
5. 先运行新模块测试，再用上一已结束交易日做独立dry-run，检查真实来源覆盖率、图表和资源占用。
   之后加 `--write --email`，确认新表写入读回及SMTP接受，邮件明确标记历史回放验收。
6. 通过后在原crontab末尾仅追加独立任务（云端系统时区必须为America/New_York）：

```cron
50 16 * * 1-5 /bin/bash /home/winters_dong426/core_funding_monitor/current/scripts/run_core_funding.sh --write --email
```

7. 前后比对原文件哈希与原cron，除新增块外保持完全不变。等待自然cron、最新实测数据库行和用户实际收到邮件。
   测试回放不能代替这一验收；纽约节假日自动跳过，不影响旧任务。

手动验收命令（回放日期替换为已结束NYSE交易日）：

```bash
/bin/bash /home/winters_dong426/core_funding_monitor/current/scripts/run_core_funding.sh \
  --as-of 2026-09-24T16:45:00-04:00 --write --email
```

资源控制：低于800MiB可用内存则本次不启动；虚拟地址空间上限1200MiB、CPU时间480秒、
墙钟8分钟，终止后最多30秒强制退出缓冲；nice19、空闲IO优先级、数值计算线程数1。
16:50纽约时间启动，为17:00关机留出余量；超时会退出而不是承诺每次必然完成。
服务器时区为America/New_York，自动跟随夏令时。共享服务器不意味着物理隔离，
这些限制用于控制额外占用，不能承诺绝对零资源竞争。

独立邮件使用原配置的发送/接收邮箱，但主题和邮件单独发送；不会增加原复盘邮件附件。
成功提交SMTP后写SENT回执，同日/版本/模式不重复发送；若断线留下PENDING，需人工核对后处理，
不会为重试而重复投递。没有数据库读回确认，不发送成功报告。

停用只需注释独立的16:50资金指数行，不删除/恢复其他cron；同一时间其他任务不能误停。
保留专属数据表和输入档案，避免丢失已发布历史。JSON和PNG是可再生输出，输入档案应单独备份。

```sql
SELECT report_date, calc_version, observation_mode, score, coverage,
       result->'funding_pressure' AS funding_pressure,
       result->>'input_digest' AS input_digest
FROM public.core_funding_daily
ORDER BY report_date DESC, observation_mode
LIMIT 10;
```

迁移/写库失败会显示FAILED；图表失败只降级图表，基准缺失不会影响指数；
此模块任何异常不参与原报告的AI/预警判定。不能把“邮件正常到达”当作新表写入成功。

## 官方契约参考

- [ALFRED观测输出与初值模式](https://fred.stlouisfed.org/docs/api/fred/series_observations.html)
- [FRED实时版本含义](https://fred.stlouisfed.org/docs/api/fred/realtime_period.html)
- [WDTGAL周三TGA余额](https://fred.stlouisfed.org/series/WDTGAL)
- [WTREGEN周均TGA，不能混用](https://fred.stlouisfed.org/series/WTREGEN)
- [WRESBAL准备金](https://fred.stlouisfed.org/series/WRESBAL)
- [H.8银行资产](https://fred.stlouisfed.org/series/TLAACBW027SBOG)
- [SLOOS贷款标准](https://fred.stlouisfed.org/series/DRTSCILM)
