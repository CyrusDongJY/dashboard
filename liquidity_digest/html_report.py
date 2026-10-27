"""Self-contained, script-free full report attachment from the frozen snapshot."""
from base64 import b64encode
from html import escape

from .render import (
    LAYERS, chart_images, disagreement, number, quality, summary_lines, cutoff_label,
)
from .states import MATRIX_TEXT, matrix_note
from .internals_view import internals_html
from .frontpage import front_html, build_frontpage
from .evidence import evidence_html


def text(value):
    return escape(str(value if value is not None else "待更新"), quote=True)


def table(headers, rows):
    head = "".join(f'<th scope="col">{text(h)}</th>' for h in headers)
    body = "".join("<tr>" + "".join(f"<td>{text(v)}</td>" for v in row) + "</tr>" for row in rows)
    return f'<div class="table-wrap"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'


def full_html(report, images=None, front_images=None):
    images = chart_images(report) if images is None else images
    if len(images) != 3:
        raise ValueError("Full report requires all three chart panels")
    panels = []
    for key in ("macro", "micro"):
        domain = report["domains"][key]
        state = domain["short"]
        rows = [(g["label"], g["short"]["label"], g["medium"]["label"], g["windows"])
                for g in domain["groups"]]
        reasons = sorted({g["label"] + "：" + (
            "未取得合格的连续历史变化" if g["key"] == "risk_capital" else e[p]["reason"])
            for g in domain["groups"] for e in g["evidence"] for p in ("short", "medium")
            if e[p]["direction"] == "UNKNOWN" and e[p].get("reason")})
        flags = "；".join(domain["flags"]) or "未检出所列分项提示；不代表宽松或低压"
        panels.append(
            f'<section class="domain"><h2>{text(domain["label"])}</h2>'
            f'<p class="state" style="color:{text(state["color"])}">{text(state["label"])}</p>'
            f'<p>中期：{text(domain["medium"]["label"])} · 近期有效 {state["ready_groups"]}/3 组</p>'
            + table(("观察组", "近期", "中期", "窗口"), rows)
            + f'<p class="notice">{text(flags)}</p>'
            + f'<p class="muted">{text(domain.get("timing_note", ""))}</p>'
            + "".join(f'<p class="muted">待确认原因：{text(reason)}</p>' for reason in reasons)
            + "</section>")

    pressure = report["states"].get("display_pressure", report["states"]["pressure"])
    matrix = ['<div class="matrix"><span></span><span>正在缓解</span><span>变化不明显</span><span>正在增加</span>']
    for zone, label in (("HIGH", "高压"), ("MID", "中间"), ("LOW", "低压")):
        known_row = pressure["zone"] == zone and pressure["direction"] == "UNKNOWN"
        matrix.append(f'<strong class="{"known-level" if known_row else ""}">{label}</strong>')
        for direction in ("DOWN", "FLAT", "UP"):
            chosen = pressure["zone"] == zone and pressure["direction"] == direction
            cls = "matrix-cell current" if chosen else "matrix-cell"
            matrix.append(f'<span class="{cls}">{text(MATRIX_TEXT[(zone, direction)])}'
                          + ("<small>当前</small>" if chosen else "") + "</span>")
    matrix.append("</div>")
    pressure_note = matrix_note(pressure)
    front = report.get("frontpage") or build_frontpage(report, {})
    alerts = "".join(f'<li>{text(a["text"])}</li>' for a in front["alerts"])

    groups = list(LAYERS)
    grouped = {key for _, keys in groups for key in keys}
    groups.append(("补充原始指标 / 不重复计票",
                   tuple(key for key in report["metrics"] if key not in grouped)))
    data = []
    for title, keys in groups:
        rows = []
        for key in keys:
            m = report["metrics"][key]
            changes = "；".join(f'{period} {number(value, True)}'
                                for period, value in m["changes"].items())
            if key == "usdjpy_return_5d_pct":
                changes = "当前值已为5日收益率；" + changes
            rows.append((m["label"], m["unit"], number(m["value"]), changes,
                         m["source_date"] or "无观测", quality(m)))
        data.append(f'<details><summary>{text(title)}</summary>' +
                    table(("指标", "单位", "当前", "各窗口变化", "观测日", "数据状态"), rows) + '</details>')

    charts = []
    for title, caption, image in zip(
        ("资金数量与分解", "资金价格与信用", "市场传导与连续状态"),
        ("同日口径的净流动性代理与周变化分解，不等于进入股市的资金流。",
         "融资利差与分层信用利差；日频缺失处断线，不以旧值填补。",
         "各板块使用各自尺度；只保留最新规则版本，不相加或作为交易信号。"),
        images,
    ):
        data_uri = b64encode(image.getvalue()).decode("ascii")
        charts.append(f'<figure><h3>{text(title)}</h3><div class="chart-wrap">'
                      f'<img src="data:image/png;base64,{data_uri}" alt="{text(title)}" width="1717" height="360">'
                      f'</div><figcaption>{text(caption)}</figcaption></figure>')
    funding = report["states"]["funding"]
    funding_line = (f'综合流动性支持（混合指标，非纯宏观）：{number(funding["display_value"])}/100，'
                    f'{funding["level_cn"]} / {funding["direction_cn"]}；'
                    f'5日压力方向变化 {number(funding["changes"]["5d"], True)} 点。')
    market_states = []
    for key, label in (("risk_capital", "风险资本扩散／收缩"), ("event_pulse", "异动事件扩散")):
        row = report["panels"].get(key)
        market_states.append(f'<p>{label}：{text((row.get("state_cn") or "状态待核实") if row else "昨日记录缺失")}。</p>')

    sources = [(m["label"], m.get("source"), m.get("source_date"), m.get("fetched_at"),
                quality(m)) for m in report["metrics"].values()]
    captures = [(key, v.get("source"), v.get("status"), v.get("captured_at"), v.get("attempt_at"))
                for key, v in report["source_audit"].items()]
    tag = "【核查更新版，非原定时报告】" if report.get("diagnostic") else "【模拟样例】" if report["demo"] else ""
    gaps = "、".join(report["gaps"]) or "无已识别缺口"
    focus = "".join(f'<li>{text(line)}</li>' for line in report["focus"])
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src data:; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'">
<meta name="referrer" content="no-referrer">
<title>{text(tag)}美股流动性与风险传导日报 {text(report["report_date"])}</title>
<style>
*{{box-sizing:border-box}}html{{background:#f4f6f7;color:#293b40;font-family:system-ui,-apple-system,"Microsoft YaHei",sans-serif;line-height:1.65;letter-spacing:0}}
body{{margin:0}}main{{max-width:1180px;margin:auto;background:#fff;padding:32px 40px}}
h1{{font-size:28px;line-height:1.35;margin:0 0 12px}}h2{{font-size:21px;line-height:1.4;margin:0 0 16px}}
h3{{font-size:17px;margin:24px 0 10px}}p{{margin:10px 0}}a{{color:#176c8c;text-underline-offset:3px}}
header{{border-bottom:2px solid #176c8c;padding-bottom:14px}}nav{{display:flex;gap:10px 20px;flex-wrap:wrap;margin-top:12px;font-size:13px}}
.index-grid{{display:grid;grid-template-columns:1fr 1fr;gap:28px}}.index-panel p{{overflow-wrap:anywhere}}
.band{{padding:28px 0;border-bottom:1px solid #dbe2e4}}.domains{{display:grid;grid-template-columns:1fr 1fr;gap:32px}}
.domain{{min-width:0}}.state{{font-size:29px;font-weight:600;line-height:1.25;margin:12px 0}}.muted,figcaption{{color:#5c7078;font-size:13px}}
.notice{{color:#8c5e12}}.warning{{color:#a23646;border-left:3px solid #a23646;padding-left:12px}}
.table-wrap,.chart-wrap{{overflow-x:auto;max-width:100%}}table{{width:100%;border-collapse:collapse;font-size:14px;text-align:left}}
th{{background:#edf3f5;font-weight:600}}th,td{{padding:10px 12px;border-bottom:1px solid #e1e7e9;vertical-align:top}}
td,th{{overflow-wrap:anywhere}}td:first-child{{min-width:120px}}.table-wrap table{{min-width:590px}}.domain table{{min-width:440px}}
#internals td,#internals th{{min-width:0}}#internals table[role="presentation"] td,#internals table[role="img"] td{{border:0}}
.confirmation{{display:grid;grid-template-columns:1fr 1fr;gap:32px}}.confirmation>*{{min-width:0}}
.matrix{{display:grid;grid-template-columns:42px repeat(3,minmax(0,1fr));gap:4px;font-size:13px;text-align:center}}
.matrix>span,.matrix>strong{{display:flex;align-items:center;justify-content:center;min-height:44px;padding:7px 3px;overflow-wrap:anywhere}}
.matrix-cell{{background:#edf2f4;flex-direction:column}}.matrix-cell.current{{background:#293b40;color:#fff;outline:2px solid #176c8c;outline-offset:-2px}}
.known-level{{color:#a23646;border-left:3px solid #a23646}}
.matrix small{{display:block;font-size:11px}}figure{{margin:0 0 28px}}figure img{{display:block;width:100%;min-width:680px;height:auto;aspect-ratio:1717/360}}
figcaption{{margin-top:10px}}ul{{padding-left:22px}}li{{margin-bottom:8px}}details{{margin:18px 0}}summary{{cursor:pointer;font-weight:600;color:#176c8c}}
footer{{font-size:12px;color:#5c7078;padding-top:22px}}.version{{overflow-wrap:anywhere}}
@media(max-width:760px){{main{{padding:24px 16px}}h1{{font-size:24px}}.domains,.confirmation,.index-grid{{grid-template-columns:1fr;gap:18px}}.band{{padding:24px 0}}.state{{font-size:26px}}.domain table{{min-width:0}}.domain td:first-child{{min-width:90px}}.domain td,.domain th{{font-size:13px;padding:8px 6px}}}}
@media print{{html{{background:#fff}}main{{max-width:none;padding:0}}nav{{display:none}}.table-wrap,.chart-wrap{{overflow:visible}}table{{min-width:0!important}}figure img{{min-width:0}}figure{{break-inside:avoid}}h2,h3{{break-after:avoid}}}}
</style></head><body><main>
<header><h1>{text(tag)}美股流动性与风险传导日报</h1>
<p>{"核查更新版" if report.get("diagnostic") else "盘前版"} / 宏观与微观分层 · {text(report["report_date"])}</p>
<p class="muted">美东{text(cutoff_label(report))}截止 · 收盘基准 {text(report["market_date"])} · 宏观按各源原生观测日</p>
{('<p class="notice">本次核查：' + text(report['correction']['note']) + '。原定时报告保留不动。</p>') if report.get('correction') else ''}
<nav aria-label="报告章节"><a href="#priority">资金曲线</a><a href="#overview">宏微观概况</a><a href="#tactical">战术压力</a><a href="#alerts">关键异常</a>
<a href="#capital">配置与持仓</a><a href="#internals">市场内部</a><a href="#evidence">数据明细</a><a href="#trends">更多趋势</a><a href="#audit">来源审计</a></nav></header>
{front_html(report, front_images)}
<section id="domain-evidence" class="band"><p class="muted">{text(report["domains"].get("evidence_note", ""))}</p>
<details><summary>宏观与微观分组证据</summary><div class="domains">{"".join(panels)}</div></details></section>
{internals_html(report)}
{evidence_html(report)}
<section class="band confirmation"><div><h2>跨资产确认 / 压力水位与方向</h2>{"".join(matrix)}
<p class="muted">{text(pressure_note)}</p><p class="muted">{text(pressure["reason"])}</p></div><div><h2>异常明细与分歧</h2><ul>{alerts or "<li>未检出既有观察规则异常，不代表没有风险。</li>"}</ul>
<p class="notice">数据待核查：{text("；".join(front["gaps"]) or "无已识别缺口")}</p>
<p>{text(disagreement(report))}</p></div></section>
<section id="evidence" class="band"><h2>分类数据与观测日期</h2>
{"".join(f"<p>{text(line)}</p>" for line in summary_lines(report))}
{"".join(data)}<p class="warning">原始指标日期/覆盖缺口：{text(gaps)}；不包含同版历史不足与补充来源不可用，详见首页及配置持仓板块。缺失及过期数据不参与状态确认。</p>
<p class="notice">错期参考：{text("；".join(report.get("publication_lags", [])) or "无")}。不冒充昨日同步数据。</p></section>
<section id="trends" class="band"><h2>趋势与证据 / 近150个自然日</h2>{"".join(charts)}
<p>{text(funding_line)}</p>{"".join(market_states)}<h3>优先查阅</h3>
<ul>{focus or "<li>尚无足够连续观测确认方向。</li>"}</ul></section>
<section id="audit" class="band"><h2>数据来源与解释边界</h2>
<p>{text(report["domains"]["scope_note"])}</p>
<p>宏微观各3组证据需完整，至少2组同向且无反向；1组变化称局部，冲突称分化。
数量组用1周/4周，其他组用5日/21日，不能视为同日同步变化。边际改善不等于当前宽松。</p>
<p>净流动性代理 = 同日美联储总资产 - 同日财政账户 - 同日逆回购，不是进入股市的资金量。
准备金为周平均。变化为原单位差值，百分数的变化为百分点，不是收益率；日元5日收益率单独标注。</p>
<p>股票按NYSE交易日；融资业务日历：SOFR按SIFMA，EFFR按联储银行；休市日不算缺口，真实缺测仍断窗。
信用与利率最多允许落后1个原生业务日的错期参考，并保留原日期；TGA按次业务日16:00发布节奏判断。
各时间状态只保留最新规则版本。历史图采用抓取时可见的修订值，不代表当日已经可知。</p>
<p>定时报告09:15之后发布的信息不回填原报告。核查更新版独立标识实际截止时间。
网页版与PDF来自同一冻结快照，不会随打开时间自动更新。</p>
<details><summary>逐项来源、观测日与抓取时间</summary>
{table(("指标", "来源", "观测日", "抓取时间", "状态"), sources)}</details>
<details><summary>采集审计</summary>{table(("输入项", "来源", "状态", "接收时间", "尝试时间"), captures)}</details>
<details><summary>首页资金指数口径</summary>{table(("指标", "表", "版本", "快照日", "接收时间"),
[(i["label"], i["source"], i["version"], i["source_date"], i["fetched_at"]) for i in front["indices"].values()])}
<p>核心资金实测与PIT_REPLAY回放分开，回放不参与当前状态或趋势；水位仪历史为存档计算值，非逐日已知信息回测。
仅使用各自固定权重版本，缺失或部分覆盖诊断值不作为正式总分，不跨版本拼线。</p></details>
</section><footer><p class="version">影子观察 · {text(report["version"])} · {text(report["domains"]["version"])}</p>
<p>缺失不等于平稳，未触发观察线不等于低风险。原有独立报告与预警保持不变。</p></footer>
</main></body></html>"""
