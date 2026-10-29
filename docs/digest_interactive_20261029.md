# Digest v1.9: offline date selection and verified supplementation

## Display contract

- The HTML attachment has synchronized 30/90/150-day, full-archive and custom
  date controls for all ten time-series charts. Pointer and keyboard inspection
  show dated observations; filtering never recomputes scores or changes the
  report cutoff. "All" means all included, eligible archived data, not unlimited
  history. Empty ranges show no data, not zero.
- Daily series preserve native exchange/funding calendars; weekly observations
  keep their native cadence. Real missing dates break lines. Producer versions,
  original observations and reconstructed references remain separate series.
- Score axes remain 0-100 and pressure guides remain the existing 65/35 values.
  Units, dates and reference labels remain visible.
- Only two SHA-256-authorized inline scripts are permitted by the HTML CSP.
  Data is JSON-escaped; no eval, external JavaScript, network calls or storage.
  Mail/PDF stay static. With JavaScript disabled or during printing, the original
  PNGs remain available. HTML must be opened in a browser for interaction.
- Index retrieval expands from 150 to a bounded 430 calendar days. Static views
  retain 150 days. Pagination, timestamps, eligibility and version gates remain.

## Data audit

- October 26-29 scheduled send receipts are SENT/smtp_accepted. This confirms
  sending-server acceptance, not QQ inbox delivery.
- October 29 report has all core raw inputs. Remaining gaps include new-version
  live histories (core 4/6, liquidity/environment/risk-capital 3/6), an absent
  October 21 original tactical component record, and September 30 VWAP values
  whose source dates are September 29. Do not relabel those stale observations.
- September 30 risk-capital summary/member rows are absent. They cannot be
  treated as original observations; reconstruction must stay separate.
- Cloud ICI GET returns HTTP403. The public official release of October 28 was
  independently inspected and five domestic-equity weekly values transcribed.
  `verified_import` validates the pinned URL, units, dates and SHA256, records
  actual import time, explicitly identifies the transcription digest, and
  appends observations without editing old reports or delivery receipts.
- The ICI structural-activity footnote is retained; raw flows are not used to
  infer investor withdrawals or feed scores. The report explicitly says this
  is one-off supplementation, not a repaired automatic connection.
- Later failed refreshes retain CACHED/CACHED_AFTER_ERROR semantics. A one-off
  import does not guarantee future publication coverage.

## Acceptance

Tests cover script hashes/escaping, date bounds, gaps, version separation,
extended pagination, import time/cutoff, wrong sources, invalid values and
idempotency. Browser checks cover 1440/390/320px, all presets, custom and invalid
ranges, keyboard inspection, no remote requests, print and no-JavaScript fallback.
Production activation additionally requires release hashes, a rollback copy,
unchanged scheduled reports/receipts, producer code, configuration and timers.
New data appears only in a separately identified correction or future reports.

Local acceptance: 713 main regression tests plus 5 isolated report-format tests
passed; 2 optional tests skipped. Browser checks passed at all three widths with
ten active charts, four 65/35 guide pairs, zero external requests and no overflow.
