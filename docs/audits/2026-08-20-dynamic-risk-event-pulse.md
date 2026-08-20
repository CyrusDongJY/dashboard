# Dynamic Risk Event Pulse Review

Date: 2026-08-20

## Decision

Keep the fixed eight-name risk-capital ladder as the comparable longitudinal
baseline. Add a separate, shadow-only dynamic event pulse to discover changing
leaders and laggards. Do not mix dynamic names directly into the fixed score.

## Why

MRNA's 2026-08-19 move shows the fixed-basket blind spot. The stock more than
doubled after positive Phase 3 melanoma-treatment news. That move contains both
company-specific information and a possible risk-appetite/positioning response.
Price and volume alone cannot identify how much came from fundamentals, short
covering, options hedging, or broad risk demand.

References:

- AP, 2026-08-19: https://apnews.com/article/2330dce708b0af215b68570b19d025df
- IBKR market scanner contract and limits:
  https://interactivebrokers.github.io/tws-api/market_scanners.html

## Production contract

- Discovery: IBKR `TOP_PERC_GAIN`, `TOP_PERC_LOSE`, and `HOT_BY_VOLUME`.
- Scanner rows can omit industry metadata; request full IBKR contract details
  for those candidates before evaluating cross-industry confirmation.
- Universe filters: US major stocks, USD, common stock, price at least $5,
  daily volume at least 1 million, scanner market cap at least $1 billion.
- The fixed ladder's eight names and QQQ are excluded from the dynamic list to
  avoid double counting.
- Candidates are validated with 9 months of split-adjusted RTH daily bars.
- Direction requires all three: absolute daily move at least 5%, relative QQQ
  move at least 3%, and robust log-dollar-volume z-score at least 1.
- A single-name extreme flag requires an absolute daily move at least 20%, or
  a move of at least 5% together with robust turnover z-score at least 5.
- Market confirmation requires at least three same-direction candidates across
  at least two known industries. One industry is thematic, not market-wide.
- Catalyst status remains `UNVERIFIED`; the module has no licensed news/event
  classifier and must not invent one.
- Missing one of the required gain/lose scans produces `UNAVAILABLE`.
- The output is shadow-only and does not alter alerts, environment scores,
  position sizing, or execution.

## Known limitations

- Scanner lists are selected tails, not a representative market breadth sample.
- IBKR scanner membership is point-in-time and cannot be faithfully backfilled.
- Scanner industry metadata can be missing or coarse; unknown industries cannot
  satisfy cross-industry confirmation.
- Dollar volume is `close x RTH volume`, not net capital flow.
- Thresholds are heuristic until enough prospective observations exist for
  walk-forward validation.
- Event/news attribution needs a separate licensed source before it can move
  beyond `UNVERIFIED`.

## Acceptance path

1. Unit and migration tests.
2. Production-interpreter compile and import.
3. IBKR scanner smoke test with all three scan codes.
4. Supabase full-field read after migration.
5. One natural 16:02 ET run, then verify summary and candidate rows.
6. Confirm both the track-one and 16:45 review emails render the pulse.
