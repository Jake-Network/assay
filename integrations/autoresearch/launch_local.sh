#!/usr/bin/env bash
# One machine, N GPUs, one agent per GPU (docs/autoresearch.md, scenario S1).
# Run from the root of an autoresearch checkout whose data is prepared (uv run prepare.py):
#   bash /path/to/assay/integrations/autoresearch/launch_local.sh 4 [project]
# Prints one command per GPU; with AR_TMUX=1 it opens them as tmux windows instead.
set -euo pipefail
N=${1:?usage: launch_local.sh <gpus> [project]}; P=${2:-autoresearch}; TAG=$(date +%b%d | tr '[:upper:]' '[:lower:]')
assay ar init --project "$P"
for i in $(seq 0 $((N - 1))); do
  [ -d "../ar-gpu$i" ] || git worktree add -q "../ar-gpu$i" -b "autoresearch/$TAG-gpu$i"
  cp program.assay.md "../ar-gpu$i/"
  CMD="cd ../ar-gpu$i && CUDA_VISIBLE_DEVICES=$i ASSAY_AGENT=gpu$i-$(hostname -s | cut -c1-8) ASSAY_PROJECT=$P claude 'Read program.assay.md and start.'"
  if [ "${AR_TMUX:-0}" = "1" ] && command -v tmux >/dev/null; then
    tmux has-session -t "ar-$P" 2>/dev/null || tmux new-session -d -s "ar-$P"
    tmux new-window -t "ar-$P" -n "gpu$i" "$CMD"
  else
    echo "$CMD"
  fi
done
echo "viewer: assay serve   (http://127.0.0.1:7466)"
