---
name: benchbook-lead
description: Run an benchbook research swarm - split a goal into tasks, launch benchbook-worker subagents in parallel, read the ledger (not their messages), decide claims, and plan the next round. Use when the user asks to run a swarm, autoresearch loop or parallel experiments with benchbook.
argument-hint: <goal> [rounds=N] [workers=K]
---

# benchbook lead loop

You are the lead (orchestrator). Use `agent="lead"` on every benchbook MCP call. Goal and budget: $ARGUMENTS

1. **Project.** If `.benchbook/config.toml` is missing, confirm the goal, primary metric and direction (max/min)
   with the user, then run `benchbook init --goal "..." --metric NAME --direction max|min` (add
   `--config-schema @schema.json` if configs have a known shape). `init` registers `lead` as orchestrator and
   `human` as human.
2. **Tasks.** Split the search space into 4-8 independent tasks with `benchbook_create_tasks`. Give every task
   `tags` and `spec.config_space` (the keys and values that task should explore).
3. **Workers.** Launch K `benchbook-worker` subagents in parallel (default K=4), in one message. Each prompt is
   exactly two sentences: "Your benchbook agent name is `worker-<n>`. Take a task with benchbook_next_task and carry
   it out." Number n uniquely across rounds. Do not pass results or other workers' findings in the prompt;
   the brief does that.
4. **Read the ledger.** When the round ends, do not summarise subagent messages. Read `benchbook_status` and
   `benchbook_brief`; use `benchbook_sql` (read `benchbook://schema` first) for anything else, for example the configs
   with the most failures: `SELECT config, n_failed FROM v_tried ORDER BY n_failed DESC LIMIT 5`.
5. **Decide.** Move claims with `benchbook_decide` when the evidence is there. Cite the original experiment in the
   `supported` decision or link it to the claim with `supports`. Use `verified` with oracle `rerun` once valid
   runs of that config and code cover at least three seeds and another agent has reproduced the result.
   Otherwise create a replication task with the same config and code, new seeds, and tag `replicate`.
   If an evaluator bug or exploit shows up, invalidate the affected experiments (`new_status="invalidated"`).
6. **Next round.** Create fewer tasks where failures cluster (`v_negative`) and more around improvements
   (`v_best`, recent `improved` experiments).
7. **Stop** at the user's budget (rounds, time or cost). Final report: best value and its id, verified claims,
   number of experiments, duplicates blocked and repeat-check matches (`benchbook_status`), and the viewer
   address (`benchbook serve`, then http://127.0.0.1:7466).
