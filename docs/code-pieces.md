# Coding work: pieces and path occupancy

Part of [the code pack](code.md).

## Path occupancy

Two pieces that change the same file end in a merge conflict. Before a piece starts, the agent that will work on it
declares the paths it will change; a second piece whose paths overlap a live one's is refused, and the refusal names
the first piece's task id.

```bash
benchbook piece start media-upload --paths 'src/app/modules/media/*' --paths 'tests/media/test_upload.py' \
    [--tags mod:media] [--lease 7200]
benchbook piece list [--tags mod:media] [--all-paths]
benchbook piece done media-upload [--failed] [-m "merged in #42"]
```

Exit codes: 0 done, 1 a user error (a bad glob, no `--paths` on a first start, nothing to release), 2 refused:

```
error: t:3f2a91c0 by impl-a already holds these paths (paths): src/app/modules/media/upload.py and its
src/app/modules/media/* (both match src/app/modules/media/upload.py): piece media-upload
```

**A piece is a task.** `piece start` creates a worker intent (the mechanism the ml-research pack uses to occupy
knobs): a task tagged `intent`, `piece` and `piece:<name>`, held by the agent that ran the command, with the spec
`{"piece", "paths", "declared"}`. While it is live, its branch is gated by the Claude Code completion gate
(docs/claude-code.md, `benchbook hook task-completed`): a task completed there needs a passing run of every
declared check at HEAD. A branch with no live piece is not gated, so a lead on its own feature branch completes its
to-do items freely.

**What a piece occupies.** Globs are read as in `[guard]`, from the repository root, and expanded once, at start,
against `git ls-files`: the tracked files a glob matches go into `paths` (escaped, so `app/[id]/page.tsx` stays that one
file). A glob that matches no tracked file, or more than 200, is kept as the glob, which occupies any file it matches,
new ones included. `declared` keeps what was typed; a path typed exactly as a tracked file is that file, brackets
included. A file added later is covered only if a kept glob covers it, so to reserve a file you will create, name it (or
a glob that matches no tracked file yet) in `--paths`.

**When two pieces overlap.** When some path matches an entry of each: the same file, a file under the other's
glob, or two globs that could both match one path (`src/new/**` and `src/*/x.py` both match `src/new/x.py`).
The glob-against-glob test is exact and errs on the side of refusing: a stored pattern it cannot read counts as
overlapping. A piece also cannot take a piece name another agent holds. The check runs in the ledger inside the
same advisory lock as the exclusive-key check, so of two pieces started at the same moment exactly one wins.

**How long it lasts.** While the intent's lease is live: 7200 seconds by default (`--lease`).

- *Renew:* run `piece start <piece>` again as the same agent. Without `--paths` it renews the lease and keeps the
  paths; with `--paths` it replaces them (checked against everyone else's, the agent's own other pieces included).
  It is the same task, so the piece keeps its id and its tags (tags given on a restart are not added).
  `benchbook task heartbeat <task>` also renews, with the project's default lease (900 seconds) unless `--lease`
  is given. Both renew only a live lease, and every lease counts from the database's clock after the overlap check's
  lock. Once a lease has run out another piece may hold the paths: a heartbeat fails with "lease lost", and
  `piece start <piece>` starts a new piece through the overlap check, taking the paths the expired piece declared
  unless `--paths` is given. A piece released with `piece done` needs `--paths` again. Renewing, restarting and
  releasing each read the current generation in the same transaction, under the project intent lock.
- *Release:* `piece done <piece>` by the holder marks the task done (`--failed`: failed). A lead (orchestrator or
  human) running it on another agent's piece cancels that task, for instance after merging it. `check verify` does
  not release a piece: a refuted completion goes back to its implementer, who still needs the paths.
- *Expire:* when the lease runs out the paths are free; nobody needs to act. A piece started again after that is a
  new task.

**Who holds what.** `piece list` prints one line per live piece (task id, piece, agent, time left, extra tags,
declared globs) and its occupied paths. The same rows are in the reader view `v_inflight` (`spec` holds `piece`,
`paths` and `declared`; `tags` holds the module tags), so a brief can show occupied paths per module:

```
SELECT spec ->> 'piece' AS piece, agent, lease_left_s, spec -> 'paths' AS paths
FROM v_inflight WHERE is_intent AND tags @> ARRAY['mod:media']
```

**Under tokens** (`benchbook_writer` login) the piece is the token's agent's: only that agent can renew it or mark
it done, and a lead's token can cancel it. On an owner connection the agent is `--agent` / `BENCHBOOK_AGENT`.
Workers need `settings.worker_intents` (JSON `true`), which `init --pack code` sets; in a project without it a lead
sets it with `Ledger.update_settings({"worker_intents": True})`, or starts the pieces itself. The database enforces
that setting and the lease cap ([multi-machine.md](multi-machine.md#what-it-prevents-and-what-it-does-not)).

**A limit under tokens:** the overlap check (piece name and paths) runs in the SDK, not in the database. A worker
that writes the task row with its own SQL on the writer login can still take a piece name or paths another agent
holds, for at most the lease cap; the row is in its name, and `piece list` and `v_inflight` show it. Path occupancy
prevents accidental collisions between agents that use benchbook, not a writer set on causing one.

From the SDK the same check is available to any intent: `create_intent(..., exclusive_paths=["paths"])` and
`update_intent(..., exclusive_paths=["paths"])` refuse when the named spec field (a list of globs) overlaps the same
field of a live intent.
