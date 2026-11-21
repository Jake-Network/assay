"""Dry mode: scripted (LLM-free) agents emulate the three configurations end to end.

This validates the harness pipeline (evaluation log, ledger, metrics, report). Its numbers come from a
simulation with stated assumptions (token costs per action, message drop rate, compaction threshold); they are
NOT evidence for or against benchbook. The real comparison runs Claude Code headless (bench/real.py).

Shared by all three configurations: same workers, rounds, evaluations per task, token budget, evaluator,
seeded RNG for config choices, and a fixed token cost per evaluation step.

A baseline  workers return results as text; the lead's context accumulates them; a message is lost with
            probability --drop-rate (subagent output never delivered); above --compact-at tokens the lead
            compacts and keeps only the best result. The lead passes the last 10 tried configs to new workers.
B files     workers append each result to a JSON file; the lead reads the whole directory every round
            (nothing is lost, the lead context grows). Same 10-config hint as A.
C benchbook     workers use the real benchbook SDK: brief, check_tried before every evaluation, record after it;
            the lead reads status + brief. Results that are never reported are still in the ledger.
"""
from __future__ import annotations

import json
import random
import uuid
from pathlib import Path
from typing import Any

from benchbook.dedup import canonical_json
from benchbook.tokens import estimate_tokens

from .common import CODE_REF, SPACE, EvalLog, RunMetrics, best_curve, duplicate_ratio, load_evals

STEP_TOKENS = 400  # fixed cost of one worker evaluation turn (identical in A, B, C)
PROMPT_BASE = 250


def sample(rng: random.Random, focus: str) -> dict[str, Any]:
    cfg = {k: rng.choice(v) for k, v in SPACE.items()}
    cfg["init"] = focus
    return cfg


def line(e: dict[str, Any]) -> str:
    c = e["config"]
    v = "n/a" if e["value"] is None else f"{e['value']:.4f}"
    return f"{c['init']}/{c['optimizer']}/iters={c['iters']}/perturb={c['perturb']} seed={e['seed']} -> {v}" + (
        f" ({e['notes']})" if e.get("notes") else "")


class Sim:
    def __init__(self, mode: str, out: Path, run: int, args: Any) -> None:
        self.mode, self.out, self.run, self.a = mode, out, run, args
        self.rng = random.Random(args.seed * 100 + run)
        self.tokens = 0
        self.lead_ctx = 0
        self.lead_ctx_max = 0
        self.known: dict[str, dict[str, Any]] = {}  # results the lead can still report
        self.hint: list[str] = []
        self.log = EvalLog(out / "evals.jsonl")
        self.worker_n = 0
        self.lg = None
        if mode == "C":
            from benchbook import Ledger

            pid = f"bench-{uuid.uuid4().hex[:8]}"
            Ledger.init_project(pid, goal="Maximize the sum of radii of 26 circles in the unit square.",
                                metric="sum_radii", direction="max")
            self.pid = pid
            self.lg = Ledger.open(project=pid, agent="lead")
        if mode == "B":
            (out / "results").mkdir(parents=True, exist_ok=True)

    def spend(self, n: int) -> None:
        self.tokens += n

    def lead_reads(self, n: int) -> None:
        self.spend(n)
        self.lead_ctx += n
        self.lead_ctx_max = max(self.lead_ctx_max, self.lead_ctx)

    def over(self) -> bool:
        return self.tokens >= self.a.budget

    # ------------------------------------------------------------------ one round
    def round(self) -> None:
        focuses = [self.rng.choice(SPACE["init"]) for _ in range(self.a.workers)]
        if self.mode == "C":
            assert self.lg is not None
            self.lg.create_tasks([{"title": f"explore {f}", "tags": [f],
                                              "spec": {"config_space": dict(SPACE, init=[f])}} for f in focuses])
            self.lead_reads(60 * len(focuses))
        for f in focuses:
            if self.over():
                return
            self.worker_n += 1
            self.worker(f"worker-{self.worker_n}", f)
        self.lead_review()

    def worker(self, name: str, focus: str) -> None:
        mine: list[dict[str, Any]] = []
        if self.mode == "C":
            from benchbook import Ledger

            w = Ledger.open(project=self.pid, agent=name)
            task = w.next_task()
            self.spend(PROMPT_BASE + estimate_tokens(w.brief(task.id if task else None, 1500)))
        else:
            self.spend(PROMPT_BASE + estimate_tokens("\n".join(self.hint)))
            w, task = None, None
        avoid = set(self.hint)
        for _ in range(self.a.evals):
            if self.over():
                break
            cfg, seed = sample(self.rng, focus), 0
            for _try in range(8):
                if self.mode == "C":
                    t = w.check_tried(cfg, CODE_REF)  # type: ignore[union-attr]
                    self.spend(60)
                    if not any(e.seed == seed for e in t.exact):
                        break
                elif canonical_json(cfg) not in avoid:
                    break
                cfg = sample(self.rng, focus)
            e = self.log.evaluate(name, cfg, seed, self.tokens)
            self.spend(STEP_TOKENS)
            mine.append(e)
            if self.mode == "B":
                (self.out / "results" / f"{name}-{e['i']}.json").write_text(json.dumps(e))
            if self.mode == "C":
                w.record_experiment(cfg, {"sum_radii": e["value"]} if e["value"] is not None else {},  # type: ignore[union-attr]
                                    run_status=e["run_status"], seed=seed, code_ref=CODE_REF, notes=e["notes"],
                                    task_id=task.id if task else None)
                self.spend(40)
        # final message to the lead
        if self.mode == "A":
            msg = "Results:\n" + "\n".join(line(e) for e in mine)
            self.spend(estimate_tokens(msg))
            if self.rng.random() >= self.a.drop_rate:
                self.lead_reads(estimate_tokens(msg))
                for e in mine:
                    self.known[str(e["i"])] = e
        elif self.mode == "B":
            self.lead_reads(estimate_tokens(f"{name}: wrote {len(mine)} result files"))
        else:
            if task is not None:
                w.complete_task(task.id, "done", f"{len(mine)} evaluations")  # type: ignore[union-attr]
            self.lead_reads(estimate_tokens(f"{name}: done, {len(mine)} experiments recorded; ids e:xxxxxxxx ..."))

    def lead_review(self) -> None:
        if self.mode == "A":
            if self.lead_ctx > self.a.compact_at:
                best = max((e for e in self.known.values() if e["value"] is not None), key=lambda e: e["value"],
                           default=None)
                self.known = {str(best["i"]): best} if best else {}
                self.lead_ctx = 800
            self.hint = [canonical_json(e["config"]) for e in list(self.known.values())[-10:]]
        elif self.mode == "B":
            files = sorted((self.out / "results").glob("*.json"))
            text = "\n".join(f.read_text() for f in files)
            self.lead_reads(estimate_tokens(text))
            rows = [json.loads(f.read_text()) for f in files]
            self.known = {str(e["i"]): e for e in rows}
            self.hint = [canonical_json(e["config"]) for e in rows[-10:]]
        else:
            assert self.lg is not None
            self.lead_reads(estimate_tokens(json.dumps(self.lg.status(), default=str)))
            self.lead_reads(estimate_tokens(self.lg.brief(None, 2000)))
            self.lead_ctx = min(self.lead_ctx, 6000)  # the lead re-reads the bounded brief instead of history

    def finish(self) -> RunMetrics:
        self.log.close()
        evals = load_evals(self.out / "evals.jsonl")
        if self.mode == "C":
            assert self.lg is not None
            n = self.lg.sql_internal("SELECT count(*) AS n FROM benchbook.experiments WHERE project_id=%s",
                                     (self.pid,))[0]["n"]
            lost = len(evals) - n
        else:
            lost = sum(1 for e in evals if str(e["i"]) not in self.known)
        curve = best_curve(evals)
        return RunMetrics(self.mode, self.run, len(evals), duplicate_ratio(evals), max(lost, 0), self.lead_ctx_max,
                          self.tokens, curve[-1][1] if curve else None, curve)


def run_dry(mode: str, out: Path, run: int, args: Any) -> RunMetrics:
    sim = Sim(mode, out, run, args)
    for _ in range(args.rounds):
        if sim.over():
            break
        sim.round()
    return sim.finish()
