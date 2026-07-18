# ETF Fund Data RFI and Sample Acceptance Checklist

- **RFI ID**: `capflow-etf-rfi-2026-07-18`
- **Purpose**: source selection for a point-in-time, auditable U.S. ETF primary-market flow estimate
- **Status**: READY FOR VENDOR RESPONSE
- **Related decision**: [ETF 数据源与预算选型](2026-07-18-etf-data-source-selection.md)

This document is designed to be sent unchanged to every candidate vendor so responses can be compared field by field. A marketing confirmation without sample data and licensing language will not be treated as acceptance.

---

## 1. Intended use

We are evaluating data for an internal research and market-monitoring system. The system will:

- ingest and retain source observations and revisions;
- calculate an auditable `ETF_PRIMARY_MARKET_FLOW_ESTIMATE` from fund-level shares outstanding and NAV;
- normalize flows by AUM and aggregate them by asset class, sector and horizon;
- retain point-in-time timestamps to prevent look-ahead bias;
- display derived indicators internally.

The metric is an estimate of the NAV value of ETF creations/redemptions. We do not represent it as settled cash flow.

---

## 2. Required sample universe

Please provide samples for all available instruments below. If an instrument is not covered, mark it explicitly rather than substituting another share class.

| Ticker | Issuer | Exposure |
|---|---|---|
| SPY | State Street | U.S. large-cap equity |
| IVV | iShares | U.S. large-cap equity |
| VOO | Vanguard | U.S. large-cap equity |
| QQQ | Invesco | Nasdaq-100 equity |
| VTI | Vanguard | U.S. total-market equity |
| IWM | iShares | U.S. small-cap equity |
| HYG | iShares | High-yield corporate bonds |
| LQD | iShares | Investment-grade corporate bonds |
| XLF | State Street | Financial sector equity |
| TQQQ | ProShares | 3x leveraged equity |

---

## 3. Field-level questionnaire

For each field, please provide the exact field name, definition, unit, update schedule, first available date and a populated sample.

| Requirement | Vendor response |
|---|---|
| Fund-level shares outstanding / units outstanding | |
| NAV per fund share | |
| Fund net assets / AUM | |
| Vendor-calculated daily fund flow, if available | |
| Creation-unit size | |
| Primary-market creation/redemption units or baskets, if available | |
| Split and reverse-split events and adjustment factors | |
| Merger, liquidation, ticker, CUSIP and share-class changes | |
| Stable fund identifier across identifier changes | |
| Observation/effective date | |
| Vendor publication timestamp | |
| Earliest timestamp the value was available to clients | |
| Revision identifier and revision publication timestamp | |
| As-reported value and adjusted value | |
| Currency and valuation timezone | |
| Preliminary/final status | |
| Null, stale and suppressed-value conventions | |

Please state whether “shares outstanding” refers to ETF fund units or to shares of the fund's underlying portfolio companies. Only fund units meet this requirement.

---

## 4. Point-in-time and revision requirements

Please answer each item with **Yes / No / Partial**, then explain and provide evidence.

1. Can a client query exactly what value was available at a specified historical timestamp?
2. Are corrected observations retained as separate vintages rather than silently overwritten?
3. Are `effective_at`, `published_at` and `available_at` distinct where applicable?
4. Is historical coverage at least three years? Please quote the first date by field.
5. Can adjusted and as-reported shares be retrieved separately?
6. Can adjustment factors and the corporate-action event that caused them be joined deterministically?
7. Does historical data include inactive, liquidated and renamed ETFs?
8. How are late files, restatements and vendor backfills communicated?

---

## 5. Mandatory sample files

Please provide machine-readable CSV, JSON or Parquet samples containing:

1. At least 20 consecutive trading days for the ten-ETF universe.
2. At least five years of daily data for SPY, IVV, QQQ, IWM, HYG and TQQQ, where available.
3. Three to five historical corporate-action cases, including at least one ETF split or reverse split.
4. Two or more vintages of a revised observation, if revisions occur.
5. The related data dictionary and corporate-action code list.

Samples must retain vendor-native timestamps and identifiers. A screenshot or PDF report alone is not sufficient.

---

## 6. Calculation methodology

If the product supplies a finished flow field, please disclose:

- the exact formula;
- whether current-day or prior-day NAV is used;
- treatment of dividends, fees, distributions and FX;
- treatment of in-kind creations/redemptions;
- treatment of splits, reverse splits, mergers and liquidations;
- preliminary/final timing and subsequent revisions;
- whether flow is reconstructed from AUM or directly from fund shares.

We will retain vendor flow and internally calculated flow as separate metric versions. They will not be silently blended.

---

## 7. Delivery and service levels

Please specify:

| Item | Vendor response |
|---|---|
| Delivery method: API / SFTP / cloud / bulk file | |
| Normal U.S. ETF publication time and timezone | |
| Preliminary and final release schedule | |
| Expected holiday/non-trading-day behavior | |
| Historical bulk-download method | |
| Rate limits and concurrency limits | |
| Correction/backfill notification channel | |
| Availability and timeliness SLA | |
| Support and escalation path | |
| Sandbox or evaluation entitlement | |

---

## 8. Licensing questionnaire

Please provide written terms covering each use separately:

- automated ingestion;
- storage of raw observations and historical revisions;
- storage after contract termination;
- calculation and storage of derived indicators;
- internal display to named users;
- internal distribution across teams or legal entities;
- use in model research and backtesting;
- use in alerts and generated reports;
- cloud hosting and disaster-recovery copies;
- publication of aggregated or transformed outputs;
- audit access to historical source values.

Please identify any exchange, index provider or issuer pass-through fees and any instrument-count, user-count or derived-data restrictions.

---

## 9. Commercial response

Please quote separately:

1. Evaluation/POC access.
2. Historical bulk delivery.
3. Ongoing daily API or file delivery.
4. Corporate-action and security-master modules.
5. Point-in-time/revision history.
6. Internal derived-data and redistribution rights.
7. Additional users, environments and legal entities.
8. One-time onboarding and support fees.

All prices should include currency, tax treatment, contract term, renewal basis and any usage tiers. Terminal seat pricing alone is not responsive to this RFI.

---

## 10. Acceptance gates

A source can enter production only after all hard gates pass:

| Gate | Required result |
|---|---|
| Fund shares and NAV coverage | ≥ 98% over the 20-trading-day POC |
| Publication lag | ≤ 1 trading day under the documented schedule |
| Corporate-action sample | 100% of selected events correctly represented |
| Point-in-time replay | Reconstructs the information available at the requested timestamp |
| Revision lineage | Original and revised values remain auditable |
| Issuer cross-check | Sample values and source dates match official issuer data |
| License | Automated storage, derivation and intended internal use confirmed in writing |

If a corporate action cannot be confirmed, the relevant daily flow is set to null and excluded from aggregation. The system will never assume an adjustment factor of one merely because the event feed is missing.
