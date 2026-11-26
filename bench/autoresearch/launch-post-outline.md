# Launch post outline: autoresearch on many GPUs

> Draft outline. Numbers come only from the pre-registered launch experiment
> ([PREREGISTRATION.md](PREREGISTRATION.md) and its report.md). Post nothing about upstream until results exist,
> and then only where a human decides (Discussions or X; no PRs or issues on karpathy/autoresearch).

1. **The pain** (2 paragraphs)
   - autoresearch is one agent, one GPU, one branch. With 8 GPUs you get 8 branches that share nothing:
     the same idea runs on several GPUs, and every `results.tsv` lives on one machine.
   - The keep/discard rule trusts a single 5-minute run. A lucky run becomes the branch's new base and every
     later experiment builds on it. Show the measured share of arm A keeps that did not reproduce (metric 5).
2. **The design** (3 short sections, one diagram)
   - One ledger, self-hosted: embedded Postgres on one machine, a Postgres URL for several; no API key.
   - Refuse before running: the normalised code hash per hardware class and harness; intents for ideas in flight.
   - Build only on replicated results: candidate → two re-runs by other agents → verified; a deterministic
     referee, no LLM judge. Show the T1-T7 table in one image.
3. **The experiment** (pre-registered; link the commit of PREREGISTRATION.md)
   - Conditions: GPUs, hours, model, budgets.
   - Table: metrics 1, 3, 5, 6 for A and B, including the ones B loses. The best-over-time plot.
   - What it cost: the replication share of GPU time; tokens and dollars.
4. **Try it** (3 lines from the README) and the template repository.
5. **What is not done**: public hub, accounts, LLM-judged verification (deliberately absent).

## Positioning notes

How benchbook differs from autoresearch-at-home (as of its last commit, 2026-03-12): autoresearch-at-home
coordinates through a hosted key-value service (Ensue, `ENSUE_API_KEY`), blocks claims whose description embedding
is ≥ 0.92 similar, keeps a best per VRAM tier and accepts a new global best after sanity checks (val_bpb ≥ 0.5, at
most 0.1 better at once, re-read before write). benchbook is self-hosted, compares exact normalised code and declared
knob changes instead of embeddings, groups by GPU model and evaluation harness, and only promotes a result after other
agents replicate it.
