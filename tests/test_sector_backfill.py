from copy import deepcopy

import pytest

from backfill_sector_history import merged_full_metrics, reconstruction_payload
from market_internals import capture_sectors, reconstruct_sectors
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
