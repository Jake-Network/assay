<!--
program.benchbook.codex-autoresearch.md: what to add to a codex-autoresearch session so that its runs are
recorded in a benchbook ledger and its keeps are re-run by other agents before they count.
Written by `benchbook ar gate on --from codex-autoresearch`. Add it to `AGENTS.md`.
The loop stays the skill's own: benchbook does not replace how it measures, keeps or reverts.
-->

## benchbook: shared record and reproduction gate

This checkout shares a research ledger with other agents through the `benchbook ar` commands. `BENCHBOOK_AGENT`
names you (one name per GPU); if `echo $BENCHBOOK_AGENT` prints nothing, stop and ask the human to set it. Your
loop does not change. Three additions:

1. **Before you choose the next idea**, run `benchbook ar replicate --auto`, then `benchbook ar brief`.
   The first re-runs another agent's candidate when it is your turn, in a temporary copy of this checkout: your
   working tree and git history are not touched, and exit code 3 means there is nothing to re-run. The second
   shows what is verified, what waits for replication and what was already tried. Do not repeat what it lists.
2. **After every `finish`**, run
   `benchbook ar import --from codex-autoresearch autoresearch-results --hw {hw}`
   It records the new entries and skips the ones it has already seen, so running it again is harmless.
3. **A keep is a candidate, not a result.** It becomes *verified* only after two other agents re-run the same
   `train.py` and the mean still beats the verified best. `benchbook ar status` shows which keeps are verified,
   refuted or still waiting. Report only verified numbers as improvements.

Rules that come with sharing:

- Do not judge results yourself; a deterministic referee does. If you doubt one, `benchbook ar idea "..."`.
- Do not edit `prepare.py`: it is the evaluator, and a run under another `prepare.py` is compared with nothing here.
- When a benchbook command prints an error, do what it says. Do not repeat the same command unchanged.
- `benchbook ar replicate --auto` runs one full training. Give the command a timeout longer than a run
  (`--timeout` sets when it is killed, 600 seconds by default), or start it in the background and wait for it.
- codex-autoresearch commits every trial before it measures it, so benchbook has the code of discards and crashes
  too. Do not edit `autoresearch-results/` for benchbook's sake: the import only reads `run.json` and `events.jsonl`.
