# Coding work: the brief and record coverage

Part of [the code pack](code.md).

## The brief

`benchbook status` counts each piece at its latest active reported commit, open out-of-scope findings, pending
and dead reports, and recent hook failures. If recording needs attention, the brief leads with the reason and
the `benchbook hook replay` command. The spool and the cached `benchbook statusline` refer to the selected
database and writer.

In a code project `benchbook brief` (MCP `benchbook_brief`) is the code pack's
(`benchbook.packs.code.brief:compile_brief`, `brief_compiler` in pack.toml); other harnesses load the same brief
when a session starts ([harnesses.md](harnesses.md)). Narrowed to a module:

```bash
benchbook brief --tags mod:media                     # comma separated; --budget N (default 2000 tokens)
benchbook brief --tags mod:media,file:src/app/modules/media/
benchbook brief --here                               # what this branch changed: file:<dir>/ tags and piece:<branch>
```

It shows, in this order:

1. **Last verified commit per piece**: the newest `verified` completion of each piece in scope, with how it was
   checked (`checked: re-run`, `checked: person`, ...; the oracle of its last decision named by pack.toml
   `[brief.labels]`).
2. **Re-run, not decided** (only when there is one): the newest completion of each piece that a re-run supported or
   refuted and nobody decided, unless a later completion of the piece is verified; most recently re-run first. Each
   line says why: `passed, needs review (tests weakened: <files>)`, `(fewer tests in <check>)` or `([checks]
   changed)` (a person decides, see [the guard](code-verify.md#added-tests-and-weakened-tests)), `failed (<checks>)`, `runs disagree
   (inconclusive)`, or `passed, not decided` (the run did not count, such as a worker's own run).
3. **Reported done, not re-run**: completions still `proposed` with no supporting and no refuting evidence, newest
   first, each `checked: none`. A report is not a check; this section keeps the two apart.
4. **Discarded approaches, do not repeat**: the claims above for the pieces in scope, each with its approach, piece,
   signature, first attempt and number of failed attempts; most recently failed first.
5. **Open out-of-scope findings**: [findings](code-findings.md#out-of-scope-findings) still `proposed` or `inconclusive`, matched by
   tag or by `file:` prefix; most reported first.
6. **Occupied paths**: live pieces ([path occupancy](code-pieces.md#path-occupancy)) with their holder, time left, tags and paths.

The footer names `benchbook get <id>` for one record's details (at most 400 tokens).

**Changed since the check.** With `--here` (and from the boundary hooks) the brief compares each line with the
checkout's HEAD and ends it with a mark:

| mark | when |
| --- | --- |
| `· applies at HEAD` | the piece's paths are unchanged between its commit and HEAD |
| `· changed since (N commits)` | N commits since the piece's commit changed its paths |
| `· commit not in this checkout` | this checkout lacks the commit |
| `· not merged into this branch` | the commit exists but is not in HEAD's history (a piece's branch read from the base, or the lead's branch before the merge) |
| `· test changed since` | a discarded approach whose failing test file (the signature before `::`) changed since the commit it failed at; the line moves to the end of its section |

The paths compared (`git diff --quiet <commit> HEAD -- <paths>`) are the files the piece's own commits changed since
the merge-base (`payload.files`, recorded by `check verify` when there are at most 200: a merge commit counts with
the files it changed itself, and a commit merged in from another piece is left out when it is that piece's reported
commit, carries its trailer or has its check runs, and nothing ties it to this piece), else those the piece's newest
task occupies, else the directories its completion recorded. A squash-merged piece reads `applies at HEAD` while
its paths match. Only the lines shown are compared, each git call has 2 seconds, and nothing is stored: the same
ledger at the same HEAD gives the same text. Without a checkout (`--tags`, MCP) the brief shows no marks.

**Scope.**

- A tag matches records that carry it exactly (`mod:media`, `piece:x`); a `file:<prefix>` tag matches by prefix
  the `file:` tags of findings and the paths pieces occupy. With several tags the sections hold the union; without
  `--tags` the brief covers the whole project.
- A piece is in scope when one of its tasks (`piece start`, any status) carries a matching tag or occupies a
  matching path. Discarded approaches and verified commits are found through their piece (a discarded approach also
  through its own `mod:` tags), and also carry the directories their commit changed (`payload.paths` and
  `file:<dir>/` tags, at most 20), so a piece never started with `piece start` is found by path. Findings are
  matched by their own tags only, as in the [query](code-findings.md#reading-open-findings).
- `--here` computes the scope from the checkout: the directories the current branch changed since its merge-base
  with the base ref (`[guard] base`, else `origin/main`) plus the working tree's changes and untracked files, the
  deepest lifted to their parent until at most 20 remain, as `file:` tags, plus `piece:<branch>`. It then adds
  `piece:<piece>` for every piece with a completion at a commit in HEAD's last 5,000 commits, and the Scope line ends
  `+ N pieces merged into HEAD`, so on an integration branch the records of the pieces merged into it belong to it.
  On the base branch itself the scope is empty and the brief covers the whole project. The boundary hooks use the
  same scope.

**Budget.** At most 8 items per section (pack.toml `[brief]`), each line cut to a fixed length; then items are
dropped from the last section upwards until the brief fits `--budget` (with the same 5% slack as the default brief),
and each section says how many it left out (`(+3 more: benchbook piece list)`). Header and footer always stay; a
budget too small for them is an error. A character limit (`max_chars`, 6,000 in the boundary hooks) is applied the
same way: whichever limit is reached first trims. Each brief is logged in `brief_log`.

`--pack` is not an option: the project's pack already chooses the compiler. Other packs refuse `--tags` rather than
ignore it; their briefs are unchanged.

## Reads that changed a plan

**Looking an id up.** Every brief line carries an id; `benchbook get <id>` prints that record in at most 400 tokens:
its kind and id, status and how it was checked (`checked: re-run`, `person`, `none`; pack.toml `[brief.labels]`),
its statement or signature and the payload keys that matter, up to five links each way and the last three decisions,
with what was cut counted ("… 45 more (--json)"). `--json` prints the whole record. Each lookup appends one line to
`lookups.jsonl` in the state directory (time, project, id): an id a brief carried that was then looked up is the
cheapest sign that the brief was read.

A read that changed a plan is counted as a claim id cited by a plan document. Two things are recorded:
the claims themselves (with their ids, tags and times) and `brief_log` (one row per brief compiled: project, agent,
task, tokens and time; not what it showed). The count, for one plan document:

```bash
ids=$(grep -oE '\bc:[0-9a-f]{8}\b' plan.md | sort -u | sed "s/.*/('&')/" | paste -sd, -)
benchbook sql "WITH cited(ref) AS (VALUES $ids) ..."     # the statement below, with $ids in VALUES
```

```sql
WITH cited(ref) AS (VALUES ('c:1a2b3c4d'), ('c:5e6f7a8b')),
found AS (
  SELECT c.ref, s.id, s.kind, s.tags, s.status, s.created_at
  FROM cited c LEFT JOIN v_claim_status s ON left(s.id::text, 8) = substr(c.ref, 3)
)
SELECT count(DISTINCT ref) AS cited,
       count(DISTINCT id) AS in_ledger,
       count(DISTINCT id) FILTER (WHERE 'discarded' = ANY(tags)) AS discarded_approaches,
       count(DISTINCT id) FILTER (WHERE 'out-of-scope' = ANY(tags)) AS findings,
       count(DISTINCT id) FILTER (WHERE kind = 'completion' AND status = 'verified') AS verified_commits,
       count(DISTINCT id) FILTER (WHERE ('discarded' = ANY(tags) OR 'out-of-scope' = ANY(tags)
                                         OR (kind = 'completion' AND status = 'verified'))
                                        AND created_at < '<plan written>') AS reads_that_changed_the_plan
FROM found
```

`<plan written>` is when the plan was written (its commit time, `git log -1 --format=%cI -- plan.md`). The last
column counts the cited claims of the kinds the brief shows that existed before the plan. Summed over the plans of a
delegate run it is the number the stop rule reads: after five delegate runs with none, drop signatures and the
brief, and keep verification.

What it can tell: that a plan names a discarded approach, an open finding or a verified commit by its id, and that
the claim was in the ledger before the plan was written. What it cannot: that the id came from a brief rather than
`benchbook search`, `get` or a PR summary (`brief_log` says a brief was compiled, not what it contained, and
`benchbook sql` cannot read it: `Ledger.sql_internal` can); that the citation changed the plan rather than confirmed
it; a read that changed a plan without its id being written down; a plan kept outside files. It counts an upper
bound on citations that came from the brief, and a lower bound on reads that mattered.

## Record coverage

The share of declared check runs that reached the ledger (target 95%): ledger attempts over the runs the
Claude Code hook observed.

- **Denominator.** Every Bash call the hook matches to a declared check appends one line to `observed.jsonl` in the
  spool directory of the database target and writer (`~/.local/state/benchbook/spool/<key>/`; `benchbook doctor`
  prints the path on its `hook failures` line). A line has `ts`, `project`, `piece`, `check`, `kind`,
  `exit_code`, `reason`, `session_id`, `tool_use_id` and `harness` (`claude-code`, `codex`, `cursor`, `gemini`,
  `opencode` or `pi`; docs/harnesses.md; Cursor's lines add `harness_version`). `kind` is `direct`, `wrapped` or
  `unrecordable`, with the `reason` of an unrecordable run (docs/claude-code.md, "`benchbook hook bash-result`").
- **Numerator.** The attempts with `via` `bash-hook` or `check-run` in the ledger.

```bash
grep -c '"project": "my-project"' "$SPOOL/observed.jsonl"          # observed runs
grep '"project": "my-project"' "$SPOOL/observed.jsonl" | grep -c '"kind": "unrecordable"'
benchbook sql "SELECT metrics ->> 'via' AS via, count(*) AS attempts FROM v_leaderboard WHERE metrics ->> 'via' IN ('bash-hook', 'check-run') AND created_at >= '<since>' GROUP BY 1 ORDER BY 1"
```

Coverage is the sum of `attempts` over the observed count, both from the same start (`<since>`: the first
observation of the delegate run measured). Read it with three limits in mind: `check run` used outside Claude Code
adds attempts no hook observed, so keep the window to the measured run; `v_leaderboard` keeps finished attempts
only (an interrupted run is observed but not counted) and at most 50 per piece; and the observation file is local
to the machine whose hooks wrote it.
