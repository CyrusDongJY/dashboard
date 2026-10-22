# 行业历史补充与市场内部结构发布

## 可补和不可补

行业ETF历史复权收盘价可直接抓取，用11个行业和SPY计算逐日的5日超额收益。
轮动观察只需连续6个这样的窗口，最低需要11个连续交易日价格，不必等待6次日报。
程序保留22个交易日价格，展示当日1/5/21日收益；历史轮动使用其中6个5日窗口。

历史价格为抓取时的复权版本，不保证与历史当时发布值相同。因此标注
`RECONSTRUCTED`、真实`captured_at`、`point_in_time_eligible=false`，仅供本期描述性观察。
不可作为原时点的回测信号，也不证明实际资金流入。价格缺口不前向填充。
历史前500市值股票的涨跌家数与平盘数不可用当前名单倒推，仍逐日积累实采记录。

## 数据与安全边界

- 常规采集：`ultimate_dashboard.py`保存原始价格及股票样本到`full_metrics.market_internals`。
- 一次性补充：`backfill_sector_history.py`只更新既有交易日记录的
  `full_metrics.sector_history_reconstruction`。不创建伪造的历史市场记录。
- 执行前备份完整原记录；复用`auto_review.lock`，对`ingested_at`作乐观并发校验；写后独立读回。
- 原分数、原广度、入库时间及其他JSON命名空间保留。重建时间不会伪装成原交易日时间。
- 日报先使用合格实采窗口，再用截止前可得的历史价格补足缺失日期。邮件、HTML、PDF都标注重建数量。
- 不修改原定时表、09:20发送时间、旧报告归档或发送回执；无需数据库迁移。

```bash
cd /home/winters_dong426/market_dashboard
/usr/bin/python3 backfill_sector_history.py
# 确认完整6个窗口后，显式写入；日期默认最近已收盘交易日。
/usr/bin/python3 backfill_sector_history.py --apply \
  --backup-dir /home/winters_dong426/deploy_backups/sector_history_20261023
```

## 本地验证

2026-10-23：547项主测试和5项隔离格式化测试通过，共552项；2项可选M2依赖测试跳过。
其中69项为股票样本、行业收益、历史重建与报告板块专项测试。
旧测试模块导入会替换交易日历，因此主测试预载真实日历，格式化测试独立执行。

本次提交包含已在云端运行的FRED/宏观部分保存依赖，使GitHub备份能独立还原当前采集链路。
不夹带其他工作区的core_funding、M2研究、盘前/盘后其他程序改动；输出、备份、配置不提交。
`auto_analyst`仅对新增原始样本字段作简短摘要，防止旧盘后邮件展开数百条成分记录。

云端部署、真实补充和验收结果在执行后追加。
