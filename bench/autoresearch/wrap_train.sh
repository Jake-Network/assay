#!/usr/bin/env bash
# Arm A run collector (§13.2): installed as `uv` early on PATH for arm A agents only. It runs the real
# command unchanged and appends one JSON line per training run to $AR_BENCH_LOG. The agent sees the same
# output and exit code as without the wrapper.
#   AR_BENCH_LOG    file to append to          AR_BENCH_AGENT   agent label (gpu<n>)
#   AR_BENCH_REAL   the real uv binary, or a command used instead of "uv run train.py" (simulator)
set -u
if [ "${1:-}" != "run" ] || [ "${2:-}" != "train.py" ]; then
  exec "${AR_BENCH_REAL:-uv}" "$@"
fi
now() { python3 -c 'import time; print(time.time())'; }  # date +%N is GNU-only
start=$(now)
tmp=$(mktemp)
if [ -n "${AR_BENCH_REAL_CMD:-}" ]; then
  bash -c "$AR_BENCH_REAL_CMD" 2>&1 | tee "$tmp"
else
  "${AR_BENCH_REAL:-uv}" "$@" 2>&1 | tee "$tmp"
fi
code=${PIPESTATUS[0]}
end=$(now)
python3 - "$tmp" "$start" "$end" "$code" <<'PY' || true
import ast, hashlib, json, os, re, subprocess, sys
log, start, end, code = sys.argv[1], float(sys.argv[2]), float(sys.argv[3]), int(sys.argv[4])
text = open(log, encoding="utf-8", errors="replace").read()
src = open("train.py", "rb").read()
try:
    h = hashlib.sha256(ast.unparse(ast.parse(src.decode("utf-8"))).encode()).hexdigest()
except Exception:
    h = "raw-" + hashlib.sha256(src).hexdigest()
m = {k: float(v) for k, v in re.findall(r"^(\w+):\s+([-\d.eE+]+)\s*$", text, re.M)}
try:
    commit = subprocess.run(["git", "rev-parse", "--short=7", "HEAD"], capture_output=True, text=True).stdout.strip()
except OSError:
    commit = ""
rec = {"agent": os.environ.get("AR_BENCH_AGENT"), "start": start, "end": end, "exit": code, "code_hash": h,
       "commit": commit, "val_bpb": m.get("val_bpb"), "peak_vram_mb": m.get("peak_vram_mb"), "cwd": os.getcwd()}
with open(os.environ["AR_BENCH_LOG"], "a", encoding="utf-8") as f:
    f.write(json.dumps(rec) + "\n")
PY
rm -f "$tmp"
exit "$code"
