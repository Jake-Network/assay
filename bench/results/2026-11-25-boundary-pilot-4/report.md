# Boundary benchmark

Comparative totals include only task repetitions completed by every selected arm.

| Arm | Matched trials | H1 Compaction answers | H1 Delayed compaction answers | H1 Session answers | H2 False completions | H2 Weakened merges | H3 Repeated failures | H4 Cost USD | H4 Session tokens | H4 Wall seconds | H4 Peak lead tokens | H4 Probe cost USD | H6 Trusted stale | H6 Misflagged fresh |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| A Single session | 1 | 0/0 (n/a) | 0/0 (n/a) | 8/10 (80.0%) | 0/4 (0.0%) | 0/4 | 0 | 0.3011 | 348598 | 99.82 | 22732 | 0.1250 | 0/2 (0.0%) | 0/2 (0.0%) |
| C Progress files | 1 | 0/0 (n/a) | 0/0 (n/a) | 7/10 (70.0%) | 0/4 (0.0%) | 0/4 | 0 | 0.5233 | 551833 | 148.52 | 23401 | 0.0734 | 0/2 (0.0%) | 0/2 (0.0%) |
| F benchbook boundary mode | 1 | 0/0 (n/a) | 0/0 (n/a) | 6/10 (60.0%) | 0/4 (0.0%) | 0/4 | 0 | 0.5054 | 662777 | 259.65 | 25951 | 0.0642 | 0/2 (0.0%) | 0/2 (0.0%) |

H1 scores the five fixed questions against the task oracle. Arm A resumes the original session; the other arms start a new one at each session boundary.
Compaction samples require a Claude Code `compact_boundary` event. The first invocation after the work call is the resume probe. A lead response between those events puts the sample in the delayed column.
H4 totals include probe calls. Wall seconds cover completed trials end to end. Claude-mem worker spending requires a separate receipt from its provider before comparing total cost.
H6 counts subsequent actions after changing one previously verified file, with an unchanged piece as a control. Per-probe scores also show each piece's applicability answer.

## H5 fan-in

| Circle-packing configuration | Requested workers | Delivered reports | Lead report tokens (estimate) | Peak lead context (tokens) |
|---|---|---|---|---|
| Run independently with `python -m bench.run --workers 8,16,30` | n/a | n/a | n/a | n/a |
