from base64 import b64encode
from copy import deepcopy
from hashlib import sha256
import json
import re
from unittest.mock import Mock

from liquidity_digest.interactive import assets, chart_payload
from liquidity_digest.model import plan, assemble
from liquidity_digest.sample import sample_report
from liquidity_digest.sources import fetch_table


def test_ten_charts_share_bounded_frozen_range_without_mutating_report():
    report = sample_report(plan("2026-10-20"))
    original = deepcopy(report)
    payload = chart_payload(report)
    assert len(payload["charts"]) == 10
    assert len({c["id"] for c in payload["charts"]}) == 10
    assert payload["end"] == report["market_date"]
    assert payload["default_days"] == 150
    assert report == original
    for chart in payload["charts"]:
        assert all(p[0] <= payload["end"] for s in chart["series"] for p in s["points"])
    for chart in payload["charts"][6:]:
        assert chart["y_bounds"] == [0, 100]
        assert [g["value"] for g in chart["guides"]] == [65, 35]


def test_missing_sessions_remain_null_not_interpolated_or_forward_filled():
    report = sample_report(plan("2026-10-20"))
    item = report["frontpage"]["indices"]["core_funding"]
    item["history"] = [{"date": "2026-10-14", "value": 0}, {"date": "2026-10-16", "value": 42}]
    points = dict(chart_payload(report)["charts"][0]["series"][-1]["points"])
    assert points["2026-10-14"] == 0
    assert points["2026-10-15"] is None
    assert points["2026-10-16"] == 42
    assert "2026-10-17" not in points
    assert points["2026-10-19"] is None


def test_series_versions_and_replay_remain_separate():
    report = sample_report(plan("2026-10-20"))
    item = report["frontpage"]["indices"]["liquidity"]
    item["legacy_segments"] = [dict(version="old-a", points=[dict(date="2026-10-01", value=20)]),
                               dict(version="old-b", points=[dict(date="2026-10-02", value=80)])]
    series = chart_payload(report)["charts"][1]["series"]
    assert len(series) == 4
    assert "old-a" in series[0]["label"] and "old-b" in series[1]["label"]
    assert series[2]["dashed"] and not series[3]["dashed"]


def test_csp_hashes_exact_inline_scripts_and_escapes_embedded_json():
    report = sample_report(plan("2026-10-20"))
    report["frontpage"]["indices"]["core_funding"]["label"] = '</script><img src=x onerror=alert(1)>'
    policy, markup = assets(report)
    scripts = re.findall(r'<script[^>]*>(.*?)</script>', markup, re.S)
    assert len(scripts) == 2
    assert "<img" not in scripts[0] and "\\u003c" in scripts[0]
    assert json.loads(scripts[0])["charts"][0]["title"].startswith("</script>")
    for script in scripts:
        assert "'sha256-" + b64encode(sha256(script.encode()).digest()).decode() + "'" in policy
    assert "unsafe-inline" not in policy and "connect-src 'none'" in policy
    assert "eval(" not in scripts[1] and "innerHTML" not in scripts[1] and "fetch(" not in scripts[1]


def test_empty_report_has_no_fabricated_points():
    payload = chart_payload(assemble(plan("2026-10-20"), {}))
    assert not [p for c in payload["charts"] for s in c["series"] for p in s["points"] if p[1] is not None]


def test_index_history_fetch_extends_to_430_days_with_pagination():
    session = Mock()
    session.get.return_value.json.side_effect = [[{"report_date": "2026-05-01"}], []]
    rows = fetch_table(session, {"SUPABASE_URL": "https://example.test", "SUPABASE_KEY":"dummy"},
                       "core_funding_daily", "2020-01-01", "2026-10-28")
    assert len(rows) == 1
    params = session.get.call_args_list[0].kwargs["params"]
    assert "2025-08-24" in params["and"]
    assert session.get.call_args_list[1].kwargs["params"]["offset"] == 1
