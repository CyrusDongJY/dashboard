# Digest Historical Reference Recovery

Release: `liquidity_digest_v1.7.3-shadow`.

## Cause

The report previously required LIVE_SHADOW records from the newest temporal and
producer versions. New liquidity/environment formulas and added member signatures
therefore reduced valid historical observations to one point, even when original
database inputs still existed. Daily top-500 membership also changed, correctly
preventing fixed-cohort persistence from using an unverified aggregate history.

## Separation of Evidence

- Published scores, timestamps, modes, archived reports and delivery receipts stay
  unchanged. Supplemental references live in `temporal_state_daily.details.history_reference`.
- References contain explicit REPLAY rows, actual calculation time, input hashes,
  provenance and `point_in_time_eligible=false` / `alert_eligible=false`.
- Display acceptance requires a matching actual anchor, compatible producer and
  temporal versions, matching membership signature, valid digest, and pre-cutoff
  calculation. Missing interior sessions still block direction calculations.
- Reference matrix direction and dashed curves never enter original alerts or
  macro/micro votes. Later compatible actual snapshots may extend the reference.
- Dynamic top-500 breadth is separately named; it does not replace fixed-cohort
  persistence or recreate old advancing/flat/declining counts.

## Verified Recovery Scope

Cloud dry run for 2026-10-26:

| Panel | Valid reference observations | Latest risk-direction value |
| --- | ---: | ---: |
| Environment | 20 | 72.2 |
| Liquidity | 6 | 67.7 (support score 32.3) |
| Risk capital | 48 | 60.0 (diffusion score 40.0) |
| Tactical stress | 66 | 61.0 |

The October 21 breadth archive contains original top-500 proportions and a source
hash. Only that recovered day's missing percentile baseline is reconstructed from
documented original top-500 observations; no database metric row is overwritten.
The October 21 tactical score remains unavailable because its original component
evidence is incomplete.

Seventeen additional standard-month VX observations (July 1-24) were recovered and
read back, preserving all other fields. Extending a percentile baseline changes
the current recalculation slightly, so the displayed liquidity reference uses the
private input frame captured before this extension, matching the issued score.
The expanded raw database remains available to future producer calculations.

## Operator Workflow

`backfill_digest_reference.py` is dry-run by default. Supply the existing private
configuration, as-of date and a new backup directory. Review the saved bundles and
anchor validation before adding `--write`. That flag modifies only the JSON details
with compare-and-set and full-row readback; it requires no schema migration.

An optional `--liquidity-frame` accepts a trusted private audit snapshot only,
together with its mandatory SHA-256. Never load untrusted pickle files. Use a fresh
backup directory for each attempt. Re-run source scans if publication inputs change;
never loosen anchor checks to force agreement.

Frozen corrections must use a new correction revision and a real collection cutoff.
Do not rebuild an issued morning report in place or reset its delivery receipt.
