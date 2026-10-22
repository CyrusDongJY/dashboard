from copy import deepcopy
import json

import pandas as pd
import pytest

from market_internals import SECTORS, capture_breadth, capture_sectors, eod_time, schedule


DAY = "2026-10-19"
CAPTURE = DAY + "T16:35:00-04:00"


def stocks(up=350, flat=10):
    return [{"s": f"NYSE:T{i}", "d": [f"T{i}", 1 if i < up else 0 if i < up + flat else -1,
             100, 90, 95, 98, 120, 50, 1000]} for i in range(500)]


def prices(day=DAY):
    dates = schedule(day).index[-30:]
    return pd.DataFrame({s: [100 * 1.01 ** i for i in range(len(dates))]
                         for s in ("SPY", "RSP", "QQQ", "QQQE", *SECTORS)}, index=dates)


def test_zero_change_and_missing_volume_have_correct_denominator():
    rows = stocks()
    rows[0]["d"][8] = None
    rows[1]["d"][8] = 0
    result = capture_breadth(rows, CAPTURE)
    assert result["status"] == "OK"
    assert (result["advancing"], result["unchanged"], result["declining"]) == (350, 10, 140)
    assert result["advancing_pct"] == 70
    assert result["valid_count"] == 500
    assert result["provider_timestamp_status"] == "UNAVAILABLE"


@pytest.mark.parametrize("bad", [None, float("nan"), float("inf"), True, "invalid"])
def test_bad_price_change_not_flat_or_down(bad):
    rows = stocks()
    rows[0]["d"][1] = bad
    result = capture_breadth(rows, CAPTURE)
    assert result["valid_count"] == 499
    assert result["advancing"] == 349
    assert result["unchanged"] == 10
    assert result["missing_count"] == 1
    json.dumps(result, allow_nan=False)


def test_missing_ma_uses_valid_ma_denominator():
    rows = stocks()
    rows[0]["d"][3] = None
    result = capture_breadth(rows, CAPTURE)
    assert result["ma"]["20"] == {"above": 499, "valid": 499, "pct": 100}
    assert result["ma"]["200"]["valid"] == 500


def test_duplicate_and_insufficient_samples_are_not_eligible():
    rows = stocks()
    assert capture_breadth(rows[:449], CAPTURE)["status"] == "LOW_COVERAGE"
    assert capture_breadth(rows[:450], CAPTURE)["status"] == "OK"
    assert capture_breadth(rows + [rows[0]], CAPTURE)["status"] == "INVALID_SAMPLE"
    result = capture_breadth([], CAPTURE)
    assert result["status"] == "MISSING"
    assert result["advancing_pct"] is None


@pytest.mark.parametrize("captured,completed", [
    ("2026-10-19T15:59:00-04:00", False), ("2026-10-19T16:04:59-04:00", False),
    ("2026-10-19T16:05:00-04:00", True), ("2026-10-18T17:00:00-04:00", False),
    ("2026-11-27T13:04:00-05:00", False), ("2026-11-27T13:05:00-05:00", True),
])
def test_completed_exchange_session_not_fixed_1600(captured, completed):
    assert eod_time(captured)[1] is completed
    assert (capture_breadth(stocks(), captured)["session"] == "EOD") is completed


def test_naive_time_is_rejected():
    with pytest.raises(ValueError):
        capture_breadth(stocks(), "2026-10-19T16:35:00")


def test_sector_returns_have_exact_adjusted_price_windows():
    result = capture_sectors(prices(), DAY, CAPTURE)
    row = result["sectors"][0]
    assert row["returns"]["1d"] == pytest.approx(1)
    assert row["returns"]["5d"] == pytest.approx((1.01 ** 5 - 1) * 100)
    assert row["returns"]["21d"] == pytest.approx((1.01 ** 21 - 1) * 100)
    assert row["excess"]["5d"] == pytest.approx(0)
    assert result["valid_count"] == 11
    assert len(result["prices"]["XLK"]) == 22


def test_hole_is_not_forward_filled_or_collapsed_into_shorter_window():
    data = prices()
    data.loc[data.index[-3], "XLK"] = float("nan")
    result = capture_sectors(data, DAY, CAPTURE)
    row = next(r for r in result["sectors"] if r["symbol"] == "XLK")
    assert row["returns"]["1d"] is not None
    assert row["returns"]["5d"] is None
    assert row["returns"]["21d"] is None
    assert row["excess"]["5d"] is None
    json.dumps(result, allow_nan=False)


def test_stale_sector_not_relabelled_as_current():
    data = prices()
    data.loc[data.index[-1], "XLK"] = float("nan")
    result = capture_sectors(data, DAY, CAPTURE)
    assert result["status"] == "PARTIAL"
    assert result["valid_count"] == 10
    row = result["sectors"][0]
    assert row["source_date"] == "2026-10-16"
    assert row["returns"]["1d"] is None


def test_outperforming_is_not_positive_absolute_return():
    data = prices()
    data.loc[data.index[-1], "SPY"] = data["SPY"].iloc[-2] * .98
    data.loc[data.index[-1], "XLK"] = data["XLK"].iloc[-2] * .99
    row = capture_sectors(data, DAY, CAPTURE)["sectors"][0]
    assert row["returns"]["1d"] == -1
    assert row["excess"]["1d"] == 1


def test_prices_missing_entirely_do_not_generate_zero_returns():
    result = capture_sectors(None, DAY, CAPTURE)
    assert result["status"] == "MISSING"
    assert all(r["returns"]["1d"] is None for r in result["sectors"])
    assert deepcopy(result) == result


def test_intraday_price_capture_cannot_be_promoted_to_eod():
    result = capture_sectors(prices(), DAY, DAY + "T15:59:00-04:00")
    assert result["session"] == "UNCONFIRMED"
    assert all(r["returns"]["1d"] is None for r in result["sectors"])
