#!/bin/bash
set -euo pipefail
umask 077
ROOT="${CORE_FUNDING_ROOT:-/home/winters_dong426/core_funding_monitor}"
export OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 MKL_NUM_THREADS=1 NUMEXPR_NUM_THREADS=1
export PYTHONNOUSERSITE=1 MPLBACKEND=Agg
export MPLCONFIGDIR="$ROOT/cache/matplotlib" XDG_CACHE_HOME="$ROOT/cache"
unset PYTHONPATH PYTHONHOME
mkdir -p "$ROOT/logs" "$ROOT/cache" "$ROOT/reports"
exec >> "$ROOT/logs/job_$(date -u +%F).log" 2>&1
exec 9>"$ROOT/job.lock"
if ! /usr/bin/flock -n 9; then
    printf '%s SKIPPED_ALREADY_RUNNING\n' "$(date -Is)"
    exit 0
fi
# Shared host: do not start when memory is already tight.
AVAILABLE=$(awk '/^MemAvailable:/ {print $2}' /proc/meminfo)
if [ "${AVAILABLE:-0}" -lt 819200 ]; then
    printf '%s SKIPPED_LOW_MEMORY available_kb=%s\n' "$(date -Is)" "$AVAILABLE"
    exit 75
fi
ulimit -c 0
ulimit -v 1228800
ulimit -t 480
cd "$ROOT/current"
printf '%s START\n' "$(date -Is)"
set +e
# A 16:50 ET start must leave margin before the host's 17:00 shutdown.
/usr/bin/timeout --kill-after=30s 8m /usr/bin/nice -n 19 /usr/bin/ionice -c 3 \
    "$ROOT/.venv/bin/python" -s core_funding_job.py \
    --config "$ROOT/config.json" --output-dir "$ROOT/reports" "$@"
STATUS=$?
set -e
printf '%s END exit=%s\n' "$(date -Is)" "$STATUS"
exit "$STATUS"
