# 宏观数据部分采集与历史补缺

## 行为变化

- `auto_review.py` 不再因 `macro_score=None` 丢弃整行：原始结果先存本地 SQLite outbox，再合并入 `market_history` 并读回核验。
- 宏观评分公式、权重和最低 70% 覆盖率不变。覆盖不足时评分保持 NULL，不调用 AI 生成宏观结论；邮件改为“部分数据已保存”。
- `full_metrics.capture_quality` 记录本次状态、缺失字段、覆盖率及上游失败原因。同日重跑不会以 NULL 覆盖已有有效字段；原分数与其输入组保持一致。
- CSV 同样不再以空值覆盖同日有效数据。
- FRED HTTP 200 的 HTML 维护页也被识别为故障；每个请求最多两次尝试，维护/网络故障会触发本轮源级熔断，其他数据源继续工作。错误不包含 API key 或完整请求 URL。
- 保留云端已经部署的 VIX 30D/3M 同日原生收盘价修复。

## 自动补缺

原有每日任务内执行，无需新建 cron。每次发现最近 10 个自然日内的缺失交易日，持久化进入队列；已经入队的日期不会随窗口滚动消失。每轮最多处理两个历史日期，源请求有超时、重试上限与 150 秒调度预算。单次已发出的网络请求可能略超过调度预算。

历史查询限定 `observation_end=目标日期`，并再次裁剪日期。FRED、Treasury 日度 TGA 与日线价格可补缺；实时 PCR、历史截面广度等没有可靠回溯依据的指标不编造。

补缺只填缺口，不覆盖已存在的有效值。每个补入字段保留原生数据日期、来源、重建时间。TGA 沿用当前生产口径：FRED 周度历史加近 45 日财政部日度覆盖。

FRED 的后修订数据不冒充历史实时信息：

- `macro_score` 保留原始评分或 NULL；不会被事后补算覆盖。
- `full_metrics.original_macro_evidence` 保留原始证据。
- 事后评分放在 `full_metrics.macro_recovery.reconstructed_macro`。
- `point_in_time_eligible=false`，不能直接用作当时可交易的回测信号。
- `macro_recovery.status=REPAIRED` 仅表示登记的可回溯宏观及日线字段补齐，不表示原报告完整，也不表示所有来源可回溯。缺失日线也继续留在补缺队列中。

## 运维

持久队列和原始快照：`~/market_dashboard/state/macro_recovery.sqlite3`，权限 600。所有入口共用文件锁，避免手动补缺与定时任务重叠写入。

只补指定已完成交易日，不运行实时引擎、不发邮件：

```bash
/usr/bin/python3 /home/winters_dong426/market_dashboard/auto_review.py \
  --repair-only --repair-date 2026-10-21 --no-email
```

上游未恢复时保留 PENDING，后续既有定时运行自动重试。不增加定时任务，不更改其他程序运行时间，不接触 IB Gateway 或交易执行。

## 验证边界

新增测试覆盖 HTML 维护响应、有限重试、NULL/零区分、非破坏合并、写库失败 outbox 重试、日期截断、事后评分隔离、真实入口的部分采集与禁用 AI 分支。另与原云端评分代码比较 120 组确定性输入，确认评分和覆盖率不变。

真实上游恢复及下一轮自然 cron 的表现须与单元测试、人工补缺结果分别记录；不能把模拟恢复测试说成 FRED 已恢复。
