#!/usr/bin/env bash
# Run collector for BOTH arms (§13.2, B1): installed as "uv" early on PATH for every agent. It runs the real
# command unchanged and appends a start line and an end line per training run to $AR_BENCH_LOG (shim_log.py).
# The agent sees the same output and exit code as without the wrapper. A run killed by a signal still gets
# an end line ("killed"); one killed with SIGKILL keeps only its start line, and the report counts it.
#   AR_BENCH_LOG     file to append to          AR_BENCH_AGENT     agent label (gpu<n>)
#   AR_BENCH_REAL    the real uv binary          AR_BENCH_REAL_CMD  a command used instead of "uv run train.py" (simulator)
#   AR_BENCH_PY      python that runs shim_log   AR_BENCH_HELPER    path of shim_log.py
# In arm B, assay ar run/replicate set ASSAY_AR_RUN and ASSAY_AR_SEED; both are logged.
set -u
if [ "${1:-}" != "run" ] || [ "${2:-}" != "train.py" ]; then
  exec "${AR_BENCH_REAL:-uv}" "$@"
fi
PY=${AR_BENCH_PY:-python3}
id=$("$PY" "$AR_BENCH_HELPER" start) || id=""
tmp=$(mktemp)
finish() { if [ -n "$id" ]; then "$PY" "$AR_BENCH_HELPER" end "$id" "$1" "$tmp" || true; fi; }
trap 'finish killed; rm -f "$tmp"; exit 143' TERM INT HUP
if [ -n "${AR_BENCH_REAL_CMD:-}" ]; then
  bash -c "$AR_BENCH_REAL_CMD" 2>&1 | tee "$tmp"
else
  "${AR_BENCH_REAL:-uv}" "$@" 2>&1 | tee "$tmp"
fi
code=${PIPESTATUS[0]}
trap - TERM INT HUP
finish "$code"
rm -f "$tmp"
exit "$code"
