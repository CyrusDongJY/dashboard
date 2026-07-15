# 环境评估仪表盘 — 交接与验收记录

分支：`feature/env-dashboard-wip` ｜ main 保持不动

本文件记录「环境评估仪表盘（曲线化 / 复合环境指数）」这一轮的
交接清单、返工内容与第三方验收结论，作为该功能进入影子运行前的验收凭证。

---

## 一、背景与定位

系统在 V9.0（异常矩阵 + 全局哨兵）之上，新增「环境评估层」：

- 每日无条件快照全部指标的派生向量（`metric_daily`），用于画长周期曲线；
- 聚合成 5 个复合环境指数 + 状态判定（`environment_daily`）；
- 盘后邮件附环境趋势 PNG。

**核心定位：观察层，不是风险评估器。** 复合指数处于影子模式
（`SHADOW_MODE=True`），只展示、不预警、不进 `ALERT_GATE`、不驱动仓位。
阈值须经走步回测校准后，才谈「可信环境评估」。

---

## 二、返工前的 9 项问题（codex 审查提出）

| # | 级别 | 问题 |
|---|------|------|
| 1 | 严重 | 数据不足时错误显示「健康」；单指标即可判「脆弱」 |
| 2 | 严重 | 复合指数缺统计有效性（credit 双计权、dpsv 双面板、非平稳水平值取分位、不看样本/滞后、阈值未回测） |
| 3 | 严重 | 历史回补失真（`period_days` 未生效、周频 ffill 成日频虚增样本、填充日贴当日 source_date、可覆盖生产快照） |
| 4 | 严重 | 期权/资金面板名不副实（ZGL/Gamma/Vanna/Wall/DIX/GEX 未入 metric_daily） |
| 5 | 严重 | 共振事件唯一键不含 `resonance_key`，同日多主题冲突 |
| 6 | 高 | 新鲜度非指标级；metric_daily 无 session，盘前/盘后互相覆盖 |
| 7 | 高 | DPSV 被过度解释为「机构吸筹」；且「FINRA 不可回补」判断错误 |
| 8 | 高 | PNG 邮件交付未完成 |
| 9 | 运维 | 改动在非 git 目录，与 GitHub 仓库分叉 |

---

## 三、返工内容（commit `4bd3b88`）

- 覆盖不足优先返回「数据不足」；核心维度覆盖门槛硬性参与状态判定。
- 指数按有效样本数 / 滞后 / 覆盖度加权；影子模式，`severity=0`，不接入 `ALERT_GATE`。
- credit_z 双计权删除；dpsv 不再同时进两个面板；非平稳水平值仅画曲线不评分。
- FRED 低频只用原生观测算 z/分位；`is_filled` 标记填充日，`effective_obs_count`
  不计填充日；`period_days` 经 `observation_start` 真正限制查询；保护 `is_final` 不被覆盖。
- 共振唯一键加入 `resonance_key`；旧数据 UPDATE 兜底。
- metric_daily 加 `session`（PRE/POST/EOD），唯一键含 session，盘前盘后隔离。
- REGISTRY 新增 DIX/GEX/ZGL/Call·Put Wall/Vanna/Charm/Gamma/ExpectedMove，连续入库。
- DPSV/DIX 降为中性代理（`bad_dir:0` + `abs:None`），不加减危险分；probes 改为「方向需价格验证」。
- 盘后邮件挂载环境趋势 PNG（`matplotlib` Agg + savefig），失败自动降级纯文本。
- 更新 `UPGRADE_README.md` / `migrations.sql` / `tests/test_environment_dashboard.py`。

---

## 四、第三方验收结论（2026-07-15）

逐条对照代码核实，9 项全部真实落地（非纸面声明）：

| # | 判定 | 关键证据 |
|---|------|----------|
| 1 | ✅ | `classify_state` 覆盖率低于门槛优先返回「数据不足」；`bd is None` 不再判脆弱 |
| 2 | ✅ | `_quality_weight` 用 `effective_obs_count`+`0.85^lag`；`SHADOW_MODE=True`、复合 `severity=0` |
| 3 | ✅ | `is_filled=source_day!=day`、`effective_obs_count=len(native)`、z/分位仅用 native、保护 `is_final` |
| 4 | ✅ | REGISTRY 新增期权/资金指标，引擎写入 metric_daily |
| 5 | ✅ | 唯一键 `(report_date,metric,scope,window_scope,layer,resonance_key)` |
| 6 | ✅ | metric_daily `session` 列 + 唯一键含 session |
| 7 | ✅ | `dpsv_pct` `bad_dir:0`+`abs:None`（既不加 z 分也不触发绝对告警）；probes 措辞中性化 |
| 8 | ✅（见保留项） | `environment_report.py` 真 savefig；auto_analyst 挂 MIMEImage + 降级 |
| 9 | ✅ | 已在 `feature/env-dashboard-wip`，main 干净 |

**本机测试复现**：`python3 -m unittest` → **10 通过 + 1 跳过**（跳过项为
PNG 像素检查，本机未装 matplotlib）。codex 环境「11 全过」在装 matplotlib 后成立。

**保留项（诚实边界，非缺陷）**：

1. PNG 像素测试仅在装 `matplotlib` 的环境实跑；审查机只能确认代码路径正确。
2. 复合指数阈值（75/85/90 等）仍未回测——但已全部标为「观察」、影子运行、不预警，
   属「已知未校准且已隔离」，符合影子层定位。

**验收判定：可进入影子运行部署。** 不再冒充可信风险评估器，而是老实的观察层：
覆盖不足即说不足、代理变量不冒充因果、填充日不虚增样本、复合指数不碰预警。

---

## 五、部署顺序（生产未动）

1. Supabase SQL Editor 执行最新 `migrations.sql`（幂等）。
2. 安装 `requirements-env-dashboard.txt`。
3. 部署代码。
4. 先 `backfill_history.py 2y dry` 检查回补行数，确认无误再正式写库。
5. 影子运行满一个季度、期权/资金面板攒够历史后，用未来 5D/21D 回撤做走步回测，
   再校准阈值——到此才可从「观察层」升级为「可信环境评估」。
