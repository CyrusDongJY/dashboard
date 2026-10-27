"""One market-internals view shared by mail, offline HTML and PDF."""
from html import escape
from io import BytesIO

from .model import finite


def value(number, signed=False, suffix=""):
    number = finite(number)
    if number is None:
        return "待采"
    number = 0 if round(number, 1) == 0 else number
    return (f"{number:+.1f}" if signed else f"{number:.1f}") + suffix


def internals_lines(report):
    panel = report.get("internals")
    if not panel:
        return []
    b, s, p, r = (panel[k] for k in ("breadth", "sectors", "persistence", "rotation"))
    p = panel.get("display_persistence", p)
    lines = ["市场内部结构 / 微观环境的解释层", panel["headline"],
             panel["interpretation"],
             f'截至 {panel["market_date"]} 收盘；{panel["scope_note"]}',
             f'持续变化：{p["label"]}；20日均线广度5日 / 21日变化 '
             f'{value(p["changes"]["5d"], True)} / {value(p["changes"]["21d"], True)} 个百分点。']
    if b["quality"] == "OK":
        lines.append(f'上涨 {b["advancing"]} / 平盘 {b["unchanged"]} / 下跌 {b["declining"]} 家；'
                     f'有效价格 {b["valid_count"]}/500；上涨比例 {value(b["advancing_pct"], suffix="%")}。')
    else:
        lines.append(b["reason"])
    lines.append(f'行业当日上涨 {s["positive_count"]}/{s["valid_count"]} 个有效行业；有效 {s["valid_count"]}/11。'
                 if s["valid_count"] else "行业当日数据尚未取得，不能按0个行业上涨解释。")
    lines.append(r["label"] + ("：" + "、".join(r["from_sectors"]) + " → " + "、".join(r["to_sectors"])
                              if r["state"] == "CANDIDATE" else "；" + r["reason"]))
    lines.append(r.get("history_note", ""))
    lines.append(p["reason"])
    lines.append(panel["shadow_note"])
    return lines


def _color(number):
    number = finite(number)
    return "#edf0f2" if number is None else "#d9eee5" if number > 0 else "#f7dfe2" if number < 0 else "#f1f3f4"


def internals_html(report, compact=False):
    panel = report.get("internals")
    if not panel:
        return ""
    b, s, p, rotation = (panel[k] for k in ("breadth", "sectors", "persistence", "rotation"))
    p = panel.get("display_persistence", p)
    esc = lambda x: escape(str(x), quote=True)
    distribution = '<p style="color:#69777d">' + esc(b["reason"]) + "</p>"
    if b["quality"] == "OK":
        blocks = []
        for key, label, color in (("advancing", "上涨", "#258269"), ("unchanged", "平盘", "#87969d"),
                                  ("declining", "下跌", "#b54659")):
            pct = b[key + "_pct"]
            if pct > 0:
                blocks.append(f'<td width="{pct:.4f}%" style="height:14px;background:{color};padding:0" '
                              f'title="{label} {b[key]}家 / {pct:.1f}%"></td>')
        distribution = ('<table role="presentation" width="100%" cellspacing="0" cellpadding="0" '
                        'style="table-layout:fixed;margin:12px 0 4px"><tr>' + "".join(blocks) + "</tr></table>"
                        + f'<p style="font-size:12px;margin:4px 0">上涨 {b["advancing"]}家（{value(b["advancing_pct"])}%）'
                        + f' · 平盘 {b["unchanged"]}家 · 下跌 {b["declining"]}家 · 有效价格 {b["valid_count"]}/500</p>')
    periods = (("1d", "1日"), ("5d", "5日"), ("21d", "21日"))
    headings = ("<th>行业</th>" + "".join(f'<th>{label}<br><small>涨跌% / 超额百分点</small></th>' for _, label in periods)
                if compact else "<th>行业</th>" + "".join(f"<th>{label}涨跌%</th><th>{label}超额百分点</th>" for _, label in periods))
    rows = []
    for row in s["rows"]:
        cells = [f'<th scope="row" style="text-align:left;white-space:nowrap;padding:5px">{esc(row["label"])}</th>']
        for period, _ in periods:
            absolute, relative = row["returns"][period], row["excess"][period]
            if compact:
                cells.append(f'<td style="text-align:center;padding:5px;background:{_color(absolute)}">'
                             f'{value(absolute, True)}<br><small style="color:#455b63">'
                             f'超额 {value(relative, True)}</small></td>')
            else:
                for number in (absolute, relative):
                    cells.append(f'<td style="text-align:right;padding:6px;background:{_color(number)}">'
                                 f'{value(number, True)}</td>')
        rows.append("<tr>" + "".join(cells) + "</tr>")
    heatmap = ('<div style="overflow-x:auto"><table aria-label="行业绝对涨跌与相对SPY表现" width="100%" '
               'cellspacing="2" cellpadding="0" style="font-size:12px;table-layout:fixed;'
               + ("min-width:300px" if compact else "min-width:590px") + '"><thead><tr>' + headings
               + "</tr></thead><tbody>" + "".join(rows) + "</tbody></table></div>")
    bars = []
    for point in panel.get("display_history", panel["history"]):
        number = finite(point["pct_20ma"])
        label = point["date"] + "：" + value(number, suffix="%")
        height = round(number * .55) if number is not None and 0 <= number <= 100 else 0
        bars.append(f'<td style="vertical-align:bottom;padding:0 1px;height:56px" title="{esc(label)}">'
                    + (f'<div style="height:{max(1, height)}px;background:#377e9e"></div>' if number is not None
                       else '<div style="height:2px;background:#b8c1c5"></div>') + "</td>")
    trend = ('<table role="img" aria-label="近21个交易日20日均线广度，缺失留空" width="100%" '
             'cellspacing="0" style="table-layout:fixed"><tr>' + "".join(bars) + "</tr></table>"
             + f'<p style="font-size:11px;color:#60737a;margin:4px 0">{esc(panel["history"][0]["date"])} 至 '
             + f'{esc(panel["market_date"])} · 柱高0-100%，末值 {value(panel["history"][-1]["pct_20ma"], suffix="%")} · 缺测留空</p>')
    rotation_note = rotation["label"] + ("：" + "、".join(rotation["from_sectors"]) + " → " + "、".join(rotation["to_sectors"])
                                        if rotation["state"] == "CANDIDATE" else "")
    spreads = panel["leadership"]["spreads"]
    lead = "；".join(f'{name}当日 {value(values["1d"], True)} 个百分点' for name, values in spreads.items())
    gaps = "；".join(panel["gaps"])
    return (f'<section id="internals" style="border-top:1px solid #d8e0e3;padding:24px 0;color:#293b40">'
            '<h2 style="font-size:20px;line-height:1.4;margin:0 0 8px">市场内部结构</h2>'
            f'<p style="font-size:16px;font-weight:600;margin:8px 0">{esc(panel["headline"])}</p>'
            f'<p style="font-size:13px">{esc(panel["interpretation"])}</p>'
            f'<p style="font-size:12px;color:#60737a">截至 {esc(panel["market_date"])} 收盘 · {esc(panel["scope_note"])}</p>'
            + distribution + f'<p style="font-size:12px">{esc(lead)}</p>'
            + '<h3 style="font-size:15px;margin:16px 0 8px">行业扩散与分化</h3>'
            + f'<p style="font-size:12px">有效行业 {s["valid_count"]}/11 · '
            + (f'上涨 {s["positive_count"]} / 下跌 {s["negative_count"]} / 平盘 {s["flat_count"]}' if s["valid_count"] else '涨跌分布待采')
            + f' · {esc(rotation_note)}</p>' + heatmap
            + f'<p style="font-size:11px;color:#60737a">{esc(rotation.get("history_note", ""))}</p>'
            + '<p style="font-size:11px;color:#60737a">超额 = 行业收益 - SPY收益；跑赢不等于上涨，不代表净资金流入。</p>'
            + f'<h3 style="font-size:15px;margin:16px 0 6px">20日均线广度：{esc(p["label"])}</h3>'
            + f'<p style="font-size:12px">20日均线广度5日 / 21日变化：{value(p["changes"]["5d"], True)} / '
            + f'{value(p["changes"]["21d"], True)} 个百分点。</p>' + trend
            + f'<p style="font-size:12px;color:#60737a">{esc(p["reason"])}</p>'
            + (f'<p style="font-size:12px;color:#946c22">待确认：{esc(gaps)}</p>' if gaps else "")
            + '<p style="font-size:11px;color:#60737a">扫描器日期按收盘后采集日标注，未获逐股源时间；收盘竞价覆盖未验证。'
            + f'{esc(panel["shadow_note"])}</p></section>')


def internals_image(report):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np
    from matplotlib import font_manager
    from .render import embedded_font

    _, font_path = embedded_font()
    font_manager.fontManager.addfont(font_path)
    with plt.rc_context({"font.family": [font_manager.FontProperties(fname=font_path).get_name(), "DejaVu Sans"],
                         "axes.unicode_minus": False}):
        panel = report["internals"]
        fig = plt.figure(figsize=(10.1, 6.8), layout="constrained")
        grid = fig.add_gridspec(2, 2, height_ratios=(1, 2.7), hspace=.13)
        ax, trend, heat = fig.add_subplot(grid[0, 0]), fig.add_subplot(grid[0, 1]), fig.add_subplot(grid[1, :])
        b = panel["breadth"]
        ax.set_title("有效价格样本：上涨 / 平盘 / 下跌", fontsize=11)
        if b["quality"] == "OK":
            left = 0
            for key, label, color in (("advancing", "上涨", "#258269"), ("unchanged", "平盘", "#87969d"),
                                      ("declining", "下跌", "#b54659")):
                pct = b[key + "_pct"]
                ax.barh([0], [pct], left=left, height=.45, color=color, label=f'{label} {b[key]}家')
                left += pct
            ax.legend(loc="lower center", bbox_to_anchor=(.5, -.35), ncol=3, fontsize=9, frameon=False)
        else:
            ax.text(.5, .5, "样本不足，不按平盘处理", transform=ax.transAxes, ha="center", fontsize=11)
        ax.set_xlim(0, 100)
        ax.set_ylim(-.5, .5)
        ax.set_yticks([])
        ax.set_xticks([0, 25, 50, 75, 100], ["0%", "25%", "50%", "75%", "100%"])
        ax.spines[["top", "left", "right"]].set_visible(False)
        points = panel.get("display_history", panel["history"])
        for key, label, color in (("pct_20ma", "20日均线广度（动态样本参考）", "#377e9e"),
                                   ("advancing_pct", "当日上涨比例（新口径）", "#258269")):
            values = [p[key] if p[key] is not None else np.nan for p in points]
            trend.plot(range(len(points)), values, label=label, color=color, linewidth=1.6, marker=".", markersize=3)
        trend.set_ylim(0, 100)
        trend.set_xticks([0, 10, 20], [points[i]["date"][5:] for i in (0, 10, 20)])
        trend.set_title("近21个交易日：缺测断线", fontsize=11)
        trend.legend(fontsize=8, frameon=False, loc="lower left")
        trend.grid(axis="y", alpha=.2)
        trend.spines[["top", "right"]].set_visible(False)
        raw = [[r[k][p] for p in ("1d", "5d", "21d") for k in ("returns", "excess")]
               for r in panel["sectors"]["rows"]]
        array = np.array([[v if v is not None else np.nan for v in row] for row in raw])
        from matplotlib.colors import LinearSegmentedColormap
        cmap = LinearSegmentedColormap.from_list("internals", ["#e9b2bb", "#f5f7f7", "#a7d7c4"]).with_extremes(bad="#e2e7ea")
        heat.imshow(array / np.array([2, 1, 5, 3, 10, 5]), vmin=-1, vmax=1, cmap=cmap, aspect="auto")
        heat.set_yticks(range(11), [r["label"] for r in panel["sectors"]["rows"]], fontsize=10)
        heat.set_xticks(range(6), ["1日涨跌%", "1日超额百分点", "5日涨跌%", "5日超额百分点", "21日涨跌%", "21日超额百分点"], fontsize=9)
        heat.xaxis.tick_top()
        heat.tick_params(length=0)
        for i, row in enumerate(raw):
            for j, number in enumerate(row):
                heat.text(j, i, value(number, True), ha="center", va="center", fontsize=10, color="#22383e")
        heat.set_title("行业绝对涨跌与相对SPY表现（各窗口独立色阶）", fontsize=11, pad=30)
        for spine in heat.spines.values():
            spine.set_visible(False)
        out = BytesIO()
        fig.savefig(out, format="png", dpi=170, facecolor="white")
        plt.close(fig)
        out.seek(0)
        return out
