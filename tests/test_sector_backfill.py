from copy import deepcopy

import pytest
import pandas as pd

from backfill_sector_history import merged_full_metrics, reconstruction_payload
from market_internals import capture_sectors, reconstruct_sectors, verified_previous_close, reuse_verified_prices
from test_market_internals import prices


DAY = "2026-10-19"
CAPTURED = "2026-10-20T08:00:00-04:00"


def test_backfill_preserves_every_other_json_namespace():
    payload = reconstruction_payload(prices(DAY), DAY, CAPTURED)
    row = {"record_date": DAY, "ingested_at": DAY + "T16:35:00-04:00",
           "full_metrics": {"market_internals": {"breadth": {"status": "MISSING"}},
                            "macro_score": 21, "macro_recovery": {"status": "PARTIAL"}}}
    original = deepcopy(row)
    merged = merged_full_metrics(row, payload)
    assert row == original
    assert merged.pop("sector_history_reconstruction") == payload
    assert merged == original["full_metrics"]
    assert payload["point_in_time_eligible"] is False


@pytest.mark.parametrize("row", [{}, {"record_date": DAY}, {"record_date": "2026-10-16", "ingested_at": CAPTURED, "full_metrics": {}}])
def test_backfill_requires_existing_timestamped_row(row):
    with pytest.raises(ValueError):
        merged_full_metrics(row, reconstruction_payload(prices(DAY), DAY, CAPTURED))


def test_incomplete_price_history_fails_without_filling():
    raw = prices(DAY)
    raw.iloc[-4, raw.columns.get_loc("XLK")] = float("nan")
    with pytest.raises(ValueError, match="six complete"):
        reconstruction_payload(raw, DAY, CAPTURED)


@pytest.mark.parametrize("bad", ["late", "naive", "duplicate", "future", "nan", "wrong_source", "intraday"])
def test_reconstruction_rejects_invalid_provenance_and_prices(bad):
    sector = capture_sectors(prices(DAY), DAY, CAPTURED)
    if bad == "late":
        sector["captured_at"] = "2026-10-20T09:16:00-04:00"
    elif bad == "naive":
        sector["captured_at"] = "2026-10-20T08:00:00"
    elif bad == "duplicate":
        sector["prices"]["XLK"].append(sector["prices"]["XLK"][-1])
    elif bad == "future":
        sector["prices"]["XLK"][-1]["date"] = "2026-10-20"
    elif bad == "nan":
        sector["prices"]["SPY"][-1]["close"] = float("nan")
    elif bad == "wrong_source":
        sector["source"] = "unverified"
    else:
        sector["captured_at"] = DAY + "T15:00:00-04:00"
    assert reconstruct_sectors(sector, "2026-10-20T09:15:00-04:00") == {}


def quote_fixture():
    raw = prices(DAY)
    previous = raw.index[-2]
    last, prev = float(raw["XLV"].iloc[-1]), float(raw["XLV"].iloc[-2])
    raw.loc[previous, "XLV"] = float("nan")
    quote = {"symbol": "XLV", "quoteType": "ETF", "regularMarketTime": pd.Timestamp(DAY + "T16:00:00-04:00").timestamp(),
             "regularMarketPrice": last, "regularMarketPreviousClose": prev,
             "previousClose": prev, "exchangeTimezoneName": "America/New_York"}
    terminal = pd.DataFrame({"Close": [last], "Adj Close": [last], "Dividends": [0],
                             "Stock Splits": [0]}, index=[pd.Timestamp(DAY)])
    return raw, quote, terminal


def test_explicit_previous_close_repair_is_audited_not_inferred():
    raw, quote, terminal = quote_fixture()
    original = raw.copy()
    recovered = verified_previous_close(raw, "XLV", DAY, quote, terminal, CAPTURED)
    assert recovered["source_date"] == "2026-10-16"
    assert recovered["close"] == quote["regularMarketPreviousClose"]
    assert recovered["point_in_time_eligible"] is False
    pd.testing.assert_frame_equal(raw, original)


@pytest.mark.parametrize("bad", ["stale", "intraday", "future", "timezone", "symbol", "missing", "dividend",
                               "split", "adjustment", "price_mismatch", "prev_disagree"])
def test_previous_close_rejects_unverifiable_quotes(bad):
    raw, quote, terminal = quote_fixture()
    if bad in ("stale", "intraday", "future"):
        times = {"stale": "2026-10-16T16:00:00-04:00", "intraday": DAY + "T15:59:00-04:00",
                 "future": "2026-10-20T16:00:00-04:00"}
        quote["regularMarketTime"] = pd.Timestamp(times[bad]).timestamp()
    elif bad == "timezone":
        quote["exchangeTimezoneName"] = "UTC"
    elif bad == "symbol":
        quote["symbol"] = "SPY"
    elif bad == "missing":
        terminal = terminal.drop(columns="Dividends")
    elif bad == "dividend":
        terminal["Dividends"] = .2
    elif bad == "split":
        terminal["Stock Splits"] = 2
    elif bad == "adjustment":
        terminal["Adj Close"] *= .99
    elif bad == "price_mismatch":
        quote["regularMarketPrice"] += 1
    else:
        quote["previousClose"] += 1
    assert verified_previous_close(raw, "XLV", DAY, quote, terminal, CAPTURED) is None


def test_saved_verified_price_is_reusable_only_with_consistent_adjustment():
    raw, quote, terminal = quote_fixture()
    current = capture_sectors(raw, DAY, CAPTURED)
    item = verified_previous_close(raw, "XLV", DAY, quote, terminal, CAPTURED)
    old = deepcopy(current)
    old["prices"]["XLV"].append({"date": item["source_date"], "close": item["close"]})
    old["price_recovery"] = [item]
    merged = reuse_verified_prices(current, [old])
    assert len(merged["prices"]["XLV"]) == len(current["prices"]["XLV"]) + 1
    assert len(merged["price_recovery"]) == 1
    current["prices"]["XLV"][0]["close"] *= .99
    assert reuse_verified_prices(current, [old]) == current
