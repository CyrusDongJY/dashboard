# Digest v1.8 reading layout

Presentation-only upgrade. Producer formulas, input eligibility, score histories,
thresholds, source timestamps, alert decisions, and the 09:20 ET schedule are unchanged.

## Reading order

1. Macro and micro summaries, followed immediately by tactical stress.
2. Current tactical score, 1/5/21-session net changes, and continuity as separate items.
3. Important observations and all data-quality gaps.
4. Core funding and liquidity curves, with source/version boundaries retained.
5. Detailed evidence, eight individual charts, and source audit.

The pressure-position matrix names its selected source (environment or tactical
proxy) instead of implying the two different scores are interchangeable.

## Output contracts

- Email has no collapsed essential content. Tactical components follow the summary.
- HTML is offline and script-free. Four independent transmission plots share the
  date range, use a two-column desktop grid and one column on mobile.
- PDF normally has six pages, including separate funding and transmission chart
  pages. Long warning sets create continuation pages instead of truncating alerts.
- `chart_images()` returns eight PNGs: four funding/credit charts, then environment,
  tactical, risk-capital and liquidity-pressure charts. Consumers verify this count.
- `reading.py` only formats existing observations; it never recalculates scores.
- All severe notices remain visible. Only lower-priority web observations may be
  collapsed after the first three. Data gaps never enter that collapsed group.
- Original scoring confidence is explicitly not a prediction-accuracy estimate.
- Missing dates remain gaps. Reconstructed history remains distinct from observed
  records; old liquidity versions cannot drive current-version changes.
- Frozen reports from v1.2 through v1.7.4 remain verifiable without altering them.

## Verification

Regression tests cover section order, original-date preservation, read-only rendering,
missing-score semantics, dynamic matrix source, critical-warning retention, archive
integrity and PDF continuation pages. Visual QA checks desktop, 390px and 320px HTML
and email, expanded details, loaded images and six rendered PDF pages. Production
acceptance additionally requires release hashes and unchanged frozen scheduled
reports, scheduled receipts, producer files, configuration and timers.
