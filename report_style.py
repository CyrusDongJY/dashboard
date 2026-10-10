# -*- coding: utf-8 -*-
"""Shared Chinese typography for the emailed PNG reports."""
from pathlib import Path
import os
import textwrap


def configure_chinese_font():
    import matplotlib
    from matplotlib import font_manager

    candidates = [
        os.getenv("REPORT_FONT_PATH"),
        str(Path.home() / ".local/share/fonts/NotoSansCJKsc-Regular.otf"),
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            font_manager.fontManager.addfont(candidate)
            bold = Path(candidate).with_name("NotoSansCJKsc-Bold.otf")
            if bold.is_file():
                font_manager.fontManager.addfont(str(bold))
            name = font_manager.FontProperties(fname=candidate).get_name()
            break
    else:
        names = {font.name for font in font_manager.fontManager.ttflist}
        name = next((name for name in (
            "Noto Sans CJK SC", "Source Han Sans SC", "PingFang SC",
            "Microsoft YaHei", "SimHei", "Heiti TC", "Songti SC",
        ) if name in names), None)
    if name is None:
        raise RuntimeError(
            "报告缺少中文字体：安装 Noto Sans CJK SC，或设置 REPORT_FONT_PATH")
    matplotlib.rcParams["font.family"] = [name, "DejaVu Sans"]
    matplotlib.rcParams["axes.unicode_minus"] = False
    return name


def wrap_cn(text, width=22):
    return "\n".join(textwrap.wrap(str(text), width=width))


COMPONENT_LABELS_CN = {
    "Net liquidity level": "净流动性存量",
    "Reserve buffer": "准备金缓冲",
    "M2 YoY growth": "M2同比增速",
    "Net liquidity 20D change": "净流动性20日变化",
    "TGA 5D change": "财政账户5日变化",
    "Fed assets 20D change": "美联储资产20日变化",
    "SOFR - IORB": "SOFR与准备金利率差",
    "TGCR - IORB": "TGCR与准备金利率差",
    "EFFR - IORB": "EFFR与准备金利率差",
    "SOFR 75th - IORB": "SOFR高分位利差",
    "SOFR 99th - median": "SOFR尾部离散度",
    "Chicago Fed NFCI": "金融条件指数",
    "MOVE": "债市波动率",
    "HY OAS": "高收益债利差",
    "IG OAS": "投资级债利差",
    "HYG/TLT 21D": "高收益债相对长债21日表现",
    "Above 20DMA": "站上20日均线占比",
    "Above 50DMA": "站上50日均线占比",
    "Above 200DMA": "站上200日均线占比",
    "Daily advancers": "当日上涨家数占比",
    "Cap-weight gap": "市值加权集中度",
    "VIX futures contango": "VIX期货期限结构",
    "VIX/VIX3M contango": "VIX现货期限结构",
    "VVIX": "波动率的波动率",
    "SKEW": "尾部偏斜",
}


def component_note_cn(note):
    for label in sorted(COMPONENT_LABELS_CN, key=len, reverse=True):
        if note.startswith(label + " "):
            return f"{COMPONENT_LABELS_CN[label]}：{note[len(label):].strip()}分"
    return note
