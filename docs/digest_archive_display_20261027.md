# Archived History and Endpoint Changes

Release: `liquidity_digest_v1.7.4-shadow`.

## Findings

The latest report omitted available history for two different reasons:

1. The water-level chart admitted only the new fixed-weight version. The database
   contains 72 published records from July 16 through October 26, of which 70 have
   scores: 69 belong to older models and one belongs to the current model.
2. Tactical changes were taken from LIVE_SHADOW temporal records, which had just
   restarted after a wrapper upgrade. This also conflated endpoint changes with
   a requirement for a complete intervening window.

Original tactical scores are 19 (September 25), 41 (October 19), 54 (October 23)
and 61 (October 26). The 1/5/21 NYSE-session net changes are therefore +7/+20/+42.
October 21 is still missing; the net increase does not establish an uninterrupted
daily rise. No missing score is inferred or written into the database.

## Changes

- Restore older water-level observations as separately labelled archive segments.
  Known version transitions and missing observations break lines. Old scores are
  not substituted into the current model, its deltas, or alerts.
- Calculate tactical endpoint changes from the original `market_history` records.
  Validate the formula version, component metadata, source identity/date, NYSE
  staleness, weighted coverage, confidence, and reproducibility of each score.
  Publish exact baseline dates/scores and missing interior dates in report JSON.
- Name the direction a *net change*, separate from daily persistence. It is not
  a new alert or macro/micro vote.
- Add the existing 65 high-pressure and 35 low-pressure reference lines to the
  market-transmission chart. Values come from `temporal_monitor`, not new rules.
  These are descriptive shadow thresholds, not execution triggers.

## Deployment Boundary

Only the independent digest changes. No database migration or historical score
overwrite is required. Producer programs, source collection, archived reports,
delivery receipts, and the 09:20 America/New_York schedule remain unchanged.
Issue a separate correction tied to the latest scheduled report after cloud,
PDF and desktop/mobile HTML checks.
