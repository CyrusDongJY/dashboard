# 资金观察与日报 v1.7 云端部署验收

最终核验：2026-10-26 02:37:54 EDT。主机 `cyrus-dong`，IP `34.180.97.66`。

## 发布身份

- 功能提交：`0468a6e1bf4f1bbd7d35b281753a208858fa8b2d`。
- GitHub分支：`CyrusDongJY/dashboard` / `feature/temporal-state-shadow`。
- 日报版本：`liquidity_digest_v1.7-shadow`。
- 环境指数：`env_v2.5-fixed`；时间状态：`temporal_state_v1.1-comparable-shadow`。
- 发布目录：`/home/winters_dong426/liquidity_digest_app/releases/20261026-v1_7-capital-observations`。
- 原子切换入口：`/home/winters_dong426/liquidity_digest_app/current`。
- 旧版 `20261023-v1_6-overview` 保留，未删除。

发布包从功能提交的白名单生成，共61个文件；不含真实配置、账户信息、抓取档案或工作区其他线程的修改。

```text
SHA-256 bff8fea19f61787d65a6b06a286a66ed48fd049b4505f318e3b1b87a22114ce3
```

## 实际修改范围

日报全包升级；盘后目录 `market_dashboard` 仅更新以下6个模块：
`environment_indices.py`、`environment_report.py`、`risk_capital_ladder.py`、
`risk_event_pulse.py`、`temporal_monitor.py`、`temporal_report.py`。

用户执行了新增 `environment_daily.details` 的迁移，随后REST读取返回HTTP200。
仅日报专用虚拟环境新增 `lxml 6.1.3`；未改系统Python依赖、其他策略环境或cron。
跨资产战术压力评分公式、阈值和调用路径未改；原核心资金与水位仪公式未改。

检查发现云端 `anomaly_engine.py` 含另外一批VX标准月期限结构校验，
与本分支基础文件不同。此文件保持原样，未被发布包中的测试依赖覆盖。
已用云端实际异常引擎、实际共享依赖和 `/usr/bin/python3` 对新环境模块做真实数据库只读试算，
结果可序列化，核心风险/信用/广度覆盖均为1；期权与资金代理不足时保持缺失。
试算日期为2026-10-23，`persist=False`，没有将试算覆盖到历史评分。

因此本提交及tag标识本轮发布范围，不代表云端全部独立程序与该分支逐字一致。
云端依赖路径和哈希已保存于部署备份的 `producer_dependency_acceptance.json`。

## 验收结果

| 项目 | 结果 |
|---|---|
| 本地功能测试 | 主测试637通过、2跳过；隔离旧格式测试5通过 |
| 新升级保护测试 | 4通过；合计646通过、2个可选研究测试跳过 |
| 从Git提交生成的隔离发布包 | 本地358通过、1跳过 |
| 云端日报专用Python | 同一发布包358通过、1跳过 |
| 跳过说明 | 发布包不含独立核心资金模块，仅跳过可选版本交叉测试 |
| 云端盘后 `/usr/bin/python3` | 4组相关unittest共64项通过；未安装全局pytest |
| 编译/导入/哈希 | 61个发布文件及6个生产更新文件通过 |
| 数据库迁移 | `environment_daily` 新列REST读取HTTP200 |
| 新模型真实输入兼容性 | 实际生产依赖下只读计算通过，不回写 |
| 模拟报告 | 云端5页PDF逐页检查通过；网页5张内嵌PNG |
| 历史报告/发送回执 | 23个文件、4条回执完全不变 |
| 其他生产模块 | 40个文件哈希不变 |
| cron/systemd/私有配置 | 完全不变；三个timer均active、enabled |
| SMTP | TLS登录与NOOP通过；没有发送测试邮件 |

系统Python存在原有Axes3D混装警告；二维图表测试及PDF生成通过。没有为了该警告重装系统Matplotlib。

## 真实数据源

在隔离备份状态目录执行GET-only采集，不使用生产状态库，不伪造09:15采集时间。
25项采集任务中24项成功：原有资金、融资、信用和数据库表读取通过；
CFTC取得1200条类别观测，最新为2026-10-20；FINRA取得12个月，最新为2026-09。

ICI首次综合核验记为FETCH_ERROR，随后单源请求确认HTTP403，仍未接通。
保留不可用状态，不绕过访问限制，不填零。单只ETF份额/NAV/公司行动仍为NOT_VERIFIED。
慢频15/65自然日观察窗不是精确官方发布日历，当前历史是本次可见修订版，不属于历史PIT。

## 自然运行边界

- 下一次采集：2026-10-26 09:05、09:10 ET。
- 下一次冻结：09:15 ET；发送：09:20 ET，即当日北京时间21:20。
- 以上是已核实的计划，不是新版自然运行或收件箱投递证明。
- 新环境版本从下一次盘后生产任务开始落库；此次没有回填、重算旧日报或重复发信。
- 变更版本/成员后趋势比较重新积累，不能为连接曲线而拼接不同口径。
- 发布不等于完成预测有效性/走步回测，也不表示所有旧采集器已统一重构。

## 备份与回退

备份目录：`/home/winters_dong426/deploy_backups/liquidity_digest_20261026-v1_7-capital-observations`。

包括旧6个模块、发布包、SQLite一致性备份、原cron/systemd、依赖清单、两种解释器测试日志、
只读源验收、依赖兼容性验收及最终 `final_acceptance.json`。目录仅部署账户可访问。
回退时应避开采集/冻结/发送和盘后任务：恢复旧6个模块并将 `current` 原子指回旧发布。
不得回退生产状态库、清除发送回执或重建已发送报告。新增JSON列可保留，无需破坏性回滚。
