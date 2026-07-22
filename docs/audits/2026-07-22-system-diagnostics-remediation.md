# System Diagnostics Remediation - 2026-07-22

## Production controls

- Distance sign contract: `LEVEL_MINUS_SPOT_V2`; positive means the level is
  above spot. Older rows are not joined into the new distance history.
- Gamma publication requires minimum contract, side, strike and request
  coverage. Raw low-coverage calculations are retained only in
  `option_gamma_buckets` for diagnosis.
- DIX and GEX retain source dates and are context-only. They do not create a
  directional anomaly.
- Missing U/D remains NULL and cannot trigger. Intraday metadata records raw
  UVOL/DVOL and source status.
- Intraday POC is explicitly the latest completed 6M daily-close volume-bin
  proxy, including its source date.

## Shadow diagnostics

- Gap acceptance is published only when the opening gap exceeds the larger of
  15 basis points and 0.1 ATR20.
- VWAP acceptance compares each 5-minute RTH typical price with the cumulative
  VWAP available at that same bar. Both time-weighted and volume-weighted
  acceptance are retained with sample coverage.
- QQQ-QQQE, SPY-RSP and Mag7-RSP are relative-performance concentration
  observations, not claims about investor identity.
- Constituent contribution attribution remains unavailable until a licensed,
  point-in-time holdings-weight source passes the existing vendor gate.

## Deployment gate

1. Apply `migrations.sql` before collectors are replaced.
2. Deploy shared modules before scripts that import them.
3. Run unit tests and syntax checks on the cloud host.
4. Run supervised pre-market, post-close and intraday smoke jobs.
5. Verify low-coverage Gamma writes NULL public values and diagnostic raw values.
