# Data Contract Remediation — 2026-07-17

Scope: remediation of the ten reported data-contract and naming issues. This
change does not promote any shadow score into alerts or position sizing.

## Disposition

| Item | Status | Control |
| --- | --- | --- |
| Gamma/PCR possible ticker crossing | Hardened | Intraday context remains explicitly SPY, must match the current NY trading date, and now reports source date, model, expirations and zero count. Stale context cannot trigger. |
| ZGL / Gamma Flip ambiguity | Fixed | Curve version, grid contract, max DTE, bucket flips and all zeroes are persisted. ALL is named as a sampled horizon, not a complete option chain. |
| Intraday POC = 0 | Fixed | POC is read from the latest `stock_spot_post_close` SPY row. Missing POC renders NA. |
| U/D = 1.00 | Fixed | Missing UVOL/DVOL returns NULL with `MISSING_UVOL_DVOL`; it cannot trigger. |
| TRIN scope ambiguity | Fixed | IBKR `TRIN-NYSE` and the TradingView top-500 sample are labeled and persisted separately. |
| Tail black-swan label inverted | Fixed | Output now reports `net new highs - new lows`; the legacy `hindenburg` field is compatibility text only. |
| Waterline source mismatch | Hardened | Dashboard TGA now uses the same Treasury DTS daily adapter as Waterline when available, with source dates and source names retained. |
| Breadth / tail coverage = 0 | Explained | Scoring still requires 60 independent observations; summaries now report valid/required counts for every missing component. |
| IV Rank called percentile | Fixed | IV Rank and empirical IV Percentile are calculated, displayed and stored separately. |
| ETF shares frozen | Hardened | The local history is now advanced atomically and emits 1/5/20-observation share and dollar changes. The unverified yfinance share series is context-only and excluded from scoring. |

## Deployment gate

1. Run `migrations.sql` before deploying collectors.
2. Deploy `data_contracts.py` alongside the existing shared modules.
3. Run `daily_pre_market.py`, `daily_post_close.py`, and one intraday scan in
   dry operational supervision; confirm missing fields show NA rather than 0/1.
4. Confirm the newest `intraday_logs.context_metadata` contains SPY, current
   pre-market date, Gamma curve version and source dates.
5. Keep Gamma Flip alerts disabled until production lineage is observed for at
   least five complete trading days.
