from copy import deepcopy

import pytest

from liquidity_digest.capital import (
    SERIES, VERSION, build_capital, parse_cftc, parse_finra, parse_ici, fetch_observations,
)
from liquidity_digest.model import plan

AT = "2026-10-26T13:05:00+00:00"


def cftc(day="2026-10-20", **updates):
    return {"cftc_contract_market_code": "20974+", "futonly_or_combined": "FutOnly",
            "report_date_as_yyyy_mm_dd": day + "T00:00:00.000", "open_interest_all": "1000",
            "contract_units": "NASDAQ 100 INDEX X $20", "asset_mgr_positions_long": "400",
            "asset_mgr_positions_short": "100", "asset_mgr_positions_spread": "50",
            "lev_money_positions_long": "120", "lev_money_positions_short": "300",
            "lev_money_positions_spread": "60", **updates}


def capture(data, status="OK", at=AT):
    return {"status": status, "captured_at": at, "data": data}


def latest(inputs, key):
    return next(r for r in build_capital(plan("2026-10-26"), inputs)["observations"] if r["key"] == key)


def test_cftc_fixed_contract_and_report_basis_no_double_count():
    rows = parse_cftc([cftc(), cftc(cftc_contract_market_code="209747"),
                      cftc(futonly_or_combined="Combined")], AT)
    assert len(rows) == 2
    assert rows[0]["value"] == 300
    assert rows[1]["value"] == -180
    assert rows[0]["details"]["net_oi_pct"] == 30
    assert rows[0]["released_at"] is None
    assert rows[0]["available_at"] == AT
    with pytest.raises(ValueError, match="Duplicate"):
        parse_cftc([cftc(), cftc()], AT)


@pytest.mark.parametrize("change", [{"asset_mgr_positions_long": None}, {"open_interest_all": "0"},
                                 {"asset_mgr_positions_spread": "900"}, {"asset_mgr_positions_short": "-1"}])
def test_missing_or_invalid_position_never_becomes_zero(change):
    rows = parse_cftc([cftc(**change)], AT)
    assert rows[0]["value"] is None


def test_cftc_weekly_delta_requires_exact_week_not_arbitrary_prior_row():
    rows = parse_cftc([cftc(), cftc("2026-10-06", asset_mgr_positions_long="200")], AT)
    result = latest({"cftc_positions": capture(rows)}, "cot_20974+_asset_mgr")
    assert result["changes"]["1w"] is None
    rows += parse_cftc([cftc("2026-10-13", asset_mgr_positions_long="200")], AT)
    assert latest({"cftc_positions": capture(rows)}, "cot_20974+_asset_mgr")["changes"]["1w"] == 200


ICI = """<html><p>Millions of dollars</p><table>
<tr><th></th><th>10/14/2026</th><th>10/7/2026</th><th>9/30/2026</th><th>9/23/2026</th></tr>
<tr><td>Domestic</td><td>-100</td><td>20</td><td>30</td><td>40</td></tr>
<tr><td>World</td><td>900</td><td>900</td><td>900</td><td>900</td></tr></table>
<p>Activity in collective investment trusts affected this release.</p></html>"""


def test_ici_structural_note_and_sum_preserve_published_values():
    rows = parse_ici(ICI, AT)
    assert len(rows) == 4
    result = latest({"ici_flows": capture(rows)}, "ici_domestic")
    assert result["value"] == -100
    assert result["sum_4w"] == -10
    assert result["details"]["structural_note_present"]
    assert result["state_cn"] == "结构活动干扰，暂停方向归因"
    assert latest({"ici_flows": capture(rows[:-1])}, "ici_domestic")["sum_4w"] is None
    with pytest.raises(ValueError, match="units"):
        parse_ici(ICI.replace("Millions", "Billions"), AT)


FINRA = """<html><p>shown in $ millions</p><table><tr><th>Month/Year</th>
<th>Debit Balances in Customers' Securities Margin Accounts</th>
<th>Free Credit Balances in Customers' Cash Accounts</th>
<th>Free Credit Balances in Customers' Securities Margin Accounts</th></tr>
<tr><td>Sep-26</td><td>1,000</td><td>100</td><td>200</td></tr>
<tr><td>Aug-26</td><td>900</td><td>110</td><td>210</td></tr>
<tr><td>Jun-26</td><td>800</td><td>120</td><td>220</td></tr></table></html>"""


def test_finra_month_end_balance_not_daily_flow():
    result = latest({"finra_margin": capture(parse_finra(FINRA, AT))}, "finra_debit")
    assert result["observation_date"] == "2026-09-30"
    assert result["changes"] == {"1m": 100, "3m": 200}
    assert result["state_cn"] == "月度融资余额增加"
    assert result["evidence_family"] == "MARGIN_BALANCE"


@pytest.mark.parametrize("field,value", [("available_at", "2026-10-26T13:16:00Z"),
                                       ("acquired_at", "2026-10-26T13:16:00Z"),
                                       ("unit", "USD"), ("frequency", "daily"),
                                       ("observation_date", "2026-10-27"), ("definition_version", "old")])
def test_point_in_time_and_definition_guards(field, value):
    rows = parse_finra(FINRA, AT)
    for row in rows:
        row[field] = value
    assert latest({"finra_margin": capture(rows)}, "finra_debit")["value"] is None


def test_access_denial_is_explicit_and_never_bypassed():
    class Response:
        status_code = 403
    class Session:
        def get(self, *_args, **_kwargs):
            return Response()
    with pytest.raises(PermissionError):
        fetch_observations(Session(), "ici_flows")
    result = latest({"ici_flows": capture([], "ACCESS_DENIED")}, "ici_domestic")
    assert result["status"] == "ACCESS_DENIED" and result["value"] is None


def test_cached_and_missing_data_are_not_healthy_or_scoring_inputs():
    result = build_capital(plan("2026-10-26"), {"finra_margin": capture(parse_finra(FINRA, AT), "CACHED_AFTER_ERROR")})
    assert result["score_eligible"] is result["decision_eligible"] is False
    assert result["etf_status"] == "NOT_VERIFIED"
    assert next(r for r in result["observations"] if r["key"] == "finra_debit")["status"] == "CACHED_AFTER_ERROR"


def test_report_build_does_not_mutate_source_capture():
    inputs = {"ici_flows": capture(parse_ici(ICI, AT)), "cftc_positions": capture(parse_cftc([cftc()], AT))}
    before = deepcopy(inputs)
    build_capital(plan("2026-10-26"), inputs)
    assert inputs == before
