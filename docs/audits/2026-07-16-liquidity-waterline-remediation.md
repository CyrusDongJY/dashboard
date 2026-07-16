# Liquidity Waterline V2 Remediation

Date: 2026-07-16

Scope: remediation of `2026-07-16-liquidity-waterline-audit.md`. The original
audit is retained unchanged as evidence. The monitor remains in shadow mode.

## Finding disposition

| Finding | Status | Remediation |
| --- | --- | --- |
| LIQ-001 | Fixed | Split futures M2/M1 contango and spot VIX/VIX3M-derived contango into separate columns, metrics, and weights. Added a mixed-source regression test. |
| LIQ-002 | Fixed | Funding and credit headline states now require both a weak historical score and an absolute stress guard. Relative-only weakness is emitted as a warning. |
| LIQ-003 | Fixed | Build history on the union of market, macro, and liquidity source dates before merging. Added a missing-market-date regression test. |
| LIQ-004 | Accepted with control | Official retrieval remains first. Database fallback accepts only rows carrying native source dates and therefore remains fail-closed for old FRED rows. HTTP retries reduce transient failures without manufacturing source dates. Coverage must be observed in production. |
| LIQ-005 | Fixed | Added bounded exponential-backoff retries and DTS pagination. Added API fixture and retry-policy tests. |
| LIQ-006 | Fixed | Replaced percentage change of net liquidity with 20-business-day USD amount change. |
| LIQ-007 | Fixed | Missing core coordinates render an explicit insufficient-data message and no neutral scatter point. |
| LIQ-008 | Fixed | Upsert status is surfaced in the result/email and failures are sent to the data-quality logger. |
| LIQ-009 | Fixed | TGA five-day change uses a strict five-business-day lookup. |
| LIQ-010 | Fixed | Added boundary, merge-priority, source-date, pagination, persistence, and chart-semantics tests. |

## Verification gate

- Run all unit tests and Python compilation before deployment.
- Confirm `calc_version = liquidity_v2` in the newest database row.
- Observe at least 3-5 complete trading days before adjusting absolute guards.
- Keep `shadow_mode = true`; no alert-gate or position-sizing integration is approved.
