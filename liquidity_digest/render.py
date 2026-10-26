"""Chinese PDF and short email, rendered from the same frozen JSON."""
from datetime import date, timedelta
from functools import lru_cache
from html import escape
from io import BytesIO
import os
from pathlib import Path

from .model import SPECS, STATUS_CN, finite, sessions, native_sessions, stamp, NY
from .states import MATRIX_TEXT, STATE_COLORS, matrix_note
from .internals_view import internals_html, internals_image, internals_lines

LAYERS = (
    ("宏观 / 资金数量", ("net_liquidity", "reserves", "tga_daily", "rrp")),
    ("宏观 / 融资价格", ("sofr_iorb", "effr_iorb", "sofr_tail")),
    ("宏观 / 信用传导", ("ig", "hy", "ccc")),
    ("宏观背景 / 贴现与外部", ("ust2", "real10", "dxy", "usdjpy_return_5d_pct")),
    ("微观 / 广度与承接", ("pct_20ma", "pct_50ma", "pct_200ma", "spy_rsp_spread_pct",
                  "spy_vwap_time_acceptance_pct", "qqq_vwap_time_acceptance_pct")),
)
COLORS = {"environment": "#b83e4b", "tactical_stress": "#7255a5",
          "risk_capital": "#008c83", "liquidity": "#246ca0"}
PANEL_CN = {"environment": "综合环境压力", "tactical_stress": "跨资产战术压力",
            "risk_capital": "风险资本收缩", "liquidity": "综合流动性压力"}


@lru_cache(maxsize=1)
def embedded_font():
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    candidates = [
        os.environ.get("DIGEST_FONT_PATH"), os.environ.get("REPORT_FONT_PATH"),
        str(Path.home() / ".local/share/fonts/NotoSansSC-Regular.ttf"),
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
        "/usr/share/fonts/truetype/arphic/uming.ttc",
        "/System/Library/Fonts/STHeiti Light.ttc",
        "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
    ]
    for path in candidates:
        if not path or not Path(path).is_file():
            continue
        try:
            font = TTFont("DigestChinese", path)
            if not all(ord(char) in font.face.charToGlyph for char in "美股流动性观察资金"):
                continue
            pdfmetrics.registerFont(font)
            return "DigestChinese", path
        except Exception:
            continue
    raise RuntimeError("Install fonts-wqy-zenhei or set DIGEST_FONT_PATH to a Chinese TrueType font")


def number(value, signed=False):
    v = finite(value)
    if v is not None and round(v, 2) == 0:
        v = 0.0
    return ("{:+.2f}" if signed else "{:.2f}").format(v) if v is not None else "待更新"


def cutoff_label(report):
    return stamp(report["cutoff"]).astimezone(NY).strftime("%H:%M")


def quality(metric):
    parts = []
    if metric["status"] != "OK":
        parts.append(STATUS_CN[metric["status"]])
    if metric["acquisition"] != "OK":
        parts.append(STATUS_CN.get(metric["acquisition"], "状态待核实"))
    if metric.get("publication_note"):
        parts.append(metric["publication_note"])
    return "；".join(parts) or "已取得"


def tactical_lines(report):
    row = report["panels"].get("tactical")
    if not row:
        return ["昨日收盘记录缺失，不能用旧分数替代；压力水位与方向待确认。", "影子观察，不触发预警，不单独推断流动性挤兑。"]
    components = row.get("stress_components") or {}
    if not isinstance(components, dict):
        components = {}
    coverage = finite(row.get("stress_coverage"))
    meta = components.get("_meta") or {}
    score = number(row.get("eod_stress_score")) if meta.get("status") in ("OK", "PARTIAL") else "不可计算"
    text = [f'跨资产确认，非纯微观。观察值 {score} / 100；覆盖率 {number(coverage * 100 if coverage is not None else None)}%；'
            f'截至 {report["market_date"]}。']
    parts = []
    for key, label in (("vix", "VIX"), ("move", "MOVE"), ("credit_spread", "信用"),
                       ("trin", "TRIN代理"), ("pcr", "PCR"), ("cmf", "CMF")):
        component = components.get(key) or {}
        contribution = (number(component.get("contribution"), True)
                        if component.get("status") == "OK" else "不可用")
        parts.append(f'{label} {number(component.get("value"))} / 贡献 {contribution}')
    text.extend(["；".join(parts[:3]), "；".join(parts[3:])])
    context = components.get("_context") or {}
    text.append(f'评分背景：VIX 5日变化 {number(context.get("vix_5d_change"), True)} 点；'
                f'252日分位 {number(context.get("vix_percentile_252"))}%；'
                f'VIX/VIX3M {number(context.get("vix_term_ratio"))}。')
    temporal = report["states"]["panels"]["tactical_stress"]
    text.append("压力值变化（1／5／21日）：" + " / ".join(
        number(temporal["changes"].get(period), True) for period in ("1d", "5d", "21d"))
        + "；" + temporal["direction_cn"] + "。")
    confidence = finite(row.get("stress_confidence"))
    status = {"OK": "可用", "PARTIAL": "部分覆盖", "UNAVAILABLE": "不可计算"}.get(meta.get("status"), "状态待核实")
    text.append(f'数据状态：{status}；置信度 {number(confidence * 100 if confidence is not None else None)}%；'
                f'版本 {row.get("stress_calc_version") or "未记录"}。')
    return text


def summary_lines(report):
    current = sum(m["status"] == "OK" and m["acquisition"] == "OK" for m in report["metrics"].values())
    total = len(report["metrics"])
    reference = sum(m["status"] == "AS_OF" and m["acquisition"] == "OK" for m in report["metrics"].values())
    text = [f'同日或按发布频率可用 {current}/{total} 项；另有错期参考 {reference} 项。未更新不等于低风险。']
    if current == 0:
        text.append("当前证据不足，暂停环境判断；请核查采集任务和数据源配置。")
    elif report["gaps"]:
        text.append("部分数据仍滞后或缺失，以下保留各自观测日期，不做混合总评分。")
    else:
        text.append("按资金数量、融资成本、信用和市场接受逐层观察，不将余额直接解释为股票资金流。")
    return text


def disagreement(report):
    a, b = report["metrics"]["net_liquidity"], report["metrics"]["pct_200ma"]
    if any(m["status"] != "OK" or m["acquisition"] != "OK" for m in (a, b)):
        return "关键分歧：数量与市场接受的证据未齐，不作同向或背离判断。"
    da, db = a["changes"].get("1周"), b["changes"].get("5日")
    if da is not None and db is not None and da * db < 0:
        return "关键分歧：净流动性周变化与市场广度5日变化方向相反；窗口不同，仅作核查线索。"
    return "关键分歧：当前未检出数量与广度的方向背离；这不代表风险不存在。"


def email_text(report):
    tag = "【核查更新版，非原定时报告】" if report.get("diagnostic") else "【模拟样例】" if report["demo"] else ""
    edition = "核查更新版" if report.get("diagnostic") else "盘前版"
    lines = [tag + "美股流动性与风险传导日报｜" + edition,
             f'报告日 {report["report_date"]}｜美东{cutoff_label(report)}截止｜收盘基准 {report["market_date"]}', ""]
    if report.get("correction"):
        lines.extend(["本次核查：" + report["correction"]["note"], "原定时报告保留不动。", ""])
    states = report["states"]
    domains = report["domains"]
    from .frontpage import build_frontpage, index_summary
    front = report.get("frontpage") or build_frontpage(report, {})
    lines.extend(["核心观察", *[item["label"] + "：" + index_summary(item)
                               for item in front["indices"].values()],
                  "", domains["headline"], *domain_lines(report), domains["relation"],
                  "宏观使用截止前最新原生观测；微观及综合评分截至 " + report["market_date"] + " 收盘。",
                  "", "盘后跨资产战术压力观察", *tactical_lines(report),
                  "", "关键异常与分歧", *[a["text"] for a in front["alerts"]],
                  "数据待核查：" + ("；".join(front["gaps"]) or "无已识别缺口"), ""])
    lines.append(domains.get("evidence_note", ""))
    lines.extend(["", *internals_lines(report), ""])
    from .capital import capital_lines
    lines.extend(["资金配置、持仓与杠杆", *capital_lines(report.get("capital", {}))])
    lines.append("跨资产综合确认（不作为宏观/微观的重复投票）：" + states["headline"])
    for state in (states["funding"], states["pressure"]):
        lines.append(f'{state["label"]}：{state["level_cn"]} / {state["direction_cn"]}；'
                     f'5日压力方向值变化 {number(state["changes"]["5d"], True)} 点。')
    lines.extend([states["pressure_note"], *[a["text"] for a in states["alerts"][:3]], ""])
    lines.append(data_text(report))
    return "\n".join(lines)


def domain_lines(report):
    lines = []
    for key in ("macro", "micro"):
        domain = report["domains"][key]
        lines.append(f'{domain["label"]}：近期{domain["short"]["label"]}；中期{domain["medium"]["label"]}。')
        for group in domain["groups"]:
            lines.append(f'  {group["label"]}：{group["short"]["label"]} / {group["medium"]["label"]}'
                         f'（{group["windows"]}）')
        lines.append("当期分项提示：" + ("；".join(domain["flags"]) or "未检出所列分项提示；不代表宽松或低压"))
        if domain.get("timing_note"):
            lines.append(domain["timing_note"])
        if domain["disagreement"]:
            lines.append("近中期方向不同，暂不认定趋势反转。")
    lines.append("先按组观察，再看共同方向；全部3组有效、至少2组同向且无反向才给出整体方向。")
    return lines


def data_text(report):
    lines = summary_lines(report)
    for title, keys in LAYERS:
        values = []
        for key in keys[:2]:
            m = report["metrics"][key]
            values.append(f'{m["label"]} {number(m["value"])} {m["unit"]}'
                          f'（{m["source_date"] or "无日期"}；{quality(m)}）')
        lines.append(title + "：" + "；".join(values))
    lines.extend(["", "优先查阅", *(report["focus"] or ["尚无足够连续观测确认方向。"]),
                  disagreement(report), "", "完整数据、趋势和缺口见附件HTML网页版或PDF；原报告保持独立。",
                  "HTML附件可下载后用浏览器打开，图表已内嵌，无需联网或登录。",
                  "数据为截至抓取时可见的修订版本；历史图不是逐日已知信息回测。"])
    return "\n".join(lines)


def email_html(report, front_images=None):
    from .frontpage import front_html
    from .evidence import evidence_html
    tag = "【核查更新版，非原定时报告】" if report.get("diagnostic") else "【模拟样例】" if report["demo"] else ""
    edition = "核查更新版" if report.get("diagnostic") else "盘前版"
    return ('<html lang="zh-CN"><head><meta name="viewport" content="width=device-width,initial-scale=1"></head>'
            '<body style="font-family:sans-serif;color:#293b40;line-height:1.65;max-width:760px;margin:auto;padding:12px">'
            f'<h2>{tag}美股流动性与风险传导日报</h2>'
            f'<p>{escape(report["report_date"])} {edition} | 美东{escape(cutoff_label(report))}截止；'
            f'微观与评分截至 {escape(report["market_date"])} 收盘；宏观按原生观测日</p>'
            + (f'<p>本次核查：{escape(report["correction"]["note"])}。原定时报告保留不动。</p>' if report.get("correction") else "")
            + front_html(report, front_images)
            + f'<p>{escape(report["domains"].get("evidence_note", ""))}</p>'
            + internals_html(report, compact=True)
            + evidence_html(report, compact=True)
            + '<p style="font-size:13px">完整分类数据、历史趋势和来源审计见HTML网页版或PDF附件。'
            'HTML附件可离线打开，原有独立报告保持不变。</p></body></html>')


def chart_images(report):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates
    import numpy as np
    from matplotlib import font_manager

    _, font_path = embedded_font()
    font_manager.fontManager.addfont(font_path)
    plt.rcParams["font.family"] = [font_manager.FontProperties(fname=font_path).get_name(), "DejaVu Sans"]
    plt.rcParams["axes.unicode_minus"] = False
    start = date.fromisoformat(report["market_date"]) - timedelta(days=150)
    day_grid = sessions(start, report["market_date"])
    metrics = report["metrics"]

    def line(ax, points, label, color, frequency="daily", key=None):
        values = {p["date"]: p["value"] for p in points if p["date"] >= str(start)}
        if frequency == "weekly" and values:
            first, last = date.fromisoformat(min(values)), date.fromisoformat(max(values))
            grid = [str(first + timedelta(days=7 * i)) for i in range((last - first).days // 7 + 1)]
        else:
            grid = native_sessions(key, start, report["market_date"]) if key else day_grid
        if values:
            ax.plot([date.fromisoformat(d) for d in grid], [values.get(d, np.nan) for d in grid],
                    label=label, color=color, linewidth=1.4)

    def finish(fig, axes):
        for ax in axes:
            ax.set_xlim(start, date.fromisoformat(report["market_date"]))
            ax.grid(axis="y", alpha=.18)
            ax.spines[["top", "right"]].set_visible(False)
            ax.tick_params(labelsize=7)
            ax.xaxis.set_major_locator(mdates.AutoDateLocator(minticks=3, maxticks=5))
            ax.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d"))
            handles, _ = ax.get_legend_handles_labels()
            if handles:
                ax.legend(fontsize=7, frameon=False, loc="best")
            else:
                ax.text(.5, .5, "数据不足，等待有效观测", transform=ax.transAxes, ha="center", fontsize=9)
        fig.tight_layout(pad=.8)
        out = BytesIO()
        fig.savefig(out, format="png", dpi=170, facecolor="white")
        plt.close(fig)
        out.seek(0)
        return out

    images = []
    fig, axes = plt.subplots(1, 2, figsize=(10.1, 2.12))
    line(axes[0], metrics["net_liquidity"]["history"], "净流动性代理", "#176c8c", "weekly")
    axes[0].set_title("同日资产 - 财政账户 - 逆回购", fontsize=9)
    axes[0].set_ylabel("十亿美元", fontsize=8)
    net_dates = [p["date"] for p in metrics["net_liquidity"]["history"] if p["date"] >= str(start)]
    for key, label, color, sign in (
        ("fed_assets", "总资产变化", "#176c8c", 1),
        ("tga_weekly", "财政账户反向变化", "#bd5135", -1),
        ("rrp", "逆回购反向变化", "#008c83", -1),
    ):
        raw = {p["date"]: p["value"] for p in metrics[key]["history"]}
        changes = []
        for day in net_dates:
            previous = str(date.fromisoformat(day) - timedelta(days=7))
            if day in raw and previous in raw:
                changes.append({"date": day, "value": sign * (raw[day] - raw[previous])})
        line(axes[1], changes, label, color, "weekly")
    axes[1].axhline(0, color="#777777", linewidth=.6)
    axes[1].set_title("同日口径的周变化分解", fontsize=9)
    axes[1].set_ylabel("十亿美元 / 周", fontsize=8)
    images.append(finish(fig, axes))

    fig, axes = plt.subplots(1, 2, figsize=(10.1, 2.12))
    for key, label, color in (("sofr_iorb", "SOFR-IORB", "#176c8c"),
                               ("effr_iorb", "EFFR-IORB", "#008c83"),
                               ("sofr_tail", "SOFR尾部", "#bd5135")):
        line(axes[0], metrics[key]["history"], label, color, key=key)
    axes[0].set_title("融资成本与尾部利差", fontsize=9)
    for key, label, color in (("ig", "投资级", "#176c8c"), ("hy", "高收益", "#bd5135"),
                               ("ccc", "CCC及以下", "#7255a5")):
        line(axes[1], metrics[key]["history"], label, color, key=key)
    axes[1].set_title("信用风险分层", fontsize=9)
    for ax in axes:
        ax.set_ylabel("基点", fontsize=8)
    images.append(finish(fig, axes))

    fig, ax = plt.subplots(figsize=(10.1, 2.12))
    from temporal_monitor import comparison_segments
    for key, color in COLORS.items():
        records = report["temporal"].get(key, [])
        for i, segment in enumerate(comparison_segments(records)):
            line(ax, [{"date": r["report_date"], "value": r["risk_value"]} for r in segment],
                 PANEL_CN[key] if i == 0 else "_nolegend_", color)
    ax.set_ylim(0, 100)
    ax.set_title("核心板块时间状态：各自尺度，不能相加或当作交易信号", fontsize=9)
    ax.set_ylabel("压力方向值", fontsize=8)
    images.append(finish(fig, [ax]))
    return images


def render_pdf(report, path, images=None, front_images=None):
    from reportlab.pdfgen import canvas
    from reportlab.lib.utils import ImageReader
    from reportlab.platypus import Paragraph
    from reportlab.lib.styles import ParagraphStyle

    font, _ = embedded_font()
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    c = canvas.Canvas(str(path), pagesize=(595.28, 841.89))
    c.setTitle("美股流动性与风险传导日报")
    c.setAuthor("Market Dashboard")
    margin, width = 34, 527
    total_pages = (4 if report.get("internals") else 3) + bool(report.get("capital"))

    def text(value, x, y, size=9, color="#293b40"):
        c.setFont(font, size)
        c.setFillColor(color)
        c.drawString(x, y, str(value))

    def paragraph(value, y, size=9, color="#293b40", x=None, max_width=None):
        style = ParagraphStyle("cn", fontName=font, fontSize=size, leading=size + 4,
                               textColor=color, wordWrap="CJK")
        p = Paragraph(escape(str(value)), style)
        _, height = p.wrap(max_width or width, 200)
        p.drawOn(c, margin if x is None else x, y - height)
        return y - height - 4

    def header(subtitle, page):
        text("美股流动性与风险传导日报", margin, 806, 18)
        text(subtitle, margin, 783, 10, "#176c8c")
        text(f'{report["report_date"]} | 美东{cutoff_label(report)}截止 | 收盘基准 {report["market_date"]}', margin, 765, 8)
        if report["demo"]:
            text("模拟样例 · 非真实行情", 410, 807, 10, "#b83e4b")
        if report.get("diagnostic"):
            text("核查更新版 · 非原定时报告", 390, 807, 9, "#b83e4b")
        c.setStrokeColor("#c4d0d3")
        c.line(margin, 752, 561, 752)
        text(f'影子观察 | {report["version"]} | {page}/{total_pages}', margin, 23, 7, "#63757a")

    from .frontpage import build_frontpage, index_images, index_summary
    front = report.get("frontpage") or build_frontpage(report, {})
    front_images = index_images(report) if front_images is None else front_images
    if len(front_images) != 2:
        raise ValueError("Front page requires two index charts")
    header("首页 / 宏微观概况、资金曲线与跨资产压力", 1)
    y = paragraph(report["domains"]["headline"], 740, 13)
    y = paragraph(report["domains"]["relation"], y, 9)
    top = y - 8
    for i, (item, image) in enumerate(zip(front["indices"].values(), front_images)):
        x = margin + i * 273
        text(item["label"], x, top, 12, item["color"])
        bottom = paragraph(index_summary(item), top - 10, 8, x=x, max_width=254)
        c.drawImage(ImageReader(image), x, bottom - 111, width=254, height=111, mask="auto")
        paragraph(item["reason"], bottom - 115, 7, "#63757a", x=x, max_width=254)
    y = paragraph(front["scope_note"], top - 180, 8, "#63757a")
    domain_top = y - 8
    for key, x in (("macro", margin), ("micro", 307)):
        domain = report["domains"][key]
        state = domain["short"]
        text(domain["label"], x, domain_top, 10)
        text(state["label"], x, domain_top - 22, 16, state["color"])
        text("中期：" + domain["medium"]["label"] +
             ("；近中期背离" if domain["disagreement"] else ""), x, domain_top - 37, 8)
        for i, group in enumerate(domain["groups"]):
            text(group["label"] + "：" + group["short"]["label"], x,
                 domain_top - 52 - i * 12, 8, group["short"]["color"])
        text(f'近期有效 {state["ready_groups"]}/3 组；缺失不代表平稳。', x, domain_top - 90, 7, "#63757a")
    y = domain_top - 98
    text("盘后跨资产战术压力观察", margin, y - 12, 12, "#7255a5")
    y -= 26
    for line in tactical_lines(report):
        y = paragraph(line, y, 9)
    y = paragraph("影子观察，不触发预警；跨资产确认层，不作为宏微观重复投票。", y, 8, "#63757a")
    text("关键异常与分歧", margin, y - 10, 12, "#9b3545")
    y -= 23
    alerts = front["alerts"][:4] or [{"text": "未检出既有观察规则异常；不代表没有风险。"}]
    for alert in alerts:
        y = paragraph("• " + alert["text"], y, 9, "#9b3545")
    if len(front["alerts"]) > 4:
        y = paragraph(f'另有 {len(front["alerts"]) - 4} 项提示，见网页版异常明细。', y, 8)
    if front["gaps"]:
        y = paragraph("数据完整性：" + "；".join(front["gaps"][:2]) + "。完整缺口见分类数据与审计。", y - 3, 8, "#806120")
    y = paragraph("曲线仅连接同版有效观测，缺测处断线；回放参考单列，不补入实测趋势。资金分数不代表股票现金流。", y, 7, "#63757a")
    if y < 40:
        raise ValueError("Priority page exceeds layout budget")
    c.showPage()

    header("分类证据 / 宏观与微观分层", 2)
    states = report["states"]
    domains = report["domains"]
    paragraph(domains["headline"], 741, 14)
    text("宏观按最新原生观测日；微观及综合评分截至 " + report["market_date"] + " 收盘。", margin, 707, 8, "#63757a")

    def domain_panel(domain, x, top):
        state = domain["short"]
        text(domain["label"], x, top, 11)
        text(state["label"], x, top - 25, 18, state["color"])
        text("中期：" + domain["medium"]["label"] +
             ("；近中期背离" if domain["disagreement"] else ""), x, top - 42, 9)
        text("观察组", x, top - 57, 7, "#63757a")
        text("近期 / 1周或5日", x + 91, top - 57, 7, "#63757a")
        text("中期 / 4周或21日", x + 174, top - 57, 7, "#63757a")
        for i, group in enumerate(domain["groups"]):
            ygroup = top - 71 - i * 13
            text(group["label"], x, ygroup, 8)
            for dx, period in ((91, "short"), (174, "medium")):
                result = group[period]
                c.setFillColor(result["color"])
                c.circle(x + dx + 2, ygroup + 3, 2.2, fill=1, stroke=0)
                text(result["label"], x + dx + 9, ygroup, 8, result["color"])
        text(f'近期有效 {state["ready_groups"]}/3 组；各组不重复计票，不生成新总分。',
             x, top - 110, 7, "#63757a")
        if domain.get("timing_note"):
            text(domain["timing_note"], x, top - 118, 6.5, "#916619")

    domain_panel(domains["macro"], 34, 682)
    domain_panel(domains["micro"], 307, 682)
    text("跨资产确认 / 压力水位 × 方向", margin, 553, 11, "#176c8c")
    pressure = states["pressure"]
    text("提示与分歧", 350, 553, 11, "#176c8c")
    matrix_x, matrix_top, cell_w, cell_h = 71, 530, 82, 30
    for j, label in enumerate(("正在缓解", "变化不明显", "正在增加")):
        text(label, matrix_x + j * cell_w + 12, 537, 7.5)
    for i, (z, label) in enumerate((("HIGH", "高压"), ("MID", "中间"), ("LOW", "低压"))):
        yrow = matrix_top - (i + 1) * cell_h
        text(label, margin, yrow + 11, 8)
        for j, d in enumerate(("DOWN", "FLAT", "UP")):
            chosen = pressure["zone"] == z and pressure["direction"] == d
            accent = "#9c6812" if z == "HIGH" and d == "DOWN" else STATE_COLORS[d]
            c.setFillColor(accent if chosen else ("#fbf0f1" if z == "HIGH" else "#eff3f5"))
            c.rect(matrix_x + j * cell_w + 1, yrow + 1, cell_w - 2, cell_h - 2, fill=1, stroke=0)
            text(MATRIX_TEXT[(z, d)], matrix_x + j * cell_w + 17, yrow + 10, 8,
                 "#ffffff" if chosen else "#67757b")
            if chosen:
                text("当前", matrix_x + j * cell_w + 3, yrow + 22, 5.5, "#ffffff")
    text(matrix_note(pressure), margin, 426, 7.5, "#63757a")
    alert_y = 540
    alerts = [{"kind": "divergence", "text": domains["relation"]}, *states["alerts"][:2]]
    for alert in alerts:
        alert_y = paragraph(alert["text"], alert_y, 8,
                            "#8c3944" if alert["kind"] in ("pressure", "shock") else
                            "#916619" if alert["kind"] in ("raw_guard", "divergence", "watch") else "#56676e",
                            x=350, max_width=211)
    if alert_y < 420:
        raise ValueError("State warnings exceed layout budget")

    text("分类证据 / 周频看1周变化，日频看5日变化", margin, 407, 10, "#176c8c")

    def compact_data(groups, x):
        from reportlab.pdfbase import pdfmetrics
        text("指标（单位）", x, 390, 7)
        for dx, label in ((117, "当前"), (164, "变化"), (215, "观测日")):
            text(label, x + dx, 390, 7)
        ydata = 373
        for label, keys in groups:
            c.setFillColor("#edf2f3")
            c.rect(x, ydata - 2, 254, 13, fill=1, stroke=0)
            text(label, x + 3, ydata + 1, 8, "#176c8c")
            ydata -= 15
            for key in keys:
                m = report["metrics"][key]
                label = f'{m["label"]}（{m["unit"]}）'
                size = min(7, 110 * 7 / max(pdfmetrics.stringWidth(label, font, 7), 1))
                text(label, x + 2, ydata, size)
                change = (m["changes"].get("1周") if m["frequency"] == "weekly" else m["changes"].get("5日"))
                change_text = "已为5日幅度" if key == "usdjpy_return_5d_pct" else number(change, True)
                c.setFont(font, 7)
                data_color = ("#a84552" if m["acquisition"] != "OK" or m["status"] not in ("OK", "AS_OF")
                              else "#916619" if m["status"] == "AS_OF" else "#293b40")
                c.setFillColor(data_color)
                for dx, value in ((151, number(m["value"])), (199, change_text), (254, m["source_date"] or "无观测")):
                    c.drawRightString(x + dx, ydata, value)
                ydata -= 13
            ydata -= 3
        return ydata

    yleft = compact_data(LAYERS[:3], 34)
    yright = compact_data(LAYERS[3:], 307)
    capital = states["panels"]["risk_capital"]
    paragraph(f'风险资本：{capital["level_cn"]} / {capital["direction_cn"]}。', yright - 4, 8,
              x=307, max_width=254)
    text("棕色：错期参考；红色：缺测或过期，不参与确认。", 307, yright - 37, 7, "#63757a")
    y = min(yleft, yright - 50) - 10
    y = paragraph("战术压力与关键异常已前置首页；本页保留分组证据，不重复累加不同评分。", y, 8)
    y = paragraph("宏微观方向采用独立分组影子规则，非交易信号；缺失不等于平稳，边际改善不等于当前宽松。"
                  "综合压力仍沿用原评分与35/65水位、5日±5点方向线，不新增总评分或改变原预警。", y - 3, 7)
    if y < 40:
        raise ValueError("Page one content exceeds layout budget")
    c.showPage()

    if report.get("internals"):
        header("微观环境解读 / 市场内部结构", 3)
        panel = report["internals"]
        y = paragraph(panel["headline"], 737, 14)
        y = paragraph(panel["scope_note"] + " 截至 " + panel["market_date"] + " 收盘。", y, 9)
        p, r, b = panel["persistence"], panel["rotation"], panel["breadth"]
        y = paragraph("20日均线广度：" + p["label"] + "；" + r["label"] + "。", y, 10)
        y = paragraph(panel["interpretation"], y, 9)
        image = internals_image(report)
        c.drawImage(ImageReader(image), margin, y - 365, width=width, height=355, mask="auto")
        y -= 380
        if b["quality"] == "OK":
            y = paragraph(f'价格有效样本 {b["valid_count"]}/500，上涨 {b["advancing"]} / 平盘 {b["unchanged"]} / '
                          f'下跌 {b["declining"]} 家；分母包含平盘，不拼接旧pct_adv口径。', y, 9)
        y = paragraph("20日均线广度5日 / 21日变化：" + number(p["changes"]["5d"], True)
                      + " / " + number(p["changes"]["21d"], True) + " 个百分点。", y, 9)
        for key, vals in panel["leadership"]["spreads"].items():
            y = paragraph(key + "相对表现差（1 / 5 / 21日）：" + " / ".join(
                number(vals[period], True) for period in ("1d", "5d", "21d")) + " 个百分点。", y, 8)
        y = paragraph("轮动观察：" + r["reason"] + (" 从" + "、".join(r["from_sectors"]) + "到"
                      + "、".join(r["to_sectors"]) + "。" if r["state"] == "CANDIDATE" else ""), y, 8)
        y = paragraph(r.get("history_note", ""), y, 8)
        y = paragraph("读图口径：涨跌是绝对收益，超额是相对SPY的收益差；相对抗跌不等于上涨。"
                      "样本每天按市值筛选，历史不代表固定成分股队列。扫描器日期按采集日，逐股源时间与收盘竞价覆盖未验证。", y, 8)
        y = paragraph("影子阈值：涨/跌占比≥65%称广泛；行业扩散需至少8/11同向；"
                      "20日均线广度单日变化1个百分点为噪声线。描述性规则未经预测回测。", y, 8)
        if panel["gaps"]:
            y = paragraph("待确认：" + "；".join(panel["gaps"]), y, 8, "#916619")
        y = paragraph(panel["shadow_note"], y, 8, "#63757a")
        if y < 40:
            raise ValueError("Market internals page exceeds layout budget")
        c.showPage()

    if report.get("capital"):
        from .capital import capital_lines
        from .evidence import funding_evidence
        header("资金配置、持仓与杠杆 / 与价格代理分开观察", total_pages - 1)
        y = paragraph(report["capital"]["scope_note"], 735, 10)
        for line in capital_lines(report["capital"]):
            y = paragraph(line, y - 8, 10)
        y = paragraph(report["capital"]["etf_note"], y - 8, 9, "#806120")
        y = paragraph("周度持仓与月度融资不是盘前实时资金流；实际接收时间及完整持仓分项见网页版。", y, 8, "#63757a")
        y = paragraph("资金条件、基数效应与传导", y - 14, 13, "#176c8c")
        facts = funding_evidence(report)
        for line in facts["notes"][:10]:
            y = paragraph(line, y, 9)
        for pillar in facts["pillars"]:
            y = paragraph(pillar["label"] + "：" + number(pillar["score"]) + "/100；" +
                          ("；".join(pillar["missing"][:2]) or "完整分项见网页版"), y, 8)
        if y < 40:
            raise ValueError("Capital evidence page exceeds layout budget")
        c.showPage()
    header("趋势与证据 / 近150个自然日", total_pages)
    images = chart_images(report) if images is None else images
    y = 740
    for title, image in zip(("01 资金数量与分解", "02 资金价格与信用", "03 市场传导与连续状态"), images):
        text(title, margin, y, 11, "#176c8c")
        c.drawImage(ImageReader(image), margin, y - 127, width=width, height=111, mask="auto")
        y -= 145
    funding = states["funding"]
    y = paragraph(f'综合流动性支持（混合指标，非纯宏观）：{number(funding["display_value"])}/100，'
                  f'{funding["level_cn"]} / {funding["direction_cn"]}；5日压力方向变化 '
                  f'{number(funding["changes"]["5d"], True)} 点。', y, 8)
    for key in ("macro", "micro"):
        domain = domains[key]
        flags = "；".join(domain["flags"]) or "未检出所列分项提示，不代表宽松或低压"
        y = paragraph(domain["label"] + "当期提示：" + flags + "。", y, 8, "#8c3944")
    for panel, label in (("risk_capital", "风险资本扩散／收缩"), ("event_pulse", "异动事件扩散")):
        row = report["panels"].get(panel)
        value = (row.get("state_cn") or "状态待核实") if row else "昨日记录缺失"
        y = paragraph(f"{label}：{value}。", y, 9)
    y = paragraph("读图口径：周频按原始观测点展示，日频缺失处断线；各面板只保留最新规则版本，"
                  "不拼接回放结果。历史序列采用本次抓取时可见的修订值，不代表当时已经可知。", y, 8)
    y = paragraph("宏微观方向要求各3组证据完整，至少2组同向且无反向；1组变化只称局部，冲突称分化。"
                  "数量组用1周/4周，其他组用5日/21日，窗口不能视为同日同步变化。"
                  "利率、美元、日元、CCC利差与集中度作背景；VIX、MOVE与战术压力作综合确认，不重复投票。", y, 8)
    y = paragraph("数量口径：净流动性代理 = 同日美联储总资产 - 同日财政账户 - 同日逆回购；"
                  "它不是进入股市的资金量。准备金为周平均，与周三时点单独展示。"
                  "周频变化列依次为1周／4周；日频为1／5／21个原生业务日（股票NYSE、SOFR债市、EFFR联储）。"
                  "变化为原单位差值，百分数的变化为百分点，不是收益率。", y, 8)
    gap_text = "、".join(report["gaps"]) or "无已识别缺口"
    if report.get("correction"):
        y = paragraph("本次核查更新：" + report["correction"]["note"] + "。原定时报告保留不动。", y, 8)
    y = paragraph("待更新或需核查：" + gap_text + "。", y, 8, "#8c3944")
    y = paragraph("数据来源：纽约联储参考利率、FRED各原始序列、美国财政部DTS及现有系统收盘表。"
                  "逐项抓取时间、源名称、原始序列、规则版本与缺口原因见随日报保存的JSON。"
                  "定时报告09:15之后发布的信息不回填原邮件；核查更新版以页首独立截止时间为准。", y, 8)
    if y < 40:
        raise ValueError("Page two content exceeds layout budget")
    c.save()
