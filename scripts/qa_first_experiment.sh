#!/usr/bin/env bash
# §18.3 scenario 1: from an empty directory to the first recorded experiment, timed. Target: < 2 minutes,
# no Docker. Uses a throwaway venv and data directory, so it does not touch your real ledger.
# Usage: scripts/qa_first_experiment.sh [package spec, default: this checkout]
set -euo pipefail
SRC="${1:-$(cd "$(dirname "$0")/.." && pwd)}"
WORK="$(mktemp -d)"
cleanup() {
    if [[ -x "$WORK/.venv/bin/python" && -f "${ASSAY_DATA_DIR:-}/postmaster.pid" ]]; then
        "$WORK/.venv/bin/python" -c "from assay.db.conn import stop_embedded; stop_embedded()" || true
    fi
    rm -rf -- "$WORK"
}
trap cleanup EXIT
# The data dir has a space on purpose: macOS's default (~/Library/Application Support/assay/pg) does too.
export ASSAY_DATA_DIR="$WORK/data dir/pg" XDG_STATE_HOME="$WORK/state"
unset ASSAY_DATABASE_URL ASSAY_PROJECT ASSAY_AGENT
start=$(date +%s)
cd "$WORK"
uv venv -q .venv
uv pip install -q --python .venv/bin/python "$SRC"
mkdir repo && cd repo && git init -q
../.venv/bin/assay init --goal "Smoke test" --metric score --direction max
ASSAY_AGENT=qa ../.venv/bin/assay exp record --config '{"lr": 0.1}' --metrics '{"score": 0.5}'
end=$(date +%s)
elapsed=$((end - start))
echo "first experiment recorded in $elapsed s (target < 120 s)"
if (( elapsed >= 120 )); then
    exit 1
fi
