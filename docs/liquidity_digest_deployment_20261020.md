# 09:20 盘前汇总日报云端部署记录

## 发布范围

- 主机：`34.146.104.219`，用户：`winters_dong426`。
- 版本：`liquidity_digest_v1.2-shadow`。
- 宏微观规则版本：`macro_micro_direction_v1.0-shadow`。
- 发布目录：`/home/winters_dong426/liquidity_digest_app/releases/20261020T125336Z-v1_2`。
- 当前入口：`/home/winters_dong426/liquidity_digest_app/current`，符号链接到上述目录。
- 独立环境：`/home/winters_dong426/liquidity_digest_venv`。
- 状态与真实报告：`/home/winters_dong426/liquidity_digest_state`。
- 私有配置：`/home/winters_dong426/.config/liquidity_digest/config.json`，权限0600。
- 发布备份：`/home/winters_dong426/deploy_backups/liquidity_digest_20261020T125336Z-v1_2`。

原有报告、17条有效cron任务和生产数据库均保留。本次仅增加独立日报，
Supabase只读，不需要数据库迁移。未执行Git提交、推送或创建tag。
云端发布归档及配置备份不等于异地灾备，不应将密钥或真实输入归档推送公开GitHub。

## 发布完整性

发布包SHA256：

```text
21208964fe1c5f3f0c9b6d49b1ec5a5dada358d10e4981eea4b417d7546d289c
```

23个发布文件均已按`release_manifest.json`验证。
三份纯依赖在新发布内独立保存，未覆盖原`market_dashboard`的同名模块。
`requirements.freeze.txt`保存在云端发布备份目录中。
本地运行手册在打包后补充部署说明，其文本不属于此次归档的相同哈希承诺。

原crontab SHA256：

```text
eeb58f9f4046eea24b9adf5a162c5342d1da1ff96cd785468d5df073aed21db6
```

## 调度

所有时间按`America/New_York`，随夏令时切换：

| 阶段 | 时间 | 作用 |
|---|---|---|
| collect | 09:05、09:10 | 两次获取真实数据，保存实际接收时间 |
| build | 09:15 | 截止采集，冻结当日JSON、中文邮件及两页PDF |
| send | 09:20 | 发送冻结报告，记录SMTP回执 |

systemd每周一至周五触发，程序跳过NYSE休市日。
不启用错过时点后的补发，09:25起禁止当日发送。
每日唯一回执防重复；`PENDING`或`UNCERTAIN`不自动重发。
机器必须在上述时间在线且时钟同步。

## 验收

- 本地回归：183项通过，1条原有Matplotlib废弃API警告。
- 云端独立Python 3.11环境：106项日报测试通过，编译通过。
- FRED、纽约联储、财政部DTS与六张Supabase表读取通过。
- SMTP TLS登录及NOOP检查通过；预检未发送邮件。
- 中文字体安装完成，模拟报告两页已逐页渲染检查。
- 09:05自然采集：09:05:00开始，09:05:30完成，退出码0，20项成功。
- 09:10自然采集：09:10:00开始，09:10:12完成，退出码0，20项成功。
- 09:15自然构建：09:15:00开始，09:15:06完成，退出码0。
- 正式报告：`demo=false`，两页PDF，334430字节，四个文件的冻结清单验证通过。
- 正式PDF下载后逐页渲染检查，中文图表、宏微观分层及跨资产压力板块显示正常。
- 09:20自然发送：09:20:00开始，09:20:07完成，退出码0，回执`SENT / smtp_accepted`。
- 09:21复核：三个timer均为`enabled / active`，下一次发送为2026-10-21 09:20 ET。
- 原crontab哈希与三份原生产共享模块哈希仍与部署前相同。
- 最终收件箱投递未独立验证，需收件人确认；未额外发送测试或重复邮件。

自然运行证据保存在上述云端备份目录的`runtime_check.json`、
`build_check.json`、`live_quality_diagnostic.json`、`final_acceptance.json`及systemd journal。

## 真实数据限制

当日报告日期为2026-10-20，市场截面为2026-10-19。
采集成功不等于观测日期已更新：

- 日终TGA、IG/HY/CCC信用利差、2年期国债和10年期实际收益率，
  本次抓取最新观测仍为2026-10-16，报告标为`LAGGED`。
- 融资利差最新观测已到2026-10-19，但5日及21日窗口均缺2026-10-12。
  当前窗口按NYSE交易日检查；融资序列使用不同业务日历。
  这属于已确认的日历口径限制，不是SSH、定时器或API连接失败。
  当前版本暂停这些窗口的方向判断，不插值或补零。
- 宏观方向因此显示“证据不足”；这不代表资金宽松，也不代表全部采集失败。
  微观短期为“内部分化”，部分中期历史仍不足。

纽约联储官方说明确认参考利率遵循各自的发布日历，2026-10-12为其假日：
[参考利率说明](https://www.newyorkfed.org/markets/reference-rates/additional-information-about-reference-rates)、
[假日日历](https://www.newyorkfed.org/aboutthefed/holiday_schedule.html)。
后续应分别引入融资业务日历，并测试业务休市、真实漏数与数据发布滞后；
不能简单删除缺失日或用前值填充。本次09:15-09:25冻结发送窗口内不修改评分规则。

## 查看与停用

```bash
cd /home/winters_dong426/liquidity_digest_app/current
/home/winters_dong426/liquidity_digest_venv/bin/python -m liquidity_digest.job status \
  --state-dir /home/winters_dong426/liquidity_digest_state
systemctl list-timers 'liquidity-digest-*' --all --no-pager
journalctl -u liquidity-digest@send.service --since today --no-pager
```

`SENT`仅表示SMTP服务器接受，最终收件箱送达仍须收件人确认。
查看journal或分享诊断时不要输出私有配置。

仅停止新增日报，不影响原报告：

```bash
sudo systemctl disable --now liquidity-digest-collect.timer liquidity-digest-build.timer liquidity-digest-send.timer
```

保留状态库、当日报告、回执和发布备份，避免丢失审计记录或误触发重复邮件。
