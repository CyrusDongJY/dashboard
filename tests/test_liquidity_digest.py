from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from liquidity_digest import VERSION
from liquidity_digest.job import (
    build, email_addresses, load_config, send, stage_guard, verify_bundle,
)
from liquidity_digest.model import assemble, clean_points, plan, summarize, valid_rows
from liquidity_digest.render import email_text, render_pdf, tactical_lines
from liquidity_digest.sample import sample_report
from liquidity_digest.sources import collect, fetch_table
from liquidity_digest.store import Store, atomic_json, file_hash


@pytest.fixture
def p():
    return plan("2026-10-20")


def capture(data, time="2026-10-20T09:10:00-04:00", status="OK"):
    return {"data": data, "captured_at": time, "status": status, "source": "TEST"}


def test_calendar_holiday_and_previous_session():
    assert plan("2026-07-06")["market_date"] == "2026-07-02"
    with pytest.raises(ValueError):
        plan("2026-07-03")


def test_dst_preserves_wall_clock():
    assert plan("2026-10-20")["cutoff"].endswith("09:15:00-04:00")
    assert plan("2026-11-02")["send_at"].endswith("09:20:00-05:00")


@pytest.mark.parametrize("stage,clock,allowed", [
    ("collect", "09:14:59", True), ("collect", "09:15:00", False),
    ("build", "09:15:00", True), ("build", "09:20:00", False),
    ("send", "09:19:59", False), ("send", "09:20:00", True),
    ("send", "09:25:00", False),
])
def test_stage_windows(p, stage, clock, allowed):
    now = "2026-10-20T" + clock + "-04:00"
    if allowed:
        stage_guard(stage, p, now)
    else:
        with pytest.raises(ValueError):
            stage_guard(stage, p, now)


def test_missing_and_nonfinite_do_not_become_zero(p):
    report = assemble(p, {})
    assert all(m["value"] is None for m in report["metrics"].values())
    assert report["panels"]["tactical"] is None
    assert clean_points([{"date": "2026-10-19", "value": float("inf")}], p["market_date"]) == []
    assert "暂停环境判断" in email_text(report)


def test_exact_daily_windows_and_stale_observation(p):
    metric = summarize("hy", [{"date": "2026-10-09", "value": 300},
                             {"date": "2026-10-19", "value": 330}], p)
    assert metric["changes"]["5日"] is None  # October 12 exists, October 9 is not a substitute.
    assert metric["trend"] == "样本不足"
    old = summarize("hy", [{"date": "2026-10-16", "value": 300}], p)
    assert old["status"] == "AS_OF"
    assert old["source_date"] == "2026-10-16"
    assert old["value"] == 300


def test_weekly_native_observations(p):
    metric = summarize("reserves", [{"date": "2026-10-07", "value": 3000},
                                   {"date": "2026-10-14", "value": 3010}], p)
    assert metric["changes"] == {"1周": 10, "4周": None}
    assert metric["status"] == "OK"


def test_weekly_previous_release_becomes_lagged_after_next_release():
    metric = summarize("reserves", [{"date": "2026-10-14", "value": 3000}], plan("2026-10-23"))
    assert metric["status"] == "LAGGED"


def test_sub_display_noise_not_a_reversal(p):
    metric = summarize("sofr_iorb", [
        {"date": d, "value": v} for d, v in
        (("2026-10-14", 1), ("2026-10-15", 1.001),
         ("2026-10-16", 1.002), ("2026-10-19", 1.001))], p)
    assert metric["trend"] == "连续持平"


def test_net_liquidity_requires_exact_date_alignment(p):
    inputs = {"fed_assets": capture([{"date": "2026-10-14", "value": 6500}]),
              "tga_weekly": capture([{"date": "2026-10-14", "value": 700}]),
              "rrp": capture([{"date": "2026-10-13", "value": 30}])}
    assert assemble(p, inputs)["metrics"]["net_liquidity"]["value"] is None
    inputs["rrp"]["data"].append({"date": "2026-10-14", "value": 40})
    assert assemble(p, inputs)["metrics"]["net_liquidity"]["value"] == 5760


def test_funding_spreads_same_date_and_basis_points(p):
    inputs = {"sofr": capture([{"date": "2026-10-19", "value": 4.4}]),
              "iorb": capture([{"date": "2026-10-19", "value": 4.3}])}
    assert assemble(p, inputs)["metrics"]["sofr_iorb"]["value"] == pytest.approx(10)
    inputs["iorb"]["data"][0]["date"] = "2026-10-16"
    assert assemble(p, inputs)["metrics"]["sofr_iorb"]["value"] is None


def test_cutoff_cache_and_failed_refresh(tmp_path, p):
    store = Store(tmp_path)
    store.append("hy", "OK", [{"date": "2026-10-19", "value": 300}], "test",
                 "2026-10-20T09:05:00-04:00")
    store.append("hy", "FETCH_ERROR", [], "test", "2026-10-20T09:10:00-04:00")
    store.append("hy", "OK", [{"date": "2026-10-19", "value": 320}], "test",
                 "2026-10-20T09:15:01-04:00")
    snap = store.snapshot(p["cutoff"])
    assert snap["hy"]["status"] == "CACHED_AFTER_ERROR"
    assert snap["hy"]["data"][0]["value"] == 300


def test_yesterday_capture_not_marked_fresh(tmp_path, p):
    store = Store(tmp_path)
    store.append("hy", "OK", [{"date": "2026-10-19", "value": 300}], "test",
                 "2026-10-19T18:00:00-04:00")
    assert store.snapshot(p["cutoff"])["hy"]["status"] == "CACHED"


def test_future_dates_and_post_cutoff_recomputations_rejected(p):
    rows = [{"report_date": "2026-10-20"},
            {"report_date": "2026-10-19", "created_at": "2026-10-20T09:16:00-04:00"},
            {"report_date": "2026-10-19", "created_at": "2026-10-19T16:45:00-04:00"}]
    assert valid_rows(rows, "report_date", p["market_date"], p["cutoff"]) == rows[-1:]


def test_metric_contract_filters_filled_nonfinal_and_versions(p):
    row = {"metric": "vix", "scope": "MACRO", "report_date": "2026-10-19",
           "source_date": "2026-10-19", "value": 20, "session": "EOD",
           "is_final": True, "is_filled": False, "calc_version": "v2"}
    for patch in ({"is_final": False}, {"is_filled": True}, {"session": "PRE"},
                  {"source_date": "2026-10-16"}, {"calc_version": None}):
        report = assemble(p, {"db_metric_daily": capture([{**row, **patch}])})
        assert report["metrics"]["vix"]["value"] is None
    old = {**row, "report_date": "2026-10-16", "source_date": "2026-10-16", "calc_version": "v1"}
    report = assemble(p, {"db_metric_daily": capture([old, row])})
    assert len(report["metrics"]["vix"]["history"]) == 1


def test_temporal_replay_and_versions_are_not_spliced(p):
    base = {"panel": "environment", "report_date": "2026-10-19", "source_date": "2026-10-19",
            "risk_value": 30, "observation_mode": "LIVE_SHADOW", "quality_status": "OK",
            "calc_version": "v2"}
    rows = [base, {**base, "panel": "risk_capital", "observation_mode": "REPLAY"},
            {**base, "report_date": "2026-10-16", "source_date": "2026-10-16", "calc_version": "v1"}]
    report = assemble(p, {"db_temporal_state_daily": capture(rows)})
    assert report["temporal"]["environment"] == [base]
    assert report["temporal"]["risk_capital"] == []


def test_read_only_paginated_rest():
    session = Mock()
    full_page = [{"report_date": "2026-10-19"}] * 1000
    first, last = Mock(), Mock()
    first.json.return_value, last.json.return_value = full_page, []
    session.get.side_effect = [first, last]
    result = fetch_table(session, {"SUPABASE_URL": "https://test.supabase.co",
                                   "SUPABASE_KEY": "PRIVATE"}, "metric_daily", "2026-01-01", "2026-10-19")
    assert len(result) == 1000
    assert session.get.call_count == 2
    assert session.get.call_args.kwargs["params"]["offset"] == 1000
    assert "usdjpy_return_5d_pct" in session.get.call_args.kwargs["params"]["metric"]
    session.post.assert_not_called()
    session.patch.assert_not_called()
    session.delete.assert_not_called()


def test_rest_respects_server_row_limit():
    session = Mock()
    first, last = Mock(), Mock()
    first.json.return_value, last.json.return_value = [{"record_date": "2026-10-19"}], []
    session.get.side_effect = [first, last]
    result = fetch_table(session, {"SUPABASE_URL": "https://test.supabase.co", "SUPABASE_KEY": "PRIVATE"},
                         "market_history", "2026-01-01", "2026-10-19")
    assert len(result) == 1
    assert session.get.call_args.kwargs["params"]["offset"] == 1


def test_collection_records_unconfigured_and_failed_sources_without_secrets(tmp_path, p, monkeypatch):
    import pandas as pd
    source = Mock()
    source.fetch_nyfed_rate.return_value = pd.DataFrame(
        {"rate": [4.3], "p99": [4.4]}, index=pd.to_datetime(["2026-10-19"]))
    source.fetch_tga.side_effect = RuntimeError("https://private?api_key=SECRET")
    monkeypatch.setattr("liquidity_digest.sources.OfficialLiquiditySources", lambda *a, **k: source)
    store = Store(tmp_path)
    live_cutoff = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
    results = collect({}, store, {**p, "cutoff": live_cutoff})
    assert results["sofr"] == "OK"
    assert results["sofr99"] == "OK"
    assert results["tga_daily"] == "FETCH_ERROR"
    assert results["hy"] == "NOT_CONFIGURED"
    assert results["db_metric_daily"] == "NOT_CONFIGURED"
    assert "SECRET" not in json.dumps(store.snapshot(live_cutoff))
    source.fetch_fred.assert_not_called()


def test_private_config_and_plain_email_addresses(tmp_path):
    path = tmp_path / "config.json"
    path.write_text("{}")
    path.chmod(0o644)
    with pytest.raises(ValueError):
        load_config(path)
    path.chmod(0o600)
    assert load_config(path) == {}
    with pytest.raises(ValueError):
        email_addresses({"SENDER_EMAIL": "a@example.com", "RECEIVER_EMAIL": "a@example.com,b@example.com"})


def fake_bundle(store, p, demo=False):
    folder = store.root / "reports" / p["report_date"]
    folder.mkdir(parents=True)
    atomic_json(folder / "report.json", {**p, "version": VERSION, "demo": demo})
    for name in ("report.pdf", "report.html", "email.txt", "email.html"):
        (folder / name).write_bytes(b"test")
    atomic_json(folder / "manifest.json", {"files": {n: file_hash(folder / n)
                for n in ("report.json", "report.pdf", "report.html", "email.txt", "email.html")}})
    return folder


MAIL = {"SENDER_EMAIL": "a@example.com", "RECEIVER_EMAIL": "b@example.com", "APP_PASSWORD": "secret"}


def test_send_accepted_then_quit_error_still_sent(tmp_path, p):
    store = Store(tmp_path)
    fake_bundle(store, p)
    smtp = Mock()
    smtp.send_message.return_value = {}
    smtp.quit.side_effect = OSError("disconnect")
    connector = Mock(return_value=smtp)
    assert send(store, MAIL, p, "2026-10-20T09:20:00-04:00", connector)["status"] == "SENT"
    assert store.delivery(p["report_date"])["status"] == "SENT"
    assert send(store, MAIL, p, "2026-10-20T09:21:00-04:00", connector)["status"] == "SKIPPED"
    assert connector.call_count == 1


def test_uncertain_send_never_auto_retried(tmp_path, p):
    store = Store(tmp_path)
    fake_bundle(store, p)
    smtp = Mock()
    smtp.send_message.side_effect = TimeoutError("response lost")
    connector = Mock(return_value=smtp)
    with pytest.raises(TimeoutError):
        send(store, MAIL, p, "2026-10-20T09:20:00-04:00", connector)
    assert store.delivery(p["report_date"])["status"] == "UNCERTAIN"
    assert send(store, MAIL, p, "2026-10-20T09:21:00-04:00", connector)["status"] == "SKIPPED"


def test_pending_crash_receipt_blocks_duplicate(tmp_path, p):
    store = Store(tmp_path)
    store.reserve_delivery(p["report_date"], "id")
    connector = Mock()
    assert send(store, MAIL, p, "2026-10-20T09:20:00-04:00", connector)["receipt"] == "PENDING"
    connector.assert_not_called()


def test_parallel_run_lock_and_delivery_uniqueness(tmp_path, p):
    import sqlite3
    store = Store(tmp_path)
    with store.lock("send"):
        with pytest.raises(BlockingIOError):
            with Store(tmp_path).lock("send"):
                pass
    store.reserve_delivery(p["report_date"], "first")
    with pytest.raises(sqlite3.IntegrityError):
        store.reserve_delivery(p["report_date"], "second")


def test_demo_and_tampered_bundle_cannot_send(tmp_path, p):
    store = Store(tmp_path)
    folder = fake_bundle(store, p, demo=True)
    connector = Mock()
    with pytest.raises(ValueError):
        send(store, MAIL, p, "2026-10-20T09:20:00-04:00", connector)
    (folder / "report.pdf").write_bytes(b"changed")
    with pytest.raises(ValueError):
        verify_bundle(folder)
    connector.assert_not_called()


def test_build_does_not_request_network(tmp_path, p, monkeypatch):
    store = Store(tmp_path)
    rendered = []
    monkeypatch.setattr("liquidity_digest.job.render_pdf",
                        lambda report, path, **kwargs: (rendered.append(report), Path(path).write_bytes(b"pdf")))
    report = build(store, p, "2026-10-20T09:15:00-04:00")
    assert report["metrics"]["hy"]["value"] is None
    store.append("hy", "OK", [{"date": "2026-10-19", "value": 300}], "late",
                 "2026-10-20T09:16:00-04:00")
    assert build(store, p, "2026-10-20T09:17:00-04:00") == report
    assert len(rendered) == 1


@pytest.mark.parametrize("empty", [False, True])
def test_pdf_five_pages_chinese_and_no_overflow(tmp_path, p, empty):
    from pypdf import PdfReader
    report = assemble(p, {}) if empty else sample_report(p)
    path = tmp_path / "report.pdf"
    render_pdf(report, path)
    reader = PdfReader(path)
    assert len(reader.pages) == 5
    assert "盘后跨资产战术压力观察" in reader.pages[0].extract_text()
    assert "核心资金指数" in reader.pages[0].extract_text()
    # ReportLab deduplicates the identical two missing-data PNGs.
    assert len(reader.pages[0]["/Resources"]["/XObject"]) == (1 if empty else 2)
    assert "市场内部结构" in reader.pages[2].extract_text()
    assert "影子阈值" in reader.pages[2].extract_text()
    assert "资金配置、持仓与杠杆" in reader.pages[3].extract_text()
    assert "信用" in reader.pages[4].extract_text()
    assert len(reader.pages[4]["/Resources"]["/XObject"]) == 3
    assert any("/FontFile2" in font.get_object()["/FontDescriptor"].get_object()
               for font in reader.pages[0]["/Resources"]["/Font"].values()
               if "/FontDescriptor" in font.get_object())
    if not empty:
        assert "模拟样例" in reader.pages[0].extract_text()
        assert any("贡献" in line for line in tactical_lines(report))
