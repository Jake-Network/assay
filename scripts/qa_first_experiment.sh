#!/usr/bin/env bash
# §18.3 scenario 1: from an empty directory to the first recorded experiment, timed. Target: < 2 minutes,
# no Docker. Uses a throwaway venv and data directory, so it does not touch your real ledger.
# Usage: scripts/qa_first_experiment.sh [package spec, default: this checkout]
set -euo pipefail
SRC="${1:-$(cd "$(dirname "$0")/.." && pwd)}"
WORK="$(mktemp -d)"
export ASSAY_DATA_DIR="$WORK/pg" XDG_STATE_HOME="$WORK/state"
unset ASSAY_DATABASE_URL ASSAY_PROJECT
start=$(date +%s)
cd "$WORK"
uv venv -q .venv
uv pip install -q --python .venv/bin/python "$SRC"
mkdir repo && cd repo && git init -q
../.venv/bin/assay init --goal "Smoke test" --metric score --direction max
../.venv/bin/assay exp record --config '{"lr": 0.1}' --metrics '{"score": 0.5}'
end=$(date +%s)
echo "first experiment recorded in $((end - start)) s (target < 120 s)"
"$WORK/.venv/bin/python" -c "from assay.db.conn import stop_embedded; stop_embedded()"
rm -rf "$WORK"
