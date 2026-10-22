# 市场内部结构观察 v1.1 / 日报 v1.5

## 目的和边界

在微观环境下增加独立解释板块：涨跌覆盖、领导结构、持续变化。
不增加总分，不给宏观或微观环境增加投票，不改变原异常预警权重。
轮动、参与扩散、行业收益差都不是实际资金流，不能证明流动性宽松。

v1.0最初为本地实现；v1.1增加行业价格历史重建，部署验收另记于本文末尾。
原有日报内容与冻结归档保留。`daily_review`仅修正标签语义：
原兜底`Rotation`改为`Mixed`（涨跌分化）；原战术“资金轮动”按实际条件显示为
“权重集中”或“内部结构分化”，规则版本升为`daily_review_v1.1-shadow`。

## 数据契约

复用`market_history.full_metrics.market_internals`，无需新的SQL迁移。
`UltimateDashboard.get_db_payload`每日生成，沿用原`auto_review`持久化流程。

| 数据 | 来源与口径 | 有效性约束 |
|---|---|---|
| 涨跌分布 | TradingView，NYSE/NASDAQ市值前500股票样本 | 有效价格至少450/500；同日收盘5分钟后采集；去重；平盘保留 |
| 11行业 | XLK/XLF/XLV/XLY/XLP/XLE/XLI/XLU/XLB/XLRE/XLC | 标普500行业代理，非全市场行业广度 |
| 收益与等权差 | yfinance显式`auto_adjust=True`的原始Close | 不使用`ffill`；完整1/5/21个NYSE交易日窗口（2/6/22点） |
| 中期参与趋势 | 现有合格`metric_daily.pct_20ma` | 同规则版本、实际观测日、连续窗口；复用但不额外计票 |

原始股票名单、涨跌幅、Close、SMA和成交量保存，均线有效分母单独统计。
上涨/下跌/平盘占比以有有效价格和涨跌幅的股票为分母，不要求成交量非空。
新上涨占比包含平盘，旧`pct_adv`排除平盘且依赖成交量；两者绝不拼接。
未返回股票、价格缺失、真实平盘分开处理。重复ticker或超出500样本使该次分布不可确认。

扫描器未给出逐股源时间，因此`source_date`明确为`CAPTURE_SESSION`，并记录
`provider_timestamp_status=UNAVAILABLE`、`closing_auction_inclusion=UNVERIFIED`。
采集契约合格不等于交易所官方终值，也不能证明每个源报价没有延迟。
每日名单可能变化，历史曲线是同筛选方法的样本，并非固定成分队列。

股票原始样本和行业窗口一起带`computed_at`、`captured_at`、观察版本保存。
行业窗口使用价格实际抓取时间，不用报告生成时间把盘中价格升级成收盘价格。
数据库重试时整个`market_internals`对象原子替换，不把两次采集的计数、分母、
窗口质量和价格拼成一组。原有宏观部分补缺规则不变。

## 描述性规则

这些是版本化展示阈值，未经预测回测，仅影子观察：

- 广泛上涨/下跌：相应有效样本占比至少65%；其余为涨跌分化。缺测为证据不足。
- 跨行业扩散：合格广度与至少8/11行业绝对涨跌同向。
- 权重集中上涨：SPY为正、RSP-SPY不高于-0.5个百分点、上涨比例低于60%。
- 等权相对占优：RSP-SPY至少+0.5个百分点且多数样本上涨；不解读为精确贡献归因。
- 联合领导结构须有同日广度、全部11行业及SPY/RSP。行业缺失不能降低分母后宣称跨行业扩散。
- 持续性以20日均线上方比例的变化为依据；单日1个百分点为噪声线。
  连续至少3日同向为持续改善/恶化，前两日同向、最新反向只称转折待确认。
  5日/21日变化需完整6/22点窗口；真实缺口断窗。
- 轮动迹象需连续6个交易日完整的行业5日相对收益：相较5日前，至少2个行业进入前三，
  最新连续3日保持前三且各自超额至少+0.5个百分点，同时至少2个旧领涨行业退出。
  第三/第四名差不足0.1个百分点不据排名确认。否则为未确认或历史不足，而不是默认轮动。

当“当日广泛上涨”与“20日均线广度持续恶化”并存时，报告单独提示窗口分歧，
不自动宣布趋势反转。绝对下跌但跑赢SPY的行业不会计入上涨行业数。

## 报告和积累

- 邮件、离线HTML和PDF使用同一冻结JSON；网页版在微观环境后展示新板块。
- HTML包含涨跌分布条、行业1/5/21日绝对与相对热力表、近21交易日广度柱图。
- PDF新增第二页内部结构，原趋势图移到第三页，原战术压力板块保留。
- 无数据时显示灰色/待采，绝不补零；有旧版记录也不反推出原来未保存的平盘数。
- 行业轮动需要连续6个完整的5日超额收益窗口，可用保存的历史复权价格重建，不必等待6次日报。
  收盘后实采窗口优先；缺少实采日期时使用独立标注的`RECONSTRUCTED`窗口。
  历史股票涨跌/平盘家数不据当前名单重建。旧报告、旧评分和报警均不重算。
- 日报只读拉取近70个自然日的`market_history`，不下载无限历史股票快照。
- 09:15冻结和09:20发送不变；晚于截止入库或计算的观测不能进入当期报告。
- v1.2、v1.3、v1.4归档可继续校验、读取；不重写或补发已存在的报告。

## 验证与预览

新增测试覆盖样本分母、平盘、缺数、重复ticker、半日市、截止时间、版本切换、
缺失行业、相对抗跌、连续窗口、轮动排名并列、原子持久化、HTML转义和旧归档。
预览是显式模拟数据，不能作为2026-10-22的真实行情分析。

```bash
/tmp/dashboard-digest-runtime/bin/python -m pytest -q \
  tests/test_market_internals.py tests/test_liquidity_digest_internals.py \
  tests/test_liquidity_digest.py tests/test_liquidity_digest_states.py \
  tests/test_liquidity_digest_domains.py tests/test_liquidity_digest_html.py \
  tests/test_liquidity_digest_availability.py tests/test_daily_review.py \
  tests/test_macro_recovery.py
```

旧`test_auto_analyst_formatting.py`在导入时全局替换交易日历，需单独运行。
`test_environment_dashboard.py`会在依赖尚未导入时安装简化日历桩；全量回归应先导入真实日历，
避免把测试桩缺少`valid_days`/`market_close`误认为生产错误。不要为通过测试把生产日历改成工作日近似。

2026-10-23 v1.0初版（增加历史重建之前）的本地验收结果：

- 预载真实日历后主测试集532项通过，独立格式化测试4项通过，共536项。
  2项M2研究测试因可选`statsmodels`依赖缺失跳过；有1条既存Matplotlib弃用警告。
- 相关Python文件编译及`git diff --check`通过。
- 模拟完整HTML和邮件HTML在1440px桌面、390px手机均无页面横向溢出、无脚本错误，
  11行业行完整，原3张内嵌图加载正常；宽行业表在自身容器内横向滚动。
- 三页PDF逐页渲染检查，中文字体嵌入，图例无重叠；冻结预览的5个文件哈希校验通过。
- 最终预览目录：`output/market_internals_v1_5_preview/demo/2026-10-23/`。
  数据均为模拟；本轮没有线上真实采集、数据库读回或自然定时运行验收。

```bash
/tmp/dashboard-digest-runtime/bin/python -c \
  'import pandas_market_calendars; import pytest; raise SystemExit(pytest.main(["-q", "--ignore=tests/test_auto_analyst_formatting.py"]))'
/tmp/dashboard-digest-runtime/bin/python -m pytest -q tests/test_auto_analyst_formatting.py
```

## 部署清单

1. 先备份云端原文件和现有发布链接，确认待部署内容不夹带其他线程改动。
2. 原采集目录至少更新`market_internals.py`、`ultimate_dashboard.py`、`macro_recovery.py`；
   使用`daily_review.py`的现有报告进程同步更新标签修正。
3. 独立日报发布包带上根目录`market_internals.py`和整个`liquidity_digest`目录；
   安装器新增测试还需`tests/test_market_internals.py`、`tests/test_liquidity_digest_internals.py`。
4. 比较文件SHA、生产解释器编译和测试；不在09:15-09:25冻结发送窗口切换。
5. 验证下一次自然盘后采集落库的观察版本、日期、有效样本数、行业窗口质量与原始样本。
6. 验证下一次09:20自然日报的新板块、附件哈希与发送回执。不能仅凭模拟预览宣称部署成功。

只部署日报渲染端不会自动创造采集数据；采集端和日报端必须配套更新。

## v1.1实际部署

已部署云端35.200.111.243，最终代码为`b9edf18`。六个行业窗口完成真实补充及独立读回，
旧股票涨跌家数未重建，原报告归档未改写。完整过程、缺口来源、版本及回退位置见
[行业历史补充与部署验收](sector_history_reconstruction_20261023.md)。
