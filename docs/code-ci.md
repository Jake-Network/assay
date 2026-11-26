# Coding work: CI evidence and the PR summary

Part of [the code pack](code.md).

## CI evidence and the PR summary

A re-run by CI is a re-run on a machine the implementer does not control. The GitHub Action in
[integrations/github-action](../integrations/github-action/README.md) runs `check verify` for each piece a pull
request completes and keeps one comment on the pull request that says which completions an independent re-run
confirmed. The logic is in three commands, which run anywhere:

```bash
benchbook pr pieces --base BASE [--head HEAD] [--claims] [--whole-as NAME]   # piece<TAB>commit[<TAB>reporter<TAB>claim]
benchbook pr summary --base BASE [--head HEAD] [--run-url URL] [--whole-as NAME]   # the markdown, starting with the marker
benchbook pr comment --repo OWNER/NAME --pr N --body-file F [--author LOGIN] [--update-only]
```

**Pieces.** A commit names the piece it completes with the trailer `Benchbook-Piece: <piece>` (the trailer block at the
end of the message; the key is case-insensitive, and one commit may carry several). For each piece named in `BASE..HEAD`
the piece's commit is the last commit carrying its trailer. A value that is not a piece name (1-100 characters, no
spaces) is skipped with a warning. `--claims` adds the reporter and the id of the completion claim for that piece at
that commit (`-` for none). The reporter is `payload.reported_by` when a lead, a person or a harness agent recorded the
claim, otherwise its author ([Who the implementer is](code.md#who-the-implementer-is)). A claim counts when it carries both
tags and its payload names that piece and commit; with several, a verified one, else the oldest. Passing the id to
`check verify --claim` links the re-run to that very claim, whatever its statement.

**The action's run**, per piece: `check verify <piece> <commit> --base <base sha> --trace-ref '{"ci":
"github-actions", "repository", "run_id", "run_attempt", "run_url", "pull_request"}' --no-decide`, with
`--claim` when a claim exists. Without a claim it records only the attempt, and the piece shows as *not
reported*: CI never reports a completion on someone's behalf. Then `pr summary` and `pr comment`. The job fails when
a re-run failed a check. A pull request rebased after the lead's `check verify` has new commit ids, so its pieces
have no claim at the new commits until someone reports them there.

**Without a ledger** (no `database-url`) the action re-runs the pull request in a ledger it creates for the
workflow run only, and passes `--whole-as pr-<number>` to `pr pieces` and `pr summary`, so the head is re-run as
that piece, reported by the pull request's author, when no trailer names the head commit. The checks still come
only from the config committed at the merge-base ([Set up](code.md#set-up)). What it shows and cannot show: the action's
README, "Without a ledger".

**The summary**, per piece: the state, the runs of its commit (passed, failed, and the independent authors), the
number of discarded approaches (`observation` claims tagged `discarded` and `piece:<piece>`, any status), the open
out-of-scope findings (tagged `out-of-scope` and `piece:<piece>`, status `proposed` or `inconclusive`; a finding
recorded without a piece tag is not counted), and whether tests changed (any attempt of the commit with
`guard_touched` or `checks_changed` true; *unknown* when no attempt says). The states, checked in this order:

| state | when |
| --- | --- |
| **verified** | the claim is `verified`; or no run of the commit failed, none touched a guarded path or changed `[checks]`, and at least one passing run by an agent other than the implementer (not `via` check run or the hook) satisfies the pack's rule (`Ledger.check_rerun`, the same check `decide ... verified --oracle rerun` makes). The second case is shown with the claim's status: CI records, a lead decides. |
| **re-run disagrees** | the claim is `refuted` or `inconclusive`, or a valid finished run of the commit failed a check (anyone's: the implementer's own failing run counts too). |
| **claimed only** | a claim exists and none of the above: no independent re-run yet, or the commit changed tests, which a person decides. |
| **not reported** | the ledger has no completion claim for the piece at that commit. |

Piece names, agent names and paths go into the comment as inline code with backticks and `|` neutralised, and the
reasons are escaped so nothing in them renders as HTML, a link or a mention.

**The comment.** `pr comment` lists the pull request's comments with `gh api --paginate` (GH_TOKEN), takes the
first one whose body starts with `<!-- benchbook-summary -->` and, with `--author`, whose author is that login,
updates it, and deletes further marked comments by the same author. With none it creates one, unless
`--update-only` (the action passes it when the pull request names no piece, so pull requests without pieces get
no comment). A marked comment by someone else is never taken for the summary.

**Identity.** Who the action's records are by (`ci:<run_id>` with the owner's URL, the token's agent with a
token), when it may decide, and why the guarantee is against misreporting rather than a pull request written to take
the credential: the action's README, "Who the re-runs are by".
