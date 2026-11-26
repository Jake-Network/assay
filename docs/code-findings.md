# Coding work: out-of-scope findings

Part of [the code pack](code.md).

## Out-of-scope findings

An implementer often notices problems outside its piece: a bug in a neighbouring module, a flaky test, a missing
index. With one line in `.benchbook/config.toml` the result-capture hook keeps them as claims the lead can decide
on later, instead of leaving them in a report nobody reads again:

```toml
[report]
out_of_scope_heading = "Out of scope"  # or "범위 밖", or any heading text
agents = ["*"]                         # see Which reports are read
```

Ask the implementers to end their final report with that section, one top-level list item per finding:

```markdown
## Out of scope
- `src/app/modules/media/upload.py`: the retry loop ignores the timeout. mod:media
  - reproduced with tests/media/test_upload.py::test_retry
- The auth token refresh races with logout.
```

Nothing else changes for them: no command to run, no id to cite.

### Which reports are read

The same ones the hook already captures (docs/claude-code.md): the final message of a subagent whose type is in
`BENCHBOOK_HOOK_AGENTS`, or the report it handed back with `SubagentHandback` (then the closing text is not read
again). Findings are recorded only in a project created with `--pack code` and only while the heading is set. The
heading is read when the hook fires and kept with the queued report, so `benchbook hook replay` looks for the
section the run had. The hook's background processor parses the section and writes the claims; the hook process
only queues the report and exits. A report that cites its own records gets its findings recorded too (the note is
what such a report skips).

`[report] agents` widens the reports read for findings without making them workers: a list of subagent types
(with or without a plugin prefix), or `["*"]` for every subagent. While `BENCHBOOK_HOOK_AGENTS` is unset, the
final report of a listed subagent is queued for its findings only: no `[auto-captured]` note is written for it.
`BENCHBOOK_HOOK_AGENTS`, when set, still decides alone. `benchbook init --pack code` writes the `[report]` table
above when config.toml has none yet.

### What counts as a finding

- **The section**: a markdown heading of any level, or a line that is bold as a whole (`**Out of scope**`,
  `**Out of scope:**`), whose text equals the configured heading after NFKC normalisation, case folding,
  whitespace collapsing and dropping a trailing colon. A heading section ends at the next heading of the same or a
  higher level (deeper headings inside it are group labels); a bold-line section ends at the next heading of any
  level or the next bold line. A thematic break (`---`) ends either. Every such section in the report is read.
- **One line**: outside a section, a line `<heading>: <text>` (also as a list item, with the heading in bold, or
  with a full-width colon) is one finding whose text is what follows the colon. `Out of scope: none` is nothing.
- **The items**: each top-level list item (`-`, `*`, `+`, `1.`, `1)`; a `[ ]` checkbox is dropped) is one finding.
  Its first paragraph, wrapped lines included, is the finding's text. Nested items and indented paragraphs are its
  details. Paragraphs between the items, and lines in fenced code blocks, are never findings. An item whose text is
  empty or only says there is nothing (`none`, `n/a`, `없음`, `해당 없음`, ...) is not a finding. At most 100 per
  report.
- **Paths**: a token of ASCII letters, digits and `_ . - /` counts as a file path when it has a `/` and either sits
  in backticks, ends in `/` or ends in an extension (`src/app/x.py`, `src/app/modules/media/`); without a `/` only
  when it ends in a common source or config extension (`hooks.py`, `config.toml`, but not `os.path` or
  `hooks.process`). A `:12`, `:12:5` or `#L12` suffix and a leading `./` are dropped. An absolute path counts only
  inside the subagent's working directory, and is made relative to it; a path with `..` never counts. Paths come
  from the item's own text, or from its details when the text names none.

### What is recorded

One claim per distinct finding:

```
kind       observation
statement  the item's text (whitespace collapsed; cut at 500 characters)
tags       out-of-scope
           piece:<piece>     when the run recorded attempts (exp run / exp record) of exactly one piece; else
                             the piece the report's commit names (DONE <sha> or commit: <sha>, then its
                             Benchbook-Piece trailer, a piece: line or the one branch at it), the piece the
                             automatic re-run of the same report verifies
           mod:<name>        each mod:<name> the item's text writes itself (never inferred from a path)
           file:<path>       each path the finding names, repository-relative, up to 10
payload    {"text", "details", "file", "piece", "heading",
            "source": {"session_id", "event", "agent_id", "agent_type", "tool_name"}}
author     hook (the agent of BENCHBOOK_TOKEN when the hook runs under a token)
```

`payload.source` says which subagent reported the finding, also when the author is a token's agent.
`payload.file` is the first path, the one the dedup key uses. The report itself is stored once as a source
(`uri` `subagent-report:sha256:<sha256 of the report text>`, title naming the subagent type, agent and session) and
linked `supports` to every finding it names. The report text itself stays in the auto-captured note.

**One finding, one claim.** The dedup key is
`sha256("benchbook:out-of-scope\0" + file + "\0" + normalised text)`, where file is the first path ("" for none) and
the text is normalised by dropping backticks and `**`, then as every claim statement is (NFKC, lower case,
whitespace collapsed, punctuation at both ends dropped). It is stored as the claim's `statement_hash`, so it never
changes between releases. It does not include the author: when a second report, from any subagent or token, names
the same finding, its report source is linked `supports` to the existing claim instead of creating another, and
`n_support` in `v_claim_status` counts the reports that named it. The same item twice in one report is one finding,
and a retried report finds its source, claims and links again. So the number of out-of-scope claims equals the
number of distinct items across the reports.

### Deciding

The ledger's existing statuses carry the decision; there is no status of its own for findings. Both decisions are
for an orchestrator or a human (`--as human` in local mode; under a token, the lead's own token and no `--as`), need
an oracle and a rationale of at most 1000 characters, and no evidence (only `verified` requires that).

```bash
# accepted: supported, with the follow-up issue linked as a source
benchbook finding accept c:1a2b3c4d --issue https://github.com/org/repo/issues/12 --oracle human --as human

# rejected: refuted (final in the claim state graph)
benchbook decide c:1a2b3c4d refuted --oracle human --rationale "intended: uploads retry forever by design" --as human
```

`benchbook finding add "..." [--file PATH] [--piece P]` records a finding the lead found itself (or one a worker
gave in a message) as a one-item "Out of scope" report, with the same key and dedup as a report's, and a
`finding noted by <agent>` source.

`benchbook finding accept` exists because no other command links a follow-up issue: it adds the issue URL as a source
(title "follow-up issue"), links it `cites` to the finding, and decides `supported` with that source as the
decision's evidence and the rationale `accepted, follow-up <url>` unless `--rationale` says otherwise. It refuses a
claim without the `out-of-scope` tag and a finding that is already decided, before anything is written. An agent
lead deciding on its own judgement passes `--oracle llm_judge`.

### Reading open findings

A finding is open while nobody has decided it: status `proposed` (or `inconclusive`, which decides nothing).
Accepted and rejected findings drop out. The open findings of one module, by the module's tag or by its path
prefix, in one `benchbook sql` statement:

```sql
SELECT id, statement, tags, n_support AS reports, created_at
FROM v_claim_status
WHERE kind = 'observation' AND 'out-of-scope' = ANY(tags)
  AND status IN ('proposed', 'inconclusive')
  AND ('mod:media' = ANY(tags)
       OR EXISTS (SELECT 1 FROM unnest(tags) AS t WHERE t LIKE 'file:src/app/modules/media/%'))
ORDER BY created_at
```

And the counts a summary needs:

```sql
SELECT count(*) FILTER (WHERE status IN ('proposed', 'inconclusive')) AS open,
       count(*) FILTER (WHERE status = 'supported') AS accepted,
       count(*) FILTER (WHERE status = 'refuted') AS rejected
FROM v_claim_status
WHERE kind = 'observation' AND 'out-of-scope' = ANY(tags)
```
