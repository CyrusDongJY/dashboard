"""Observed fund allocation, futures positioning and margin balances.

These series are supplementary facts, never inputs to the existing scores.
Current-vintage history is not a historical point-in-time publication archive.
"""
from datetime import date, datetime, timedelta
from copy import deepcopy
from hashlib import sha256
from io import StringIO
import json

import pandas as pd
from lxml import html

from .model import NY, UTC, finite, stamp

VERSION = "capital_observation_v1.0-shadow"
CFTC_URL = "https://publicreporting.cftc.gov/resource/gpe5-46if.json"
ICI_URL = "https://www.ici.org/research/stats/combined_flows"
FINRA_URL = "https://www.finra.org/rules-guidance/key-topics/margin-accounts/margin-statistics"
CONTRACTS = {"20974+": "纳指100合并期货", "13874+": "标普500合并期货", "1170E1": "VIX期货"}
CATEGORIES = {"asset_mgr": "资产管理机构", "lev_money": "杠杆基金"}
SERIES = {
    "ici_domestic": ("美国股票基金净申赎", "ici_flows", "weekly", "百万美元", "FUND_ALLOCATION"),
    "finra_debit": ("证券保证金融资余额", "finra_margin", "monthly", "百万美元", "MARGIN_BALANCE"),
    **{f"cot_{code}_{category}": (label + " / " + category_label, "cftc_positions", "weekly", "合约", "FUTURES_POSITION")
       for code, label in CONTRACTS.items() for category, category_label in CATEGORIES.items()},
}
STATUS_LABELS = {"OK": "本次已核验", "MISSING": "尚无观测", "STALE": "超过更新观察窗",
                 "FETCH_ERROR": "采集失败", "ACCESS_DENIED": "官方源拒绝程序访问",
                 "DEADLINE": "截止前未完成", "CACHED": "旧采集，仅供查阅",
                 "CACHED_AFTER_ERROR": "刷新失败，保留旧抓取", "INVALID": "字段或日期不合格"}


def numeric(value):
    if isinstance(value, str):
        value = value.strip().replace(",", "").replace("\N{MINUS SIGN}", "-")
        if value.startswith("(") and value.endswith(")"):
            value = "-" + value[1:-1]
    return finite(value)


def record(series, day, value, acquired_at, url, digest, details=None):
    label, _, frequency, unit, family = SERIES[series]
    return dict(series=series, label=label, observation_date=str(date.fromisoformat(day)),
                value=numeric(value), frequency=frequency, unit=unit, evidence_family=family,
                acquired_at=stamp(acquired_at).isoformat(), available_at=stamp(acquired_at).isoformat(),
                released_at=None, availability_basis="ACTUAL_RECEPTION_NOT_ASSUMED_RELEASE",
                history_basis="CURRENT_VINTAGE_NOT_PIT", source_url=url,
                definition_version=VERSION, source_digest=digest, details=details or {})


def parse_cftc(rows, acquired_at):
    if not isinstance(rows, list):
        raise ValueError("CFTC response is not a table")
    digest = sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest()
    result, seen = [], set()
    for row in rows:
        code = row.get("cftc_contract_market_code")
        if code not in CONTRACTS or row.get("futonly_or_combined") != "FutOnly":
            continue
        day = str(row.get("report_date_as_yyyy_mm_dd", ""))[:10]
        if (code, day) in seen:
            raise ValueError("Duplicate CFTC contract/date")
        seen.add((code, day))
        oi = numeric(row.get("open_interest_all"))
        for category in CATEGORIES:
            def field(side):
                key = category + "_positions_" + side
                return numeric(row.get(key, row.get(key + "_all")))
            long, short, spreading = field("long"), field("short"), field("spread")
            valid = (oi is not None and oi > 0 and all(
                v is not None and 0 <= v <= oi for v in (long, short, spreading))
                and long + spreading <= oi and short + spreading <= oi)
            net = long - short if valid else None
            result.append(record(f"cot_{code}_{category}", day, net, acquired_at, CFTC_URL, digest,
                dict(contract_code=code, category=CATEGORIES[category], report_basis="FutOnly",
                     contract_units=row.get("contract_units"), long=long, short=short,
                     spreading=spreading, open_interest=oi,
                     net_oi_pct=net / oi * 100 if valid else None,
                     field_status="OK" if valid else "INCOMPLETE_OR_INVALID",
                     interpretation="净持仓不是现货买卖金额；分类可能含套保、套利及分类调整")))
    return result


def parse_ici(document, acquired_at):
    tree = html.fromstring(document)
    if "millions of dollars" not in " ".join(tree.text_content().lower().split()):
        raise ValueError("ICI units not verified")
    # Preserve footnotes as evidence, but never turn their text into instructions.
    notes = [" ".join(node.text_content().split()) for node in tree.xpath("//p")
             if "collective investment trust" in node.text_content().lower()]
    digest = sha256(document.encode()).hexdigest()
    tables = pd.read_html(StringIO(document), flavor="lxml")
    for table in tables:
        labels = table.iloc[:, 0].astype(str).str.strip().str.lower()
        domestic = table[labels.eq("domestic")]
        if len(domestic) != 1:
            continue
        result = []
        for column in table.columns[1:]:
            parts = column if isinstance(column, tuple) else (column,)
            days = [pd.to_datetime(p, format="%m/%d/%Y", errors="coerce") for p in parts]
            day = next((d for d in days if pd.notna(d)), None)
            if day is None:
                continue
            result.append(record("ici_domestic", str(day.date()), domestic.iloc[0][column],
                acquired_at, ICI_URL, digest,
                {"estimate": True, "structural_note_present": bool(notes),
                 "source_notes": notes, "revision_policy": "SOURCE_CURRENT_REVISED_VINTAGE",
                 "interpretation": "共同基金现金净流量与ETF净发行的周估计；不得再加样本ETF流量"}))
        if result:
            return result
    raise ValueError("ICI Domestic weekly table not found")


def parse_finra(document, acquired_at):
    if "millions" not in html.fromstring(document).text_content().lower():
        raise ValueError("FINRA units not verified")
    digest = sha256(document.encode()).hexdigest()
    for table in pd.read_html(StringIO(document), flavor="lxml"):
        names = {str(c): c for c in table.columns}
        month = next((c for s, c in names.items() if s == "Month/Year"), None)
        debt = next((c for s, c in names.items() if s.startswith("Debit Balances")), None)
        cash = next((c for s, c in names.items() if s.startswith("Free Credit") and "Cash Accounts" in s), None)
        margin = next((c for s, c in names.items() if s.startswith("Free Credit") and "Margin Accounts" in s), None)
        if month is None or debt is None or cash is None or margin is None:
            continue
        result = []
        for _, row in table.iterrows():
            period = pd.to_datetime(row[month], format="%b-%y", errors="coerce")
            if pd.isna(period):
                continue
            day = str((period + pd.offsets.MonthEnd(0)).date())
            value = numeric(row[debt])
            result.append(record("finra_debit", day, value if value is not None and value >= 0 else None,
                acquired_at, FINRA_URL, digest,
                dict(period=day[:7], date_basis="MONTH_LABEL_NOT_EXACT_BUSINESS_DAY",
                     cash_free_credit_m=numeric(row[cash]), margin_free_credit_m=numeric(row[margin]),
                     interpretation="月末存量，通常次月第三周发布；变化不是当日股票净流入，统计方法调整也可影响余额")))
        if result:
            return result
    raise ValueError("FINRA monthly table not found")


def fetch_observations(session, source):
    url = {"cftc_positions": CFTC_URL, "ici_flows": ICI_URL, "finra_margin": FINRA_URL}[source]
    params = None
    if source == "cftc_positions":
        codes = ",".join("'" + code + "'" for code in CONTRACTS)
        params = {"$where": f"cftc_contract_market_code in ({codes}) AND futonly_or_combined='FutOnly'",
                  "$order": "report_date_as_yyyy_mm_dd DESC,cftc_contract_market_code ASC", "$limit": 600}
    response = session.get(url, params=params)
    if response.status_code in (401, 403):
        raise PermissionError("Official source denied automated access")
    response.raise_for_status()
    acquired = datetime.now(UTC).isoformat()
    parser = {"cftc_positions": parse_cftc, "ici_flows": parse_ici, "finra_margin": parse_finra}[source]
    return parser(response.json() if source == "cftc_positions" else response.text, acquired)


def build_capital(report, inputs):
    cutoff, end = stamp(report["cutoff"]), date.fromisoformat(report["market_date"])
    observations = []
    for key, (label, source, frequency, unit, family) in SERIES.items():
        capture = inputs.get(source, {})
        status = capture.get("status", "MISSING")
        item = dict(key=key, label=label, unit=unit, frequency=frequency,
                    evidence_family=family, status=status, value=None, observation_date=None,
                    changes={}, details={}, history=[], available_at=None, acquired_at=None,
                    source_url={"ici_flows": ICI_URL, "finra_margin": FINRA_URL, "cftc_positions": CFTC_URL}[source])
        rows = {}
        for row in capture.get("data", []):
            if not isinstance(row, dict):
                continue
            try:
                if (row.get("series") != key or row.get("definition_version") != VERSION
                        or row.get("unit") != unit or row.get("frequency") != frequency
                        or row.get("evidence_family") != family
                        or stamp(row["available_at"]) > cutoff or stamp(row["acquired_at"]) > cutoff
                        or stamp(row["available_at"]) != stamp(row["acquired_at"])
                        or stamp(row["acquired_at"]) > stamp(capture["captured_at"])
                        or stamp(capture["captured_at"]) > cutoff
                        or date.fromisoformat(row["observation_date"]) > end):
                    continue
            except (KeyError, TypeError, ValueError):
                continue
            rows[row["observation_date"]] = row
        if rows:
            day = max(rows)
            latest = rows[day]
            value = numeric(latest.get("value"))
            age = (end - date.fromisoformat(day)).days
            if status == "OK" and stamp(capture["captured_at"]).astimezone(NY).date() != cutoff.astimezone(NY).date():
                status = "CACHED"
            if status == "OK":
                status = "INVALID" if value is None else "STALE" if age > (15 if frequency == "weekly" else 65) else "OK"
            item.update(value=value, observation_date=day, details=deepcopy(latest.get("details", {})),
                        status=status, acquired_at=latest["acquired_at"], available_at=latest["available_at"],
                        source_digest=latest.get("source_digest"), history=list(rows.values()))
            for count in ((1, 4) if frequency == "weekly" else (1, 3)):
                previous = str((pd.Timestamp(day) - (pd.DateOffset(weeks=count) if frequency == "weekly"
                               else pd.DateOffset(months=count)) + (pd.offsets.MonthEnd(0) if frequency == "monthly" else pd.Timedelta(0))).date())
                prior = numeric(rows.get(previous, {}).get("value"))
                suffix = f"{count}{'w' if frequency == 'weekly' else 'm'}"
                item["changes"][suffix] = value - prior if value is not None and prior is not None else None
                if key.startswith("cot_") and count == 1:
                    previous_details = rows.get(previous, {}).get("details", {})
                    for field in ("long", "short", "spreading", "open_interest"):
                        old = numeric(previous_details.get(field))
                        new = numeric(item["details"].get(field))
                        item["details"][field + "_change_1w"] = new - old if old is not None and new is not None else None
            if key == "ici_domestic":
                window = [str(date.fromisoformat(day) - timedelta(weeks=i)) for i in range(4)]
                values = [numeric(rows.get(d, {}).get("value")) for d in window]
                item["sum_4w"] = sum(values) if all(v is not None for v in values) else None
                item["details"]["four_week_interpretation"] = "同一次抓取版本；含结构调整时不认定风险偏好方向"
        item["status_cn"] = STATUS_LABELS.get(item["status"], "待核查")
        item["state_cn"] = "方向待确认"
        if item["status"] == "OK" and item["value"] is not None:
            if key == "ici_domestic":
                item["state_cn"] = ("结构活动干扰，暂停方向归因" if item["details"].get("structural_note_present") else
                                    "本周净申购" if item["value"] > 0 else "本周净赎回" if item["value"] < 0 else "本周净额为零")
            else:
                delta = item["changes"].get("1m" if frequency == "monthly" else "1w")
                if delta is not None:
                    item["state_cn"] = ("月度融资余额" if frequency == "monthly" else "周度净持仓") + (
                        "增加" if delta > 0 else "减少" if delta < 0 else "不变")
        observations.append(item)
    return {"version": VERSION, "observations": observations,
            "score_eligible": False, "decision_eligible": False,
            "etf_status": "NOT_VERIFIED", "etf_note": "发行人同日份额、NAV及公司行动尚未核验；未启用ETF资金流估计",
            "scope_note": "申赎、持仓、融资余额分别观察，不相加，不等于全市场资金净流入；慢频重复展示不算新增样本。"}


def capital_lines(capital):
    lines = []
    for row in capital.get("observations", []):
        if row["key"].startswith("cot_") and not row["key"].startswith("cot_20974+"):
            continue
        value = "不可用" if row["value"] is None else f'{row["value"]:,.0f} {row["unit"]}'
        periods = {"1w": "较上周", "4w": "较4周前", "1m": "较上月", "3m": "较3月前"}
        changes = " / ".join(f'{periods[k]} {v:+,.0f}' if v is not None else f'{periods[k]} 待积累'
                             for k, v in row["changes"].items() if row["key"] != "ici_domestic" or k == "1w")
        if row["key"] == "ici_domestic":
            total = row.get("sum_4w")
            changes += "；近4周净额合计 " + (f"{total:+,.0f} 百万美元" if total is not None else "待积累")
        lines.append(f'{row["label"]}：{row["state_cn"]}；{value}；{row["status_cn"]}；观测 {row["observation_date"] or "未取得"}；{changes}')
        if row["details"].get("structural_note_present"):
            lines.append("ICI本次发布含结构性活动说明，原值保留，不直接解释为投资者撤资。")
        if row["details"].get("acquisition_method") == "OFFICIAL_PAGE_VERIFIED_IMPORT":
            lines.append("ICI数据为官方页面单次核验补录，自动抓取尚未恢复；按实际补录时间可用，不回填为当时已知。")
    return lines
