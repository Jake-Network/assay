---
name: assay-lead
description: Run an assay research swarm - split a goal into tasks, launch assay-worker subagents in parallel, read the ledger (not their messages), decide claims, and plan the next round. Use when the user asks to run a swarm, autoresearch loop or parallel experiments with assay.
argument-hint: <goal> [rounds=N] [workers=K]
---

# assay lead loop

You are the lead (orchestrator). Use `agent="lead"` on every assay MCP call. Goal and budget: $ARGUMENTS

1. **Project.** If `.assay/config.toml` is missing, confirm the goal, primary metric and direction (max/min)
   with the user, then run `assay init --goal "..." --metric NAME --direction max|min` (add
   `--config-schema @schema.json` if configs have a known shape). `init` registers `lead` as orchestrator and
   `human` as human.
2. **Tasks.** Split the search space into 4-8 independent tasks with `assay_create_tasks`. Give every task
   `tags` and `spec.config_space` (the keys and values that task should explore).
3. **Workers.** Launch K `assay-worker` subagents in parallel (default K=4), in one message. Each prompt is
   exactly two sentences: "Your assay agent name is `worker-<n>`. Take a task with assay_next_task and carry
   it out." Number n uniquely across rounds. Do not pass results or other workers' findings in the prompt;
   the brief does that.
4. **Read the ledger.** When the round ends, do not summarise subagent messages. Read `assay_status` and
   `assay_brief`; use `assay_sql` (read `assay://schema` first) for anything else, for example the configs
   with the most failures: `SELECT config, n_failed FROM v_tried ORDER BY n_failed DESC LIMIT 5`.
5. **Decide.** Move claims with `assay_decide` when the evidence is there. Use `verified` with oracle
   `rerun` only when the evidence includes >= 3 seeds and >= 2 different agents; otherwise create a replication
   task (same config, new seeds, tag `replicate`). If an evaluator bug or exploit shows up, invalidate the
   affected experiments (`new_status="invalidated"`).
6. **Next round.** Create fewer tasks where failures cluster (`v_negative`) and more around improvements
   (`v_best`, recent `improved` experiments).
7. **Stop** at the user's budget (rounds, time or cost). Final report: best value and its id, verified claims,
   number of experiments, duplicates blocked and repeat-check matches (`assay_status`), and the viewer
   address (`assay serve`, then http://127.0.0.1:7466).
