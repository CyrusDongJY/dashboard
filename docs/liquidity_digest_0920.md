# 美股流动性与风险传导日报：09:20 盘前版

## 边界

这是独立新增的影子报告，不替代现有日报、不修改现有评分、不触发交易或原有预警。
Supabase 仅发起 GET；每日采集、冻结输入、报告和发送回执保存在单独的本地目录。
无需数据库迁移。不要改动旧 cron，也不要让新的 systemd 定时器与新 cron 重复运行。

报告是“一封摘要邮件 + 完整HTML网页附件 + 三页中文PDF”，同时归档JSON、邮件HTML/文本及校验清单。
v1.5在微观环境后增加市场内部结构，保留原两页内容；历史版本和已发送报告不重写：

1. 资金数量：同日净流动性代理、准备金、财政账户、逆回购。
2. 资金价格：SOFR-IORB、EFFR-IORB、SOFR第99百分位与中位利率差。
3. 信用传导：投资级、高收益、CCC及以下 OAS。
4. 贴现与外部：2年国债、10年实际收益率、DXY、美元兑日元5日变化。
5. 市场接受：20/50/200日广度、等权差、SPY/QQQ均价线接受率。
6. 独立展示盘后跨资产战术压力的数值、覆盖率、分项数据及贡献、时间变化。
7. 三组中文趋势图：数量和分解、融资和信用、核心板块时间状态。
8. 市场内部结构：涨跌分布、11行业绝对/相对表现、20日均线广度持续性与轮动迹象。

不新增混合总评分。未配置、源返回空、抓取失败、旧缓存、观测滞后分别记录，
不补零，不以个股事件替代市场整体状态，也不把净流动性余额当作流入股市的金额。

## 时间契约

所有时间均为 `America/New_York`，由时区数据库自动处理夏令时。

| 步骤 | 定时 | 作用 |
|---|---|---|
| collect | 09:05、09:10 | 两次只读采集，逐源保存实际接收时间 |
| build | 09:15 | 只读取09:15前已保存的输入；不再请求网络；冻结当期报告 |
| send | 09:20 | 只发送当日冻结结果；09:25以后禁止补发 |

这是目标发送时间，不是收件箱到达时间保证；SMTP和邮件服务可能延迟。
NYSE休市日三个任务均正常跳过，半日市仍生成盘前报告。
报告的价格和市场结构基准是上一个NYSE交易日，今日尚未收盘的数据不得混入。
SOFR/EFFR等今晨公布的上一营业日数据可使用，但仍标注其实际观测日期。
周频数据保留原始频率，周变化严格找7/28日前观测，日频变化严格找1/5/21个NYSE交易日前观测。
不同市场假日导致对应日缺失时保持缺失，不用“上一条”替换。

09:20不承诺所有数据源都已发布：例如信用数据可能仍为更早日期，周/月频也不日更。
本期邮件不等待到下午，不用晚到的数据偷偷改写已冻结报告，也不自动重复发日报。
当前适配器只在两个盘前窗口抓取；未增加下午补抓任务。

## v1.1：先看状态，再看数据

首页及邮件顶部新增：

- 资金条件仪表：趋于收紧 / 趋于放松 / 变化不明显 / 方向待确认。
- 跨资产压力仪表：压力增加 / 压力减轻 / 变化不明显 / 方向待确认。
- 压力水位与近5日方向的九宫格：区分高压缓解、高压加剧、低位升压等情况。
- 高压、单日突变、资金与压力背离、环境与战术分歧、缺数提示。
- 原18项数据压缩为双列表格，跨资产战术分项保留；完整变化窗口仍归档在JSON。

状态来自已落库的`temporal_state_daily`，不利用今天的新数据重新计算昨天的评分。
资金条件复用`liquidity`面板；压力优先取`environment`，不可用才采用明确标注的
`tactical_stress`代理。环境与战术指标有重叠输入，不能相加或当成两份独立确认。
它们不一致时显示分歧，不平均抵消。风险资本方向另列，不把个股上涨当作系统资金充裕。

复用`temporal_monitor.py`已有参数：压力方向值≤35为低区、≥65为高区，
5日变化达到±5点才标记方向，单日变化达到±10点提示突变。这些是既有影子规则，
并非经回测验证的交易阈值或官方风险评级。新增的是展示层，没有新总分或新邮件触发门槛。
资金仪表的观察值为`100 - risk_value`，越高表示支持越强；
仪表下方的变化统一使用压力方向值，正值为恶化，负值为缓解。

方向判断要求同一来源评分版本、同一时间状态版本、完整的实际NYSE交易日窗口，
并校验LIVE_SHADOW、OK质量、有效覆盖、置信度、当前观测日期及今晨抓取状态。
历史不足、版本断点、源过期或刷新失败时，方向为未知，不归入平稳。
低水位与改善方向分开显示：高压缓解仍保留高压提示，未知水位不放指针。
图中的连续天数是连续观测交易日，不是重复采集次数。

“资金条件”“压力”是昨日收盘时系统的观察状态，不是今天09:15已经完成的盘中评价；
今晨SOFR/EFFR等更新另列。若新取得、日期匹配的SOFR-IORB、EFFR-IORB或SOFR尾部利差
超过既有`liquidity_monitor.FUNDING_STRESS_GUARDS`绝对观察线，新增“晨间观察”提示，
但不据此重写昨日状态；单项越线不代表系统性收紧，也不绕过原系统的历史分位确认条件。
官方口径也应区分资金量和融资成本：
[纽约联储SOFR定义](https://www.newyorkfed.org/markets/reference-rates/sofr)
将其定义为以美国国债抵押的隔夜现金借贷成本，不是风险资产资金流入量。

v1.1归档使用`liquidity_digest_v1.1-shadow`，展示规则为`digest_state_v1.0-shadow`。
不要在09:15冻结后、09:25发送窗口结束前更新程序；
版本不匹配会拒绝发送，而不是改写已冻结报告。旧PDF、JSON归档保留不动。
部署时需一并带上现有纯计算模块`temporal_monitor.py`、`liquidity_monitor.py`，
展示层直接引用它们的参数，未复制或更改其阈值。

## v1.2：宏观、微观与综合确认分开

该阶段版本为`liquidity_digest_v1.2-shadow`。宏微观方向规则为
`macro_micro_direction_v1.0-shadow`，旧综合状态展示名称更新为`digest_state_v1.1-shadow`。
不修改原生产评分、数据库、原邮件或定时器；只在新日报中新增分类与方向观察。

此前两块不能直接视作纯宏观：
`liquidity_monitor`总分含市场分配和尾部韧性，
`environment_indices`含波动、信用、广度、期权和行为代理。
战术压力则混合VIX、MOVE、信用、TRIN、PCR、CMF，不是纯微观指标。
因此不拆分旧总分，也不把它们重新加权成两个新总分。

| 层次 | 主要证据 | 输出 |
|---|---|---|
| 宏观：资金与金融条件 | 数量与缓冲、融资价格、信用传导 | 边际趋松/趋紧、局部变化、分化或证据不足 |
| 微观：市场内部承接 | 广度、SPY/QQQ均价线接受率、风险资本扩散 | 承接扩散改善/内部压力增加、局部变化、分化或证据不足 |
| 跨资产综合确认 | 原综合流动性、环境压力、战术压力 | 保留原数值、水位、方向与高压提示；不向前两层重复投票 |

“宏观”不代表经济增长预测；“微观”指股票市场内部，不是买卖价差、
订单簿深度或真实成交冲击成本。不能用本报告声称已测得交易执行流动性。
`metric_daily.scope=MACRO`是原数据库的市场级命名，不代表本报告的经济分类，
新增广度读取仍使用既有MACRO/EOD/final数据契约，无需迁移。

### 分组及时间窗口

- 数量组：同日净流动性代理、周均准备金，分别在各自原生周日期上比较1周/4周。
  TGA、逆回购已参与数量代理分解，不再重复作为额外支持票。
- 融资组：SOFR-IORB、EFFR-IORB、SOFR尾部利差，比较5/21个NYSE交易日。
- 信用组：投资级、高收益利差，比较5/21日。CCC为补充背景，不以其常见发布滞后
  替代或污染主信用证据。
- 广度组：20/50/200日均线上方股票占比，比较5/21日。
- 承接组：SPY和QQQ均价线上方时间占比，比较5/21日。
- 扩散组：既有合格风险资本时间状态，比较5/21日压力方向值。
- 利率、美元、日元、等权差和个股异动为背景。它们方向依赖情境，
  不机械设置“涨=收紧”或“跌=放松”的单调关系。

各组要求指定分量全部有效，同组多个相关变量只构成一组方向证据。
组内有相反方向即分化；任一必要分量缺失则该组待确认。
整体方向要求3组完整、至少2组同向且无反向；仅1组变化标为“局部”；
存在反向不做多数表决；缺一组不外推成整个宏观或微观结论。
短、中窗口分别计算，不能把短期改善视为中期反转。
这些是端点净变化观察，不代表每天同向，也不代表统计独立的确认或因果关系。
宏观周频与日频时间不完全一致，逐项保留比较起止日；不把宏微观背离当作严格同频统计结果。

### 新方向观察线

以下是**新增、未经过走步回测的展示降噪参数**，不是原模型阈值、官方标准或交易信号。
变化先转为压力方向，达到观察线才标记上/下，其余为“变化不明显”，不是低风险。
不得把本次新增分类表述为“只改标题”或“所有规则完全沿用原版”。

| 分量 | 近期观察线 | 中期观察线 | 数值上升含义 |
|---|---:|---:|---|
| 净流动性代理、准备金 | 各5十亿美元/1周 | 各10十亿美元/4周 | 数量支持改善 |
| SOFR-IORB、EFFR-IORB | 各1基点/5日 | 各2基点/21日 | 融资压力增加 |
| SOFR尾部利差 | 2基点/5日 | 4基点/21日 | 尾部压力增加 |
| 投资级信用利差 | 5基点/5日 | 10基点/21日 | 信用压力增加 |
| 高收益信用利差 | 10基点/5日 | 20基点/21日 | 信用压力增加 |
| 20/50日广度 | 各3个百分点/5日 | 各6个百分点/21日 | 参与改善 |
| 200日广度 | 2个百分点/5日 | 4个百分点/21日 | 参与改善 |
| SPY/QQQ均价线时间接受率 | 各5个百分点/5日 | 各10个百分点/21日 | 承接改善 |
| 风险资本压力方向值 | 5点/5日 | 5点/21日 | 扩散转弱；复用既有压力变化线 |

所有历史窗口要求完整的原生观测日，同源版本断点、刷新失败、越界或缺数均留空，
不以较早一行或填充数值补齐。新读入20/50日广度，其余复用既有只读输入。
规则参数、各分量变化、实际起止日和版本随当日报告JSON一起冻结。

### 水位、趋势与分歧

宏观和微观不生成未经校准的0-100总水位。当前水位风险另用可核查提示：
宏观引用既有融资/信用绝对观察线；微观列出200日线上股票不足半数、
SPY/QQQ均价线上时间不足半数、风险资本扩散偏弱等事实。
“低于半数”是描述性事实，不是经回测的危机阈值。
无所列提示不等于宽松。综合压力九宫格继续显示原有35/65水位。
因此宏观“边际趋松”可与融资仍越线、微观转弱、综合压力仍高同时出现。
报告明确提示两层是否同向，不相互抵消，更不把同向直接作为交易许可。

## 数据与追溯

官方接口复用现有 `liquidity_sources.OfficialLiquiditySources` 的原始抓取方法，
不使用其中可能混合日期的前向填充派生值。

- FRED：WALCL、WDTGAL、WRESBAL、RRPONTSYD、IORB、BAMLC0A0CM、
  BAMLH0A0HYM2、BAMLH0A3HYC、DGS2、DFII10。
- 纽约联储：SOFR、SOFR第99百分位、EFFR。
- 美国财政部：DTS日终TGA。
- Supabase：metric_daily、market_history、temporal_state_daily、
  risk_capital_daily、risk_event_pulse_daily、daily_review。
- 市场指标只使用MACRO/EOD/final、未填充、source_date与report_date相符的记录；
  图表不拼接不同规则版本，temporal图排除REPLAY和非OK质量记录。

FRED历史值是“本次抓取时可见的修订版本”，不是ALFRED首次发布历史，
不能据此声称历史无前视回测。本程序开始积累后的每日实际抓取档案可追溯当时输入。
财务数量计算严格同日：WALCL - WDTGAL - 当日RRP。WRESBAL是周均准备金，单独展示。
单位均明确：数量十亿美元，信用/融资利差基点，收益率百分比。

运行目录：

```text
liquidity_digest_state/
  captures.sqlite3          # 追加采集记录、失败尝试、发送回执
  reports/YYYY-MM-DD/
    report.json             # 指标历史、分项、来源、观测日、抓取时间
    report.pdf              # 三页中文报告（v1.5起）
    report.html             # 完整离线网页，三组PNG图表内嵌
    email.txt
    email.html
    manifest.json           # 文件SHA256，发送前必须匹配
  demo/YYYY-MM-DD/           # 模拟样例，与正式报告隔离
```

示例与真实数据不能混合，demo报告被发送入口拒绝。
SQLite需要连同reports目录一起备份，不能只备份代码。
主机磁盘丢失会丢失本地归档，建议纳入原有云盘快照或私有存储备份；
真实输入档案、密钥、邮件地址不要提交公开GitHub。

## 本地验收

推荐Python 3.11或3.12及独立虚拟环境，不对系统Python强制重装库。

```bash
python3.11 -m venv /tmp/liquidity_digest_check
/tmp/liquidity_digest_check/bin/pip install -r requirements-liquidity-digest.txt pytest pypdf
/tmp/liquidity_digest_check/bin/python -m pytest tests/test_liquidity_digest.py -q
/tmp/liquidity_digest_check/bin/python -m liquidity_digest.job demo \
  --date 2026-10-20 --state-dir /tmp/liquidity_digest_preview
```

demo无需密钥和网络，不发送邮件。日期必须是NYSE交易日。
中文图表与PDF使用同一字体，并将字体嵌入PDF，收件端不需要安装中文语言包。
Linux可安装`fonts-wqy-zenhei`；也可用环境变量`DIGEST_FONT_PATH`指定中文TrueType字体文件。
仅CFF轮廓的OTF字体不适用于ReportLab正文嵌入，不能只依赖原来图表使用的OTF字体。
字体缺失会使build失败，不发送缺图报告。

## 云端安装

生产使用独立目录`/home/winters_dong426/liquidity_digest_app/current`，
指向`releases/<release_id>`；不覆盖`market_dashboard`中的原程序或共享模块。
发布包包含新package、依赖文件、部署模板、三份纯依赖
`liquidity_sources.py`、`liquidity_monitor.py`、`temporal_monitor.py`及日报测试。
每份发布包都需要文件SHA256清单与整体归档SHA256。不要上传本地凭据。

`deploy/liquidity-digest/install_release.py`在云端以原用户运行，参数为
`--archive <发布包> --sha256 <已核实哈希> --release <唯一发布名>`。
它验证清单、备份旧cron与新日报unit、安装独立venv与字体、运行118项日报测试，
然后安装unit但不启用timer。配置只在服务器内部从既有`market_config.py`
所需字面量字段复制，不执行配置脚本、不输出密钥，不覆盖已存在的新日报私有配置。
接口和SMTP只登录不发信验收通过后，才能执行下方timer启用步骤。
该安装器不应在09:15-09:25冻结与发送窗口内使用。
升级验收的模拟报告生成在当次备份目录中，不与既有日期的模拟或正式归档碰撞。

以下为独立运行环境和已有发布目录的维护命令，不是上传整个旧项目：

```bash
cd /home/winters_dong426/liquidity_digest_app/current
python3 -m venv /home/winters_dong426/liquidity_digest_venv
/home/winters_dong426/liquidity_digest_venv/bin/pip install -r requirements-liquidity-digest.txt
sudo apt-get install fonts-wqy-zenhei
install -d -m 700 /home/winters_dong426/liquidity_digest_state
install -d -m 700 /home/winters_dong426/.config/liquidity_digest
```

将`deploy/liquidity-digest/config.example.json`配置到
`/home/winters_dong426/.config/liquidity_digest/config.json`，
填入现有FRED、Supabase只读权限凭据和邮件应用密码，权限必须0600且属当前用户。
程序不会读取旧Python配置脚本，不执行旧报告。只读Supabase key需有上述表的SELECT权限。
示例中的REPLACE是假值，不能直接用于正式运行。接收人目前只支持一个纯邮箱地址。

```bash
chmod 600 /home/winters_dong426/.config/liquidity_digest/config.json
/home/winters_dong426/liquidity_digest_venv/bin/python -m liquidity_digest.job demo \
  --state-dir /home/winters_dong426/liquidity_digest_state
```

确认中文预览正常后，校验日历表达式并安装独立定时器：

```bash
systemd-analyze calendar 'Mon..Fri *-*-* 09:20:00 America/New_York'
sudo install -m 644 deploy/liquidity-digest/liquidity-digest@.service /etc/systemd/system/
sudo install -m 644 deploy/liquidity-digest/liquidity-digest-collect.timer /etc/systemd/system/
sudo install -m 644 deploy/liquidity-digest/liquidity-digest-build.timer /etc/systemd/system/
sudo install -m 644 deploy/liquidity-digest/liquidity-digest-send.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now liquidity-digest-collect.timer liquidity-digest-build.timer liquidity-digest-send.timer
systemctl list-timers 'liquidity-digest-*'
```

不要对`liquidity-digest@.service`模板本身enable，只有timer启动对应阶段。
主机需在09:05以前启动、09:25以后仍在线，NTP正常；不得同时有相同的新cron。
定时器不启用Persistent，机器下午恢复不会补发过期盘前信号。
采集总预算180秒，单请求超时不超过8秒，服务总超时240秒。

## 首日与每日验收

在首个正常交易日观察自然运行，不通过伪造日期绕过实盘窗口：

```bash
journalctl -u liquidity-digest@collect.service --since today --no-pager
journalctl -u liquidity-digest@build.service --since today --no-pager
journalctl -u liquidity-digest@send.service --since today --no-pager
/home/winters_dong426/liquidity_digest_venv/bin/python -m liquidity_digest.job status \
  --state-dir /home/winters_dong426/liquidity_digest_state
```

依次确认：逐源状态及实际观测日 -> 当日报告存在且哈希有效 -> SMTP回执 ->
实际收到邮件和PDF。`SENT`仅表示SMTP接受，不证明收件箱已送达。
没有数据也会生成明确“证据不足”的健康可见报告，避免静默失败；
PDF无法生成、当日冻结报告缺失或哈希错误则禁止发送。
`FETCH_ERROR`不记录含密钥的原始URL异常，按source_status定位后单独排查连接/权限/字段。

防重复策略：进程锁 + 每日期唯一回执，SMTP DATA前写PENDING，
服务器确认接受后立即写SENT，之后QUIT断开不将成功改判为失败。
PENDING或UNCERTAIN表示可能已经发出，自动重试只会跳过。
必须先检查收件箱和SMTP日志再人工处置回执；程序故意不提供一键强制重发。

停用仅禁用新timer，原系统不受影响：

```bash
sudo systemctl disable --now liquidity-digest-collect.timer liquidity-digest-build.timer liquidity-digest-send.timer
```

不要删除归档目录。新代码可移除，原生产表和原邮件任务从未改写。

## 本轮验收记录（2026-10-20）

- Python 3.12隔离环境：新增33项测试通过。
- 加入既有流动性、战术压力、时间状态、盘后复盘、风险资本与事件模块后，共110项通过。
- 既有`temporal_report.py`有1条Matplotlib待废弃API警告，本轮未改动该模块。
- 新package与测试文件编译检查通过。
- 正常/缺失两种报告均验证为两页，中文字体已嵌入；模拟预览逐页渲染检查通过。
- 纽约联储SOFR/EFFR和财政部DTS真实公开接口只读冒烟通过。
- FRED认证接口、Supabase真实表权限/数据、SMTP实发、Linux定时器及自然09:20运行尚未验收。
- 本轮未提交或推送GitHub，未连接云端安装任务，未向真实收件人发信。

## 状态展示优化验收（2026-10-20，v1.1）

- 新增37项状态展示测试；连同日报原33项及相关既有模块，共147项测试通过。
- 覆盖缺数、低覆盖、刷新失败、历史缺口、规则版本断点、压力代理回退、
  高压缓解、低位升压、单日突变、相互矛盾的方向及晨间融资观察线。
- 新package与测试文件编译检查通过；仍只有上述既有Matplotlib待废弃API警告。
- 模拟预览`output/pdf/liquidity_digest_states_v1_1.pdf`仍为两页，逐页渲染检查通过；
  多项提示同时出现时也保持两页。预览数值均为模拟样例，不代表当日真实市场。
- 原有报告、归档和发送安排未改动；本次仅完成本地验证，未部署云端或实际发信。

## 宏微观分层验收（2026-10-20，v1.2）

- 新增36项分类/窗口/方向测试，连同此前日报及相关模块共183项测试通过。
- 验证宏微观相反方向、同组不重复投票、缺一组即暂停整体判断、近期与中期背离、
  观察线边界、信用基点换算、周频原生日期、日历缺口、版本断点、数值越界及浮点容差。
- 新package与测试编译通过。仍只有既有Matplotlib待废弃API警告。
- CLI demo成功生成PDF、邮件正文及冻结JSON；仅模拟模式，不向真实邮箱发送。
- 分层预览保持两页，逐页渲染检查通过；旧预览文件未覆盖。
- 未修改原生产评分或发送安排，未执行数据库迁移、Git提交推送或云端部署。

## 云端部署验收（2026-10-20，v1.2）

上述本地阶段之后，已在`34.146.104.219`完成独立部署。
09:05/09:10采集、09:15冻结及09:20发送均已自然运行成功；
SMTP于09:20:07接受邮件，最终收件箱送达尚需用户确认。
旧cron及原共享模块保持不变，本次不需要数据库迁移。
详细证据、数据滞后与融资业务日历限制见
[部署记录](liquidity_digest_deployment_20261020.md)。

## v1.3：完整离线HTML附件（2026-10-20）

该阶段版本为`liquidity_digest_v1.3-shadow`，评分和宏微观方向规则不变。
每天邮件附加`liquidity_digest_YYYY-MM-DD.html`，原PDF附件继续保留。
邮件正文仍是摘要，新增的`report.html`不是把摘要另存为网页：

- 包含宏观/微观状态、跨资产九宫格、全部指标及各窗口变化、观测日和数据质量。
- 保留战术压力分项、三组中文趋势图、市场传导状态和缺口。
- 网页内有章节跳转及可展开的来源审计，包含实际观测与抓取日期。
- 图表为内嵌PNG，无脚本、外部字体、远程图片、登录或公共网页托管。
- 桌面及手机可浏览；较宽的数据表和图表可横向滚动，页面本身不横向溢出。
- 不是实时网页，打开时不会更新当期冻结数据。

部分邮箱不提供HTML附件预览，可先下载附件再用浏览器打开；
原PDF可作为移动端或附件受限客户端的替代。它不是外网链接，不公开报告。
PDF和HTML共享同一组渲染图表，均由同一份冻结JSON生成。
HTML已纳入SHA256清单；缺失或被修改时禁止发送，避免正文与附件不一致。

升级后仍可读取v1.2的四文件归档，不给旧归档补写HTML、不篡改今日已发邮件，
不删除发送回执。新版归档必须有五个文件，不能静默降级省略HTML。
新增12项测试覆盖离线资源、文本转义、来源日期、双附件MIME、
HTML篡改、附件缺失、旧归档兼容、未知版本和构建失败清理。
四份日报测试共118项；连同相关既有模块共195项通过，仅有1条原有Matplotlib警告。
真实冻结快照与缺数场景完成1440px桌面和390px手机离线浏览器检查：
三组图像加载成功、无外部网络请求、无页面错误、无页面横向溢出。
