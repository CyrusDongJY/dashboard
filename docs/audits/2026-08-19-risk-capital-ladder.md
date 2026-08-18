# Risk Capital Ladder design review (2026-08-19)

## Decision

Adopt the eight-name basket as a **risk-appetite diffusion proxy** in shadow
mode. Do not label it dollar liquidity, net flow, or a return forecast. Do not
connect it to alert gates, environment classification, or position sizing until
walk-forward validation is complete.

## Why the idea is useful

- Sentiment research supports using turnover and hard-to-value, volatile,
  difficult-to-arbitrage equities as cross-sectional sentiment evidence.
- HOOD, COIN, and MSTR have business or balance-sheet exposure to trading
  activity, crypto conditions, and capital-market reflexivity.
- Relative breadth can reveal when QQQ performance remains concentrated while
  increasingly speculative equities have already started to underperform.

## Important limits

- The basket is static and selected with hindsight. Its composition must be
  versioned and reviewed periodically without rewriting historical membership.
- NVDA/PLTR/SMCI and COIN/MSTR are correlated clusters, not eight independent
  observations. Aggregation is therefore layer-neutral rather than name-neutral.
- QQQ contains some basket members, especially NVDA, which mechanically dampens
  their benchmark-relative return. The comparison is still useful but is not a
  pure factor-neutral residual.
- Earnings, regulation, product news, index changes, short squeezes, and crypto
  events can dominate one name. A single member cannot publish a market state.
- `close * RTH volume` is a dollar-volume proxy. It is not free-float turnover,
  exchange-wide consolidated dollar volume, signed order flow, or net capital
  flow.

## V1 data contract

- Benchmark: QQQ.
- Members: NVDA, TSLA / HOOD, PLTR, SMCI, CVNA / COIN, MSTR.
- Evidence: 1D and 5D return relative to QQQ, absolute 5D return, 20D and 50D
  trend, current dollar-volume proxy versus the member's own 20D median, and a
  robust z-score of log dollar volume versus the prior 60 sessions.
- Readiness: at least 7/8 eligible members; both members required in the
  institutional and extreme layers; at least 3/4 in the speculative layer.
- Aggregation: equal weight across the three layers.
- Output states: broad expansion, speculative blow-off, mega-cap concentration,
  edge contraction, active deleveraging, contraction, mixed, or unavailable.

## Validation path

1. Accumulate or backfill member-level observations without changing V1 rules.
2. Test state persistence, next-day and 5/21-day downside, drawdown, and false
   positives around earnings and major crypto events.
3. Compare incremental information versus QQQ-QQQE, TRIN, VIX structure, credit,
   and the existing environment index.
4. Promote only if out-of-sample results are stable across calm, tightening,
   crash, and recovery regimes.

## Primary references

- Baker and Stein, *Market Liquidity as a Sentiment Indicator*:
  https://www.nber.org/papers/w8816
- Baker and Wurgler, *Investor Sentiment and the Cross-Section of Stock Returns*:
  https://www.nber.org/papers/w10449
- Robinhood 2025 Form 10-K:
  https://www.sec.gov/Archives/edgar/data/1783879/000178387926000023/hood-20251231.htm
- Coinbase 2025 Form 10-K:
  https://www.sec.gov/Archives/edgar/data/1679788/000167978826000015/coin-20251231.htm
- Strategy 2025 Form 10-K:
  https://www.sec.gov/Archives/edgar/data/1050446/000105044626000020/mstr-20251231.htm
- IBKR historical-bar contract:
  https://interactivebrokers.github.io/tws-api/historical_bars.html
