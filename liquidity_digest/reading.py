"""Presentation-only views shared by email, HTML and PDF."""
from .model import finite

COMPONENT_LABELS = (("vix", "VIX"), ("move", "MOVE"), ("credit_spread", "信用利差"),
                    ("trin", "TRIN代理"), ("pcr", "PCR"), ("cmf", "CMF"))


def tactical_view(report):
    row = report["panels"].get("tactical") or {}
    components = row.get("stress_components") or {}
    if not isinstance(components, dict):
        components = {}
    meta = components.get("_meta") or {}
    score = finite(row.get("eod_stress_score")) if meta.get("status") in ("OK", "PARTIAL") else None
    temporal = report["states"]["panels"]["tactical_stress"]
    history = report.get("tactical_history") or {}
    changes = dict(history.get("changes", temporal["changes"])) if score is not None else dict.fromkeys(("1d", "5d", "21d"))
    window = history.get("windows", {}).get("5d", {})
    missing = window.get("missing_dates", [])
    continuity = ("待确认：缺测 " + "、".join(missing) if missing else
                  "5日窗口完整；净变化不等于逐日连续升压" if window.get("available") == window.get("required") == 6 else
                  "待确认：同口径连续历史不足")
    if score is None:
        continuity = "待确认：" + ("昨日收盘记录缺失，当前评分不可用" if not row else "当前评分不可用") + "；不使用旧分数替代"
    rows = []
    for key, label in COMPONENT_LABELS:
        part = components.get(key) or {}
        valid = part.get("status") == "OK"
        rows.append({"label": label, "value": finite(part.get("value")),
                     "contribution": finite(part.get("contribution")) if valid else None,
                     "date": part.get("source_date") or "未取得",
                     "status": "可用" if valid else "不可用",
                     "source": part.get("source_name") or "未记录"})
    return {"score": score, "changes": changes, "continuity": continuity, "components": rows,
            "direction": history.get("direction_cn", temporal["direction_cn"]) if score is not None else "方向待确认",
            "change_basis": "原始同版本端点净变化" if history else "原影子规则窗口变化",
            "context": components.get("_context") or {},
            "coverage": finite(row.get("stress_coverage")),
            "confidence": finite(row.get("stress_confidence")),
            "status": {"OK": "可用", "PARTIAL": "部分覆盖"}.get(meta.get("status"), "不可计算"),
            "version": row.get("stress_calc_version") or "未记录",
            "date": report["market_date"] if row else "未取得"}


def pressure_identity(report):
    pressure = report["states"].get("display_pressure", report["states"]["pressure"])
    label = "综合环境压力" if pressure["panel"] == "environment" else "跨资产战术压力代理"
    return "压力水位与方向定位", "本图采用：" + label


def alert_groups(front):
    """Never collapse severe observations; show up to three other notices."""
    critical = [a for a in front["alerts"] if a["kind"] in ("raw_guard", "shock", "pressure")]
    other = [a for a in front["alerts"] if a["kind"] not in ("raw_guard", "shock", "pressure")]
    count = max(0, 3 - len(critical))
    return critical + other[:count], other[count:]
