"""Separate macro conditions from market internals without inventing a total score."""
from datetime import date, timedelta
import math

from liquidity_monitor import CREDIT_STRESS_GUARDS, FUNDING_STRESS_GUARDS
from .model import finite, sessions
from .states import IMPULSE_THRESHOLD, STATE_COLORS

DOMAIN_VERSION = "macro_micro_direction_v1.0-shadow"
# Presentation noise floors in native units, not calibrated risk/trading thresholds.
# key: adverse sign, short-window floor, medium-window floor, valid value range
METRICS = {
    "net_liquidity": (-1, 5.0, 10.0, None, None),
    "reserves": (-1, 5.0, 10.0, 0, None),
    "sofr_iorb": (1, 1.0, 2.0, None, None),
    "effr_iorb": (1, 1.0, 2.0, None, None),
    "sofr_tail": (1, 2.0, 4.0, 0, None),
    "ig": (1, 5.0, 10.0, 0, None),
    "hy": (1, 10.0, 20.0, 0, None),
    "pct_20ma": (-1, 3.0, 6.0, 0, 100),
    "pct_50ma": (-1, 3.0, 6.0, 0, 100),
    "pct_200ma": (-1, 2.0, 4.0, 0, 100),
    "spy_vwap_time_acceptance_pct": (-1, 5.0, 10.0, 0, 100),
    "qqq_vwap_time_acceptance_pct": (-1, 5.0, 10.0, 0, 100),
}
GROUPS = {
    "macro": (
        ("quantity", "数量与缓冲", ("net_liquidity", "reserves"), "1周 / 4周"),
        ("funding", "融资价格", ("sofr_iorb", "effr_iorb", "sofr_tail"), "5日 / 21日"),
        ("credit", "信用传导", ("ig", "hy"), "5日 / 21日"),
    ),
    "micro": (
        ("breadth", "市场广度", ("pct_20ma", "pct_50ma", "pct_200ma"), "5日 / 21日"),
        ("acceptance", "价格承接", ("spy_vwap_time_acceptance_pct",
                                  "qqq_vwap_time_acceptance_pct"), "5日 / 21日"),
        ("risk_capital", "风险资本扩散", (), "5日 / 21日"),
    ),
}
NAMES = {
    "macro": {"UP": "边际趋紧", "DOWN": "边际趋松", "FLAT": "变化不明显",
              "MIXED": "松紧分化", "UNKNOWN": "证据不足",
              "EARLY_UP": "局部趋紧", "EARLY_DOWN": "局部趋松"},
    "micro": {"UP": "内部压力增加", "DOWN": "承接扩散改善", "FLAT": "变化不明显",
              "MIXED": "内部分化", "UNKNOWN": "证据不足",
              "EARLY_UP": "局部压力增加", "EARLY_DOWN": "局部承接改善"},
}
GROUP_NAMES = {"UP": "压力增加", "DOWN": "压力减轻", "FLAT": "变化不明显",
               "MIXED": "内部背离", "UNKNOWN": "待确认"}
GROUP_DIRECTIONS = {
    "quantity": {"UP": "支持减少", "DOWN": "支持增加"},
    "funding": {"UP": "融资趋紧", "DOWN": "融资缓解"},
    "credit": {"UP": "利差走阔", "DOWN": "利差收窄"},
    "breadth": {"UP": "参与转弱", "DOWN": "参与改善"},
    "acceptance": {"UP": "承接转弱", "DOWN": "承接改善"},
    "risk_capital": {"UP": "扩散减弱", "DOWN": "扩散增强"},
}
COLORS = {**STATE_COLORS, "MIXED": "#9c6812", "EARLY_UP": "#9c6812",
          "EARLY_DOWN": "#9c6812"}


def _direction(delta, threshold):
    if delta is None:
        return "UNKNOWN"
    return "UP" if delta >= threshold else "DOWN" if delta <= -threshold else "FLAT"


def _combine(directions, domain=False):
    """Require all mandatory families; disagreement cannot be averaged away."""
    if not directions or "UNKNOWN" in directions:
        return "UNKNOWN"
    if "MIXED" in directions or ("UP" in directions and "DOWN" in directions):
        return "MIXED"
    for direction in ("UP", "DOWN"):
        count = directions.count(direction)
        if count:
            return "EARLY_" + direction if domain and count < 2 else direction
    return "FLAT"


def _window(metric, market_date, count, spec):
    adverse, _, _, low, high = spec
    result = {"delta": None, "pressure_delta": None, "direction": "UNKNOWN",
              "start": None, "end": metric.get("source_date"),
              "reason": "当前数据不合格"}
    if metric.get("status") != "OK" or metric.get("acquisition") != "OK":
        return result
    end = metric.get("source_date")
    current = finite(metric.get("value"))
    if not end or current is None:
        return result
    if metric["frequency"] == "weekly":
        grid = [str(date.fromisoformat(end) - timedelta(days=7 * i))
                for i in range(count, -1, -1)]
    else:
        if end != market_date:
            return result
        grid = sessions(date.fromisoformat(end) - timedelta(days=70), end)[-count - 1:]
    points = {p["date"]: finite(p["value"]) for p in metric.get("history", [])}
    values = [points.get(d) for d in grid]
    valid = lambda v: (v is not None and (low is None or v >= low)
                       and (high is None or v <= high))
    if (len(grid) != count + 1 or not all(valid(v) for v in values)
            or not math.isclose(current, values[-1], rel_tol=0, abs_tol=1e-9)):
        result["reason"] = "连续窗口缺失、数值越界或当前值与历史不一致"
        return result
    delta = round(values[-1] - values[0], 8)
    result.update(delta=delta, pressure_delta=delta * adverse,
                  start=grid[0], reason="完整原生频率窗口；非独立资金流量")
    return result


def metric_evidence(report, key):
    metric = report["metrics"].get(key, {})
    spec = METRICS[key]
    windows = (1, 4) if metric.get("frequency") == "weekly" else (5, 21)
    result = {"key": key, "label": metric.get("label", key),
              "source_date": metric.get("source_date"), "unit": metric.get("unit"),
              "value": metric.get("value"), "source": metric.get("source"),
              "acquisition": metric.get("acquisition")}
    for period, count, threshold in zip(("short", "medium"), windows, spec[1:3]):
        entry = _window(metric, report["market_date"], count, spec)
        entry.update(threshold=threshold,
                     direction=_direction(entry["pressure_delta"], threshold))
        result[period] = entry
    return result


def _capital_evidence(report):
    panel = report["states"]["panels"]["risk_capital"]
    result = {"key": "risk_capital", "label": "风险资本收缩压力",
              "source_date": panel["source_date"], "value": panel["risk_value"],
              "unit": "点", "source": panel["source_version"]}
    for period, window in (("short", "5d"), ("medium", "21d")):
        delta = panel["changes"][window]
        result[period] = {"delta": delta, "pressure_delta": delta,
                          "direction": _direction(delta, IMPULSE_THRESHOLD),
                          "threshold": IMPULSE_THRESHOLD, "end": panel["source_date"],
                          "reason": panel["reason"]}
    return result


def _level_flags(report, domain):
    flags = []
    metrics = report["metrics"]
    if domain == "macro":
        for key, guard in (("sofr_iorb", "sofr_iorb_bps"), ("effr_iorb", "effr_iorb_bps"),
                           ("sofr_tail", "sofr_dispersion_bps")):
            item = metrics[key]
            value = finite(item.get("value"))
            if (item["status"] == item["acquisition"] == "OK" and value is not None
                    and value > FUNDING_STRESS_GUARDS[guard]):
                flags.append(f'{item["label"]}超过融资绝对观察线')
        for key, guard in (("ig", "ig_spread"), ("hy", "hy_spread")):
            item = metrics[key]
            value = finite(item.get("value"))
            if (item["status"] == item["acquisition"] == "OK" and value is not None
                    and value > 100 * CREDIT_STRESS_GUARDS[guard]):
                flags.append(f'{item["label"]}超过信用绝对观察线')
    else:
        for key, label in (("pct_200ma", "200日线上股票不足半数"),
                           ("spy_vwap_time_acceptance_pct", "SPY均价线上时间不足半数"),
                           ("qqq_vwap_time_acceptance_pct", "QQQ均价线上时间不足半数")):
            item = metrics[key]
            value = finite(item.get("value"))
            if item["status"] == item["acquisition"] == "OK" and value is not None and 0 <= value < 50:
                flags.append(label)
        if report["states"]["panels"]["risk_capital"]["zone"] == "HIGH":
            flags.append("风险资本扩散仍偏弱")
    return flags


def build_domains(report):
    domains = {}
    for name, specs in GROUPS.items():
        groups = []
        for key, label, metrics, window_label in specs:
            evidence = ([_capital_evidence(report)] if key == "risk_capital"
                        else [metric_evidence(report, metric) for metric in metrics])
            group = {"key": key, "label": label, "windows": window_label, "evidence": evidence}
            for period in ("short", "medium"):
                direction = _combine([e[period]["direction"] for e in evidence])
                direction_label = GROUP_DIRECTIONS[key].get(direction, GROUP_NAMES[direction])
                group[period] = {"direction": direction, "label": direction_label,
                                 "color": COLORS[direction]}
            groups.append(group)
        domain = {"key": name, "label": "宏观资金与融资" if name == "macro" else "微观市场承接",
                  "groups": groups, "flags": _level_flags(report, name),
                  "level_note": "不设置新的总水位；无分项提示不等于环境宽松"}
        for period in ("short", "medium"):
            directions = [g[period]["direction"] for g in groups]
            direction = _combine(directions, domain=True)
            domain[period] = {"direction": direction, "label": NAMES[name][direction],
                              "color": COLORS[direction],
                              "ready_groups": sum(d != "UNKNOWN" for d in directions),
                              "total_groups": len(groups)}
        domain["disagreement"] = (
            domain["short"]["direction"] in ("UP", "DOWN")
            and domain["medium"]["direction"] in ("UP", "DOWN")
            and domain["short"]["direction"] != domain["medium"]["direction"])
        domains[name] = domain
    macro, micro = (domains[name]["short"]["direction"] for name in ("macro", "micro"))
    if macro == "DOWN" and micro == "UP":
        relation = "宏观边际改善，微观仍在转弱；尚未看到市场承接同步改善。"
    elif macro == "UP" and micro == "DOWN":
        relation = "微观承接改善，但宏观趋紧；不能把承接改善等同于资金宽松。"
    elif macro == micro and macro in ("UP", "DOWN"):
        relation = "宏观与微观同向恶化。" if macro == "UP" else "宏观与微观同向改善；不代表已处于低压。"
    elif "UNKNOWN" in (macro, micro):
        relation = "宏微观证据尚未齐备，暂停两层联判；缺失不代表平稳。"
    else:
        relation = "宏微观尚未形成完整同向变化；分别观察分项，不以多数掩盖分歧。"
    return {
        "version": DOMAIN_VERSION, **domains, "relation": relation,
        "headline": f'宏观：{domains["macro"]["short"]["label"]}｜微观：{domains["micro"]["short"]["label"]}',
        "scope_note": "宏观是资金与金融条件，非经济增长预测；微观是市场内部，非订单簿流动性。"
                      "分组方向为影子规则，不是经回测的交易信号，不生成新总评分。",
    }
