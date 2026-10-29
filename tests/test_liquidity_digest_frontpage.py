from copy import deepcopy
from datetime import timedelta
from email.message import EmailMessage
from io import BytesIO
from base64 import b64decode, b64encode
from unittest.mock import Mock

import pytest

from liquidity_digest.frontpage import (CORE_VERSION, LIQUIDITY_VERSION, build_frontpage,
                                       index_observation, front_html)
from liquidity_digest.model import plan, sessions, stamp
from liquidity_digest.sample import sample_report
from liquidity_digest.sources import fetch_table
from liquidity_digest.email_images import add_html_with_images


P = plan("2026-10-23")
PNG = b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a1ioAAAAASUVORK5CYII=")


def capture(key="core_funding", count=25):
    dates = sessions("2026-08-01", P["market_date"])[-count:]
    rows = []
    for i, day in enumerate(dates):
        timestamp = day + "T17:00:00-04:00"
        if key == "core_funding":
            row = dict(report_date=day, calc_version=CORE_VERSION, observation_mode="LIVE_SHADOW",
                       as_of=timestamp, score=40 + i, coverage=1)
            row.update(result={**row, "state": "SUPPORTIVE"}, created_at=timestamp)
        else:
            row = dict(report_date=day, calc_version=LIQUIDITY_VERSION, composite=40 + i,
                       state="边际补水", coverage=1, created_at=timestamp,
                       details={"weight_policy": "FIXED_BASKET_FULL_COVERAGE", "observation_composite": 80})
        rows.append(row)
    return {"status": "OK", "captured_at": "2026-10-23T09:10:00-04:00", "data": rows}


@pytest.mark.parametrize("key", ["core_funding", "liquidity"])
def test_full_fixed_basket_history_and_changes(key):
    data = capture(key)
    old = deepcopy(data)
    item = index_observation(P, data, key)
    assert data == old
    assert item["value"] == 64 and item["status"] == "OK"
    assert item["changes"] == {"1d": 1, "5d": 5, "21d": 21}
    data["data"].pop(-3)
    assert index_observation(P, data, key)["changes"] == {"1d": 1, "5d": None, "21d": None}


@pytest.mark.parametrize("key", ["core_funding", "liquidity"])
@pytest.mark.parametrize("coverage", [None, .8, True, 1.2])
def test_partial_or_bad_coverage_cannot_substitute_a_score(key, coverage):
    data = capture(key)
    data["data"][-1]["coverage"] = coverage
    item = index_observation(P, data, key)
    assert item["value"] is None and item["history"][-1]["value"] is None
    assert item["changes"]["5d"] is None


@pytest.mark.parametrize("key", ["core_funding", "liquidity"])
def test_stale_last_row_is_history_only(key):
    data = capture(key)
    data["data"].pop()
    item = index_observation(P, data, key)
    assert item["value"] is None and item["status"] == "STALE"
    assert item["source_date"] != P["market_date"]
    assert item["history"][-1]["value"] is not None


def test_replay_separate_from_current_and_live_trends():
    data = capture()
    for row in data["data"]:
        row["observation_mode"] = row["result"]["observation_mode"] = "PIT_REPLAY"
    item = index_observation(P, data, "core_funding")
    assert item["value"] is None and not item["history"]
    assert len(item["replay_history"]) == 25 and item["changes"]["5d"] is None
    row = deepcopy(data["data"][-1])
    row["observation_mode"] = row["result"]["observation_mode"] = "LIVE_SHADOW"
    data["data"].append(row)
    item = index_observation(P, data, "core_funding")
    assert item["value"] == 64 and item["changes"]["5d"] is None


@pytest.mark.parametrize("key", ["core_funding", "liquidity"])
def test_old_score_version_is_not_spliced(key):
    data = capture(key)
    data["data"][-3]["calc_version"] = "old"
    assert index_observation(P, data, key)["changes"]["5d"] is None
    data["data"][-1]["calc_version"] = "old"
    assert index_observation(P, data, key)["value"] is None


@pytest.mark.parametrize("field", ["created_at", "as_of", "captured_at"])
def test_after_cutoff_observations_fail_closed(field):
    data = capture()
    if field == "captured_at":
        data[field] = "2026-10-23T09:16:00-04:00"
    else:
        data["data"][-1][field] = "2026-10-23T09:16:00-04:00"
    assert index_observation(P, data, "core_funding")["value"] is None


@pytest.mark.parametrize("status", ["FETCH_ERROR", "CACHED_AFTER_ERROR", "CACHED", "EMPTY"])
def test_cached_capture_never_drives_current_view(status):
    data = capture()
    data["status"] = status
    item = index_observation(P, data, "core_funding")
    assert item["value"] is None and not item["history"]


def test_mismatched_nested_score_and_missing_timestamp_fail_closed():
    data = capture()
    data["data"][-1]["result"]["score"] = 99
    assert index_observation(P, data, "core_funding")["value"] is None
    data = capture()
    del data["data"][-1]["created_at"]
    assert index_observation(P, data, "core_funding")["value"] is None


def test_zero_is_valid_not_missing():
    data = capture()
    data["data"][-1]["score"] = data["data"][-1]["result"]["score"] = 0
    assert index_observation(P, data, "core_funding")["value"] == 0


def test_postgres_timestamp_normalization_is_not_missing_data():
    data = capture()
    data["data"][-1]["as_of"] = "2026-10-22T21:00:00.000000+00:00"
    assert index_observation(P, data, "core_funding")["value"] == 64


def test_newer_unsupported_version_never_resurrects_old_same_day_score():
    data = capture()
    newer = deepcopy(data["data"][-1])
    newer.update(calc_version="unreviewed_next_version", created_at="2026-10-22T18:00:00-04:00")
    data["data"].append(newer)
    item = index_observation(P, data, "core_funding")
    assert item["value"] is None and item["status"] == "UNSUPPORTED_VERSION"
    assert all(p["date"] != P["market_date"] for p in item["history"])


def test_contract_version_matches_producer():
    # The standalone digest release does not bundle the producer or its dependencies.
    producer = pytest.importorskip("core_funding_index")
    assert CORE_VERSION == producer.CALC_VERSION


def test_alerts_explain_raw_values_and_thresholds_separately_from_gaps():
    report = sample_report(P)
    report["metrics"]["ig"].update(value=400, status="OK", acquisition="OK")
    front = build_frontpage(report, {})
    text = " ".join(a["text"] for a in front["alerts"])
    assert "400.00基点" in text and "观察线" in text and P["market_date"] in text
    assert all(a["kind"] != "quality" for a in front["alerts"])
    assert any("核心资金" in gap for gap in front["gaps"])
    report["metrics"]["ig"]["status"] = "STALE"
    assert "400.00基点" not in str(build_frontpage(report, {})["alerts"])


def test_home_has_domains_then_tactical_then_alerts_then_funds():
    report = sample_report(P)
    images = [BytesIO(PNG), BytesIO(PNG)]
    html = front_html(report, images)
    assert html.index('id="overview"') < html.index('id="tactical"') < html.index('id="alerts"') < html.index('id="priority"')
    assert html.count('id="overview"') == 1
    assert html.index("宏观资金与融资") < html.index("盘后跨资产战术压力观察")
    assert html.index("微观市场承接") < html.index("盘后跨资产战术压力观察")
    assert html.count("data:image/png;base64,") == 2
    report["frontpage"]["indices"]["core_funding"]["state"] = "<script>bad</script>"
    assert "<script>" not in front_html(report, images)


def test_email_png_becomes_inline_cid_and_keeps_html_attachment_offline():
    html = '<img src="data:image/png;base64,' + b64encode(PNG).decode() + '" alt="chart">'
    msg = EmailMessage()
    msg.set_content("plain")
    add_html_with_images(msg, html, "test")
    assert "cid:digest-chart-test-0@inline" in msg.get_body(preferencelist=("html",)).get_content()
    png = next(p for p in msg.walk() if p.get_content_type() == "image/png")
    assert png.get_payload(decode=True) == PNG
    assert png["Content-ID"] == "<digest-chart-test-0@inline>"
    assert png.get_content_disposition() == "inline"
    assert "data:image" in html


def test_bad_inline_png_refused_before_smtp():
    msg = EmailMessage()
    msg.set_content("plain")
    with pytest.raises(ValueError, match="PNG"):
        add_html_with_images(msg, '<img src="data:image/png;base64,YmFk">', "test")


@pytest.mark.parametrize("table", ["core_funding_daily", "liquidity_daily"])
def test_index_fetch_keeps_bounded_history_not_just_last_day(table):
    session = Mock()
    session.get.return_value.json.side_effect = [[{"report_date": P["market_date"]}], []]
    fetch_table(session, {"SUPABASE_URL": "https://example.test", "SUPABASE_KEY": "private"},
                table, "2020-01-01", P["market_date"])
    params = session.get.call_args_list[0].kwargs["params"]
    start = str(stamp(P["cutoff"]).date() - timedelta(days=431))
    assert f"report_date.gte.{start}" in params["and"]
    assert session.get.call_count == 2
