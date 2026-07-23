# Market Dashboard

美股盘前、盘中、盘后数据监测与异常预警系统。系统把数据采集、客观异常判定、
影子环境评估、邮件报告和运行质量审计分开，避免把研究性指标直接当成交易信号。

## 版本地图

| 版本 / 分支 | 提交 | 定位 | 状态 |
| --- | --- | --- | --- |
| `v9.0.0` | `da5f93d` | 全局异常哨兵基线 | 历史稳定点 |
| `v9.1.0` | `08df042` | 环境指数、历史曲线和 PNG 邮件 | 影子监测里程碑 |
| `v9.2.0-rc1` | `c39739f` | 流动性水位仪、盘前期权数据契约 | 已合并 `main` 的候选版 |
| `v9.2.0-rc2` | `c6b1402` | 数据契约、缺失值和来源追溯加固 | 推荐下一部署候选 |
| `feature/capital-flow-monitor` | `2e9f05d` | ETF 发行人数据源研究与探针 | 研究分支，不得部署 |

Tag 只标识不可移动的代码恢复点，不表示云端已经部署。根据现有日志证据，
云端在 2026-07-15 运行的是 V9.1；V9.2 必须完成迁移、部署和连续交易日验收后，
才能创建正式 `v9.2.0`。

## 分支纪律

- `main`：只接受经过审查、测试并准备部署的代码。
- `feature/data-contract-hardening`：V9.2 数据契约加固，目标是合并回 `main`。
- `feature/capital-flow-monitor`：ETF 资金行为数据源验证，当前仅研究和采购决策。
- `feature/env-dashboard-wip`：历史开发分支，核心内容已合并，不再承载新功能。
- 正式版本使用 `vX.Y.Z`；未完成云端验收使用 `vX.Y.Z-rcN`。
- 已推送 Tag 不移动、不复用；修复通过新提交和新版本号发布。

## 运行链路

```text
08:45 daily_pre_market.py
   -> 盘前现货、期权结构、Gamma 分期限数据
08:55 market_sentinel.py
   -> PRE 快照和红线审查，仅越线才发邮件

09:45-16:00 ib_intraday_sniper.py
   -> 盘中战术监测和异常提醒

16:02 daily_post_close.py
   -> 盘后现货与期权数据
16:30 auto_review.py
   -> 两年宏观/市场历史刷新和结构化日报
16:45 auto_analyst.py
   -> EOD 最终快照、AI 归因、环境图和流动性图
16:55 market_sentinel.py
   -> POST 全量红线审查，仅越线才发邮件
```

## 模块边界

| 层级 | 主要文件 | 职责 |
| --- | --- | --- |
| 数据采集 | `daily_pre_market.py`, `daily_post_close.py`, `auto_review.py` | 抓取并结构化写库 |
| 盘中监测 | `ib_intraday_sniper.py` | 盘中战术指标和上下文 |
| 数据契约 | `data_contracts.py`, `pre_market_metrics.py`, `tactical_stress.py` | 缺失值、单位、口径、来源追溯与盘后压力门控 |
| 异常判定 | `anomaly_engine.py`, `market_sentinel.py` | 单指标、多窗口和跨资产共振 |
| 环境观察 | `environment_indices.py`, `environment_report.py` | 长周期环境指数和 PNG |
| 流动性观察 | `liquidity_sources.py`, `liquidity_monitor.py`, `liquidity_report.py` | 六柱流动性水位和 PNG |
| 盘后归因 | `auto_analyst.py`, `market_probes.py` | 邮件汇总和 AI 叙述 |
| 运维与审计 | `market_utils.py`, `migrations.sql`, `tests/` | 幂等写入、质量日志、迁移和测试 |

环境指数与流动性水位仪均为 `shadow_mode=true`，只用于观察、曲线积累和回测，
不接入 `ALERT_GATE`，不驱动仓位。正式预警唯一入口是
`market_sentinel.py` 的 `ALERT_GATE`。

## 数据库主表

| 表 | 用途 |
| --- | --- |
| `stock_options_pre_market` | 盘前期权和 Gamma 结构 |
| `stock_spot_post_close` | 盘后现货、POC、OBV、IVR |
| `metric_daily` | PRE / POST / EOD 全指标每日快照 |
| `anomaly_events` | 结构化异常事件 |
| `sentinel_runs` | 哨兵运行、去重指纹和发信结果 |
| `environment_daily` | 环境指数影子历史 |
| `liquidity_daily` | 流动性六柱影子历史 |
| `data_quality` | 任务状态、缺失、滞后和写入行数 |

数据库升级统一执行 `migrations.sql`。脚本为幂等设计，但生产执行前仍应备份并在
Supabase SQL Editor 中检查结果。

## 云端目录

- `~/market_dashboard/`：共享模块、采集脚本、哨兵、迁移和测试。
- `~/quant_bot/auto_analyst.py`：盘后归因入口，从 `~/market_dashboard/` 导入共享模块。
- `~/TradingRadar/ib_intraday_sniper.py`：盘中监测入口。
- `market_config.py`：只保留在云端，不进入 Git。

详细部署顺序、迁移步骤和验收 SQL 见 `UPGRADE_README.md`；历史审查结论见
`docs/audits/`。

## 发布流程

1. 从候选分支运行完整测试，确认工作区干净。
2. 合并到 `main`，不要把研究分支直接部署到生产。
3. 在 Supabase 执行对应迁移，再按目录部署代码。
4. 验证导入、PNG、dry-run、cron、数据库质量台账和邮件附件。
5. 连续观察至少 3 至 5 个完整交易日。
6. 验收通过后，在 `main` 的精确提交创建带注释正式 Tag。

常用验证：

```bash
python3 -m unittest discover -s tests -v
git status --short --branch
git log --graph --decorate --oneline --all -20
git tag --list --sort=-version:refname
```
