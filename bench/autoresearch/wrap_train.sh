#!/usr/bin/env bash
# Run collector for BOTH arms (§13.2, B1): installed as uv and PATH Python aliases. It runs the real
# command unchanged and appends a start line and an end line per training run to $AR_BENCH_LOG (shim_log.py).
# The agent sees the same output and exit code as without the wrapper. A run killed by a signal still gets
# an end line ("killed"); one killed with SIGKILL keeps only its start line, and the report counts it.
#   AR_BENCH_LOG     file to append to          AR_BENCH_AGENT     agent label (gpu<n>)
#   AR_BENCH_REAL    the real uv binary          AR_BENCH_REAL_CMD  a command used instead of "uv run train.py" (simulator)
#   AR_BENCH_PY      python that runs shim_log   AR_BENCH_HELPER    path of shim_log.py
# In arm B, assay ar run/replicate set ASSAY_AR_RUN and ASSAY_AR_SEED; both are logged.
set -u
command=${0##*/}
case "$command" in
  uv) real=${AR_BENCH_REAL:?missing real uv path} ;;
  python) real=${AR_BENCH_REAL_PYTHON:?missing real python path} ;;
  python3) real=${AR_BENCH_REAL_PYTHON3:?missing real python3 path} ;;
  *) exit 125 ;;
esac
if [ "${AR_BENCH_WRAPPED:-}" = 1 ]; then
  exec "$real" "$@"
fi
candidate=0
for arg in "$@"; do
  case "$arg" in train.py|*/train.py) candidate=1; break ;; esac
done
if [ "$candidate" = 0 ]; then
  exec "$real" "$@"
fi
PY=${AR_BENCH_PY:-python3}
id=$("$PY" "$AR_BENCH_HELPER" start "$command" "$@") || exit 125
if [ -z "$id" ]; then
  exec "$real" "$@"
fi
export AR_BENCH_WRAPPED=1
tmp=$(mktemp) || exit 125
finish() { "$PY" "$AR_BENCH_HELPER" end "$id" "$1" "$tmp"; }
trap 'finish killed; rm -f "$tmp"; exit 143' TERM INT HUP
if [ "$command" = uv ] && [ -n "${AR_BENCH_REAL_CMD:-}" ]; then
  bash -c "$AR_BENCH_REAL_CMD" 2>&1 | tee "$tmp"
else
  "$real" "$@" 2>&1 | tee "$tmp"
fi
code=${PIPESTATUS[0]}
trap - TERM INT HUP
finish "$code" || code=125
rm -f "$tmp"
exit "$code"
