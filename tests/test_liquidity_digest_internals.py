from copy import deepcopy
from datetime import date, timedelta
import json

import pytest

from liquidity_digest.internals import build_internals
from liquidity_digest.internals_view import internals_html, internals_lines
from liquidity_digest.model import assemble, plan, sessions
from liquidity_digest.sample import sample_report
from market_internals import OBSERVATION_VERSION, SECTORS, capture_breadth, capture_sectors
from test_market_internals import stocks, prices


@pytest.fixture
def p():
    return plan("2026-10-20")


def record(day="2026-10-19", up=350):
    timestamp = day + "T16:35:00-04:00"
    return {"record_date": day, "ingested_at": timestamp, "full_metrics": {"market_internals": {
        "version": OBSERVATION_VERSION, "computed_at": timestamp,
        "breadth": capture_breadth(stocks(up), timestamp),
        "sectors": capture_sectors(prices(day), day, timestamp)}}}


def capture(rows, **updates):
    return {"status": "OK", "captured_at": "2026-10-20T09:10:00-04:00", "data": rows, **updates}


def observations(row):
    return row["full_metrics"]["market_internals"]


def metric(p, step=2):
    grid = sessions(date.fromisoformat(p["market_date"]) - timedelta(days=40), p["market_date"])[-22:]
    return {"pct_20ma": {"history": [{"date": d, "value": 30 + i * step} for i, d in enumerate(grid)],
                         "status": "OK", "acquisition": "OK", "source_date": p["market_date"]}}


def test_new_block_does_not_change_scores_or_domain_votes(p):
    base = assemble(p, {})
    report = assemble(p, {"db_market_history": capture([record()])})
    assert report["domains"] == base["domains"]
    assert report["states"] == base["states"]
    assert report["internals"]["breadth"]["state"] == "BROAD_UP"
    assert report["internals"]["leadership"]["state"] == "ADVANCE_SPREAD"
    json.dumps(report, allow_nan=False)


def test_missing_is_not_rotation_or_calm(p):
    panel = build_internals(p, {}, {})
    assert panel["breadth"]["state"] == "UNKNOWN"
    assert panel["leadership"]["state"] == "UNKNOWN"
    assert panel["rotation"]["state"] == "UNKNOWN"
    assert panel["persistence"]["state"] == "UNKNOWN"
    assert "不以旧上涨比例反推" in panel["breadth"]["reason"]


@pytest.mark.parametrize("up,state", [(350, "BROAD_UP"), (325, "BROAD_UP"), (250, "MIXED"), (100, "BROAD_DOWN")])
def test_participation_classification(p, up, state):
    assert build_internals(p, capture([record(up=up)]), {})["breadth"]["state"] == state


def test_old_records_cannot_infer_flat_counts(p):
    row = {"record_date": p["market_date"], "pct_adv": 70, "breadth_sample_size": 500}
    panel = build_internals(p, capture([row]), {})
    assert panel["breadth"]["advancing"] is None
    assert panel["breadth"]["unchanged"] is None


@pytest.mark.parametrize("field,bad", [("source_date", "2026-10-16"), ("status", "LOW_COVERAGE"),
                                     ("session", "PRE"), ("duplicate_count", 1), ("advancing", 999),
                                     ("valid_count", 400), ("advancing", True),
                                     ("captured_at", "2026-10-20T09:10:00-04:00"),
                                     ("captured_at", "2026-10-20T09:16:00-04:00")])
def test_invalid_breadth_contract_is_excluded(p, field, bad):
    row = record()
    observations(row)["breadth"][field] = bad
    assert build_internals(p, capture([row]), {})["breadth"]["quality"] == "UNAVAILABLE"


@pytest.mark.parametrize("status", ["FETCH_ERROR", "CACHED", "CACHED_AFTER_ERROR", "MISSING"])
def test_failed_capture_does_not_confirm_a_state(p, status):
    assert build_internals(p, capture([record()], status=status), {})["breadth"]["state"] == "UNKNOWN"


def test_point_in_time_rejects_late_revisions(p):
    for key in ("ingested_at", "computed_at"):
        row = record()
        target = row if key == "ingested_at" else observations(row)
        target[key] = "2026-10-20T09:16:00-04:00"
        assert build_internals(p, capture([row]), {})["breadth"]["state"] == "UNKNOWN"
    assert build_internals(p, capture([record()], captured_at="2026-10-20T09:16:00-04:00"), {})["breadth"]["state"] == "UNKNOWN"


def test_version_boundary_does_not_mix_history(p):
    row = record()
    observations(row)["version"] = "old"
    assert build_internals(p, capture([row]), {})["audit"]["accepted_dates"] == []


def test_stale_industry_or_missing_spy_excess_keeps_unknown(p):
    row = record()
    observations(row)["sectors"]["sectors"][0]["source_date"] = "2026-10-16"
    panel = build_internals(p, capture([row]), {})
    assert panel["sectors"]["valid_count"] == 10
    assert panel["leadership"]["state"] == "UNKNOWN"
    row = record()
    observations(row)["sectors"]["benchmarks"]["SPY"]["returns"]["1d"] = None
    panel = build_internals(p, capture([row]), {})
    assert panel["sectors"]["rows"][0]["excess"]["1d"] is None
    assert panel["leadership"]["state"] == "UNKNOWN"


def test_relative_leader_can_still_be_falling(p):
    row = record(up=100)
    sector = observations(row)["sectors"]
    sector["benchmarks"]["SPY"]["returns"]["1d"] = -2
    for r in sector["sectors"]:
        r["returns"]["1d"] = -1
    panel = build_internals(p, capture([row]), {})
    assert panel["sectors"]["positive_count"] == 0
    assert panel["sectors"]["rows"][0]["excess"]["1d"] == 1
    assert panel["leadership"]["state"] == "DECLINE_SPREAD"


def test_persistence_exact_windows_and_gap_break(p):
    metrics = metric(p)
    panel = build_internals(p, {}, metrics)
    assert panel["persistence"]["state"] == "IMPROVING"
    assert panel["persistence"]["changes"] == {"5d": 10, "21d": 42}
    metrics["pct_20ma"]["history"].pop(-3)
    panel = build_internals(p, {}, metrics)
    assert panel["persistence"]["state"] == "UNKNOWN"
    assert panel["persistence"]["changes"] == {"5d": None, "21d": None}
    assert panel["history"][-3]["pct_20ma"] is None


def test_single_day_up_does_not_hide_deteriorating_participation(p):
    report = sample_report(p)
    assert report["internals"]["breadth"]["state"] == "BROAD_UP"
    assert report["internals"]["persistence"]["state"] == "DETERIORATING"
    assert "单日上涨尚未扭转" in report["internals"]["interpretation"]


def test_turn_pending_not_confirmed_reversal(p):
    metrics = metric(p)
    metrics["pct_20ma"]["history"][-1]["value"] -= 5
    panel = build_internals(p, {}, metrics)
    assert panel["persistence"]["state"] == "TURN_PENDING"
    assert panel["persistence"]["streak"] == 1


def rotation_records(p):
    grid = sessions("2026-10-01", p["market_date"])[-6:]
    rows = [record(d) for d in grid]
    for i, row in enumerate(rows):
        sec = observations(row)["sectors"]
        for j, r in enumerate(sec["sectors"]):
            excess = 5 - j if i < 3 else (6 - j if j in (3, 4, 5) else -3 - j)
            r["returns"]["5d"] = sec["benchmarks"]["SPY"]["returns"]["5d"] + excess
    return rows


def test_rotation_needs_persistent_multi_sector_handoff(p):
    rows = rotation_records(p)
    for row in rows:
        observations(row)["sectors"].pop("prices")
    rotation = build_internals(p, capture(rows), {})["rotation"]
    assert rotation["state"] == "CANDIDATE"
    assert len(rotation["to_sectors"]) >= 2
    assert len(rotation["from_sectors"]) >= 2
    assert build_internals(p, capture(rows[-1:]), {})["rotation"]["state"] == "UNKNOWN"
    assert build_internals(p, capture(rows[:2] + rows[3:]), {})["rotation"]["state"] == "UNKNOWN"


def test_saved_price_history_can_seed_rotation_without_fabricating_breadth(p):
    panel = build_internals(p, capture([record()]), {})
    assert panel["rotation"]["state"] == "UNCONFIRMED"
    assert len(panel["rotation"]["available_dates"]) == 6
    assert len(panel["rotation"]["reconstructed_dates"]) == 5
    assert sum(r["advancing_pct"] is not None for r in panel["history"]) == 1
    assert "历史价格重建 5 日" in panel["rotation"]["history_note"]


def test_explicit_reconstruction_preserves_unknown_breadth_and_original_cutoff(p):
    from backfill_sector_history import reconstruction_payload
    day = p["market_date"]
    recovery = reconstruction_payload(prices(day), day, "2026-10-20T09:10:00-04:00")
    row = {"record_date": day, "ingested_at": day + "T16:35:00-04:00",
           "full_metrics": {"sector_history_reconstruction": recovery}}
    panel = build_internals(p, capture([row]), {})
    assert panel["breadth"]["state"] == "UNKNOWN"
    assert panel["leadership"]["state"] == "UNKNOWN"
    assert panel["sectors"]["valid_count"] == 11
    assert len(panel["rotation"]["reconstructed_dates"]) == 6
    recovery["computed_at"] = "2026-10-20T09:16:00-04:00"
    assert build_internals(p, capture([row]), {})["rotation"]["state"] == "UNKNOWN"


def test_reconstruction_does_not_replace_recorded_returns_or_invalid_windows(p):
    row = record()
    observations(row)["sectors"]["sectors"][0]["returns"]["5d"] = 42
    panel = build_internals(p, capture([row]), {})
    assert panel["sectors"]["rows"][0]["returns"]["5d"] == 42
    observations(row)["sectors"]["prices"]["SPY"].pop(-4)
    assert build_internals(p, capture([row]), {})["rotation"]["state"] == "UNKNOWN"


def test_rank_ties_do_not_create_rotation(p):
    rows = rotation_records(p)
    for row in rows:
        for r in observations(row)["sectors"]["sectors"]:
            r["returns"]["5d"] = 2
    assert build_internals(p, capture(rows), {})["rotation"]["state"] == "UNCONFIRMED"


def test_presentations_show_scope_dates_and_both_returns(p):
    report = sample_report(p)
    for compact in (False, True):
        html = internals_html(report, compact)
        for label in ("市场内部结构", "前500", "平盘", "超额", "跑赢不等于上涨", "近21", "缺测留空", p["market_date"]):
            assert label in html
        for label in SECTORS.values():
            assert label in html
    assert "不新增总分" in "\n".join(internals_lines(report))


def test_presentation_escapes_external_strings(p):
    report = sample_report(p)
    report["internals"]["gaps"] = ["<script>bad()</script>"]
    assert "<script>" not in internals_html(report)
    assert "&lt;script&gt;" in internals_html(report)
    missing = assemble(p, {})
    assert "不能按0个行业上涨" in "\n".join(internals_lines(missing))
    assert "涨跌分布待采" in internals_html(missing)


def test_repeated_assembly_does_not_mutate_inputs(p):
    inputs = capture([record()])
    original = deepcopy(inputs)
    assert build_internals(p, inputs, {}) == build_internals(p, inputs, {})
    assert inputs == original


def test_market_history_fetch_has_bounded_history():
    from unittest.mock import Mock
    from liquidity_digest.sources import fetch_table
    client = Mock()
    client.get.return_value.json.return_value = []
    fetch_table(client, {"SUPABASE_URL": "https://example.test", "SUPABASE_KEY": "TEST"},
                "market_history", "2020-01-01", "2026-10-19")
    assert client.get.call_args.kwargs["params"]["and"] == "(record_date.gte.2026-08-10,record_date.lte.2026-10-19)"
