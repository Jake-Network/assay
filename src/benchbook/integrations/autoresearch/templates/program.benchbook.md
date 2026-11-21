<!--
program.benchbook.md: the autoresearch agent instructions for a swarm that shares an benchbook ledger.
Derived from program.md in https://github.com/karpathy/autoresearch (MIT License, per that repository's README).
Written by `benchbook ar init`. Changes from upstream: the experiment loop goes through `benchbook ar` commands,
so runs are shared, never repeated, never lost, and only replicated results become the shared baseline.
-->

# autoresearch, as one lab

This is an experiment to have the LLM do its own research, together with other agents on other GPUs.
You are one of several agents. Each of you has one GPU and a copy of this repository. You share a
research ledger through the `benchbook ar` commands. Nobody leads; a deterministic referee (not an LLM)
decides which results are real by having other agents re-run them.

## Setup

1. **Your identity**: `BENCHBOOK_AGENT` is set for you (e.g. `gpu0-myhost`). Check with `echo $BENCHBOOK_AGENT`.
   If it is empty, stop and ask the human to set it.
2. **Read the in-scope files**: The repo is small. Read these files for full context:
   - `README.md` — repository context.
   - `prepare.py` — fixed constants, data prep, tokenizer, dataloader, evaluation. Do not modify.
   - `train.py` — the file you modify. Model architecture, optimizer, training loop.
3. **Verify data exists**: Check that `~/.cache/autoresearch/` contains data shards and a tokenizer. If not,
   tell the human to run `uv run prepare.py`.
4. **Read the brief**: `benchbook ar brief`. It shows the verified best, what others are doing now, and what has
   been tried.
5. **Go.** Do not create results.tsv by hand; `benchbook ar run` writes it.

## Experimentation

Each experiment runs on a single GPU. The training script runs for a **fixed time budget of 5 minutes**
(wall clock training time, excluding startup/compilation). You never launch it yourself: `benchbook ar run` does.

**What you CAN do:**
- Modify `train.py` — this is the only file you edit. Everything is fair game: model architecture, optimizer,
  hyperparameters, training loop, batch size, model size, etc.

**What you CANNOT do:**
- Modify `prepare.py`. It is read-only. It contains the fixed evaluation, data loading, tokenizer, and training
  constants (time budget, sequence length, etc).
- Install new packages or add dependencies. You can only use what's already in `pyproject.toml`.
- Modify the evaluation harness. The `evaluate_bpb` function in `prepare.py` is the ground truth metric.

**The goal is simple: get the lowest val_bpb.** Since the time budget is fixed, you don't need to worry about
training time — it's always 5 minutes. The only constraint is that the code runs without crashing and finishes
within the time budget.

**VRAM** is a soft constraint. Some increase is acceptable for meaningful val_bpb gains, but it should not blow
up dramatically.

**Simplicity criterion**: All else being equal, simpler is better. A small improvement that adds ugly complexity
is not worth it. Conversely, removing something and getting equal or better results is a great outcome — that's
a simplification win. A 0.001 val_bpb improvement that adds 20 lines of hacky code? Probably not worth it.
A 0.001 val_bpb improvement from deleting code? Definitely keep. An improvement of ~0 but much simpler code? Keep.

**The first run**: If the brief shows no runs in your partition yet, your very first run establishes the
baseline: run the training script as is (`benchbook ar claim "baseline"` then `benchbook ar run -m "baseline"`).
If the baseline already ran, `benchbook ar run` refuses to repeat it; go straight to the loop.

**Noise**: the same code gives slightly different val_bpb on every run. The referee measures this noise once
per partition (hardware + prepare.py) and only counts an improvement larger than epsilon. A single good run is
a *candidate*; it becomes *verified* only after two other agents re-run the same code and the mean still wins.

## The experiment loop

LOOP FOREVER:

```
LOOP FOREVER:
0. benchbook ar replicate --auto        # 남의 후보를 재현할 차례면 자동으로 돈다. 생각할 필요 없음.
1. benchbook ar sync                    # 재현된 최고 기록이 내 것보다 좋으면 가져온다.
2. benchbook ar brief                   # 무엇이 이미 시도됐고, 누가 지금 무엇을 하는지 읽는다.
3. 다음 아이디어를 정한다. brief의 "진행 중"과 "knob 이력"에 있는 것은 피한다.
4. benchbook ar claim "<한 줄 설명>" --knob K=V ...   # 거부되면 다른 아이디어로. 최대 5번.
5. train.py를 수정하고 git commit.
6. benchbook ar run -m "<같은 설명>"    # 직접 `uv run train.py`를 실행하지 않는다.
7. 출력이 KEEP이면 커밋 유지, DISCARD, CRASH, INVALID면 git reset --hard HEAD~1.
   (KEEP은 후보일 뿐이다. 나중에 sync가 refuted된 후보를 되돌릴 수 있다.)
8. 배운 것이 있으면 benchbook ar note "...", 다음에 해볼 만한 것은 benchbook ar idea "...".
```

In English, for clarity (the block above is the rule):

0. `benchbook ar replicate --auto` — if it is your turn to re-run someone else's candidate, it runs automatically in
   a separate directory (your train.py and git are untouched). Exit code 3 means nothing to replicate.
1. `benchbook ar sync` — adopt the verified best when it beats your code (it commits the new train.py for you).
2. `benchbook ar brief` — read what has been tried and what others are doing right now.
3. Choose the next idea. Avoid what the brief lists under "In progress" and in the "Knob history".
4. `benchbook ar claim "<one-line idea>" --knob K=V ...` — if it is refused (exit 2), pick another idea; at most 5 tries.
   Use `--knob` for every UPPER_CASE constant you will change; skip it for code changes.
5. Edit `train.py` and `git commit`.
6. `benchbook ar run -m "<the same one-line idea>"` — never run `uv run train.py` directly.
7. If the output starts with KEEP, keep the commit; if DISCARD, CRASH or INVALID, `git reset --hard HEAD~1`.
   KEEP means better than your previous kept code (your parent), like upstream; it only means candidate, and a
   later sync may move you off a candidate that was refuted.
8. If you learned something, `benchbook ar note "..."`; for something worth trying next, `benchbook ar idea "..."`.

**Rules that come with sharing:**
- Do not write results.tsv yourself; `benchbook ar run` appends to it in the upstream format.
- If you doubt another agent's result, record it with `benchbook ar idea "..."`. Do not judge results; the referee does.
- When an benchbook command prints an error, do what it says. Do not repeat the same command unchanged.
- A crash: if it is something dumb and easy to fix (a typo, a missing import), fix it, commit, and `benchbook ar run`
  again. If the idea itself is broken, `git reset --hard HEAD~1` and move on. The crash is already recorded.

**Timeout**: `benchbook ar run` kills a run after 10 minutes and records it as a failure. A run that trains
longer than the time budget in `prepare.py` is recorded as INVALID.

**Waiting for a run**: one `benchbook ar run` takes about 6 minutes, longer than a shell tool's default timeout
(2 minutes in Claude Code). Always give the command the longest timeout your tool allows (Claude Code Bash:
`timeout: 600000`), or start it in the background with its output in a file
(`benchbook ar run -m "..." > ar_run.out 2>&1`, run in the background) and wait until that file shows the verdict.
Never start a second `benchbook ar run` while one is running. If the command gets killed anyway, the run is
recorded as interrupted and the training is stopped; run it again with `--retry-failed`.

**NEVER STOP**: Once the experiment loop has begun (after the initial setup), do NOT pause to ask the human if you
should continue. Do NOT ask "should I keep going?" or "is this a good stopping point?". The human might be asleep,
or gone from a computer and expects you to continue working *indefinitely* until you are manually stopped. You are
autonomous. If you run out of ideas, think harder — read the brief's knob history and observations, read papers
referenced in the code, re-read the in-scope files for new angles, try combining previous near-misses, try more
radical architectural changes. The loop runs until the human interrupts you, period.
