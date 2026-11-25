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

Every run also leaves what a real run leaves (bench/dry_trace.py writes a stream and session transcripts as the
simulation goes) and is put through the same instrumentation as a real run: transcript collection, the usage
reconciliation in metrics.json and, for C, the hook replay and the ledger export. `python -m bench.analyze_cost`
therefore runs on a dry run directory.
"""
from __future__ import annotations

import json
import os
import random
import time
import uuid
from pathlib import Path
from typing import Any

from benchbook.core.dedup import canonical_json
from benchbook.core.tokens import estimate_tokens

from . import real
from .common import CODE_REF, SPACE, EvalLog, RunMetrics, best_curve, duplicate_ratio, load_evals
from .dry_trace import MCP, Agent, Trace

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
        self.trace = Trace(out, mode)
        self.pid: str | None = None
        self.log = EvalLog(out / "evals.jsonl")
        self.worker_n = 0
        self.stopped: list[tuple[Agent, str]] = []  # C: the workers whose SubagentStop the plugin hook would see
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
            tasks = [{"title": f"explore {f}", "tags": [f], "spec": {"config_space": dict(SPACE, init=[f])}}
                     for f in focuses]
            created = self.lg.create_tasks(tasks)
            self.trace.tool(self.trace.lead, MCP + "create_tasks", {"agent": "lead", "tasks": tasks}, created)
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

            me = self.trace.spawn(name, f"Your benchbook agent name is {name}. Take a task and carry it out.")
            w = Ledger.open(project=self.pid, agent=name)
            task = w.next_task()
            self.trace.tool(me, MCP + "next_task", {"agent": name}, {"task": task.id if task else None})
            brief = w.brief(task.id if task else None, 1500)
            self.trace.tool(me, MCP + "brief", {"agent": name, "task_id": task.id if task else None}, brief)
            self.spend(PROMPT_BASE + estimate_tokens(brief))
        else:
            me = self.trace.spawn(name, f"Explore init={focus} with {self.a.evals} evaluations. Already tried:\n"
                                  + "\n".join(self.hint))
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
                    self.trace.tool(me, MCP + "check_tried", {"agent": name, "config": cfg, "code_ref": CODE_REF},
                                    {"tried": bool(t.exact), "replicates": len(t.exact)})
                    self.spend(60)
                    if not any(e.seed == seed for e in t.exact):
                        break
                elif canonical_json(cfg) not in avoid:
                    break
                cfg = sample(self.rng, focus)
            e = self.log.evaluate(name, cfg, seed, self.tokens)
            receipt = {k: e[k] for k in ("eval_id", "config", "code_ref", "seed", "run_status", "value", "notes")}
            self.trace.tool(me, "Bash", {"command": f"python tools/evaluate.py --config '{json.dumps(cfg)}' "
                                                    f"--seed {seed} --agent {name}"}, receipt)
            self.spend(STEP_TOKENS)
            mine.append(e)
            if self.mode == "B":
                (self.out / "results" / f"{name}-{e['i']}.json").write_text(json.dumps(e))
                self.trace.tool(me, "Write", {"file_path": f"results/{name}-{e['i']}.json",
                                              "content": json.dumps(receipt)}, "File created successfully")
            if self.mode == "C":
                metrics = {"sum_radii": e["value"]} if e["value"] is not None else {}
                recorded = w.record_experiment(cfg, metrics, run_status=e["run_status"],  # type: ignore[union-attr]
                                               seed=seed, code_ref=CODE_REF, notes=e["notes"],
                                               task_id=task.id if task else None)
                self.trace.tool(me, MCP + "record_experiment",
                                {"agent": name, "config": cfg, "metrics": metrics, "seed": seed, "code_ref": CODE_REF,
                                 "run_status": e["run_status"]}, {"id": recorded.id})
                self.spend(40)
        # final message to the lead
        if self.mode == "A":
            msg = "Results:\n" + "\n".join(line(e) for e in mine)
            self.spend(estimate_tokens(msg))
            delivered = self.rng.random() >= self.a.drop_rate
            self.trace.report(me, msg, delivered)
            if delivered:
                self.lead_reads(estimate_tokens(msg))
                for e in mine:
                    self.known[str(e["i"])] = e
        elif self.mode == "B":
            msg = f"{name}: wrote {len(mine)} result files"
            self.trace.report(me, msg)
            self.lead_reads(estimate_tokens(msg))
        else:
            if task is not None:
                w.complete_task(task.id, "done", f"{len(mine)} evaluations")  # type: ignore[union-attr]
                self.trace.tool(me, MCP + "complete_task", {"agent": name, "task_id": task.id, "outcome": "done"},
                                {"ok": True})
            msg = f"{name}: done, {len(mine)} experiments recorded; ids e:xxxxxxxx ..."
            self.trace.report(me, msg)
            self.stopped.append((me, msg))
            self.lead_reads(estimate_tokens(msg))

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
            self.trace.tool(self.trace.lead, "Glob", {"pattern": "results/*.json"}, "\n".join(f.name for f in files))
            for f in files:
                self.trace.tool(self.trace.lead, "Read", {"file_path": f"results/{f.name}"}, f.read_text())
            self.lead_reads(estimate_tokens(text))
            rows = [json.loads(f.read_text()) for f in files]
            self.known = {str(e["i"]): e for e in rows}
            self.hint = [canonical_json(e["config"]) for e in rows[-10:]]
        else:
            assert self.lg is not None
            status, brief = json.dumps(self.lg.status(), default=str), self.lg.brief(None, 2000)
            self.trace.tool(self.trace.lead, MCP + "status", {"agent": "lead"}, status)
            self.trace.tool(self.trace.lead, MCP + "brief", {"agent": "lead", "budget_tokens": 2000}, brief)
            self.lead_reads(estimate_tokens(status))
            self.lead_reads(estimate_tokens(brief))
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
        metrics = RunMetrics(self.mode, self.run, len(evals), duplicate_ratio(evals), max(lost, 0), self.lead_ctx_max,
                             self.tokens, curve[-1][1] if curve else None, curve)
        metrics.usage_check = self.instrument()
        metrics.lead_report_tokens, metrics.delivered_worker_reports = real.worker_reports(real._read_stream(self.out))
        return metrics

    def instrument(self) -> dict[str, Any]:
        """The post-run steps of bench.real.run_real, on what the simulation left behind."""
        self.trace.close(f"dry run: {self.log.n} evaluations")
        meta: dict[str, Any] = {"dry": True, "cwd": str(self.out / "work"), "project_id": self.pid,
                                "session_id": self.trace.session_id}
        meta["transcripts"] = real.collect_transcripts(self.trace.config_dir, self.trace.session_id, self.out)
        if self.pid:
            env = {**os.environ, "BENCHBOOK_PROJECT": self.pid, "XDG_STATE_HOME": str(self.out / "state")}
            prepared = real.PreparedRun(self.out, [], env, None, self.pid, self.trace.session_id)
            self.queue_hook_entries()
            meta["ledger"] = {}
            real.keep_ledger_evidence(meta["ledger"], self.out, env, self.out, lambda: real._flush_hooks(prepared))
        (self.out / "command.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
        return real.usage_check(self.out)

    def queue_hook_entries(self) -> None:
        """Leave each worker's SubagentStop in the hook spool, in the form `benchbook hook subagent-stop` writes
        it, without starting the hook's background processor: the harness's own replay then has entries to
        snapshot, record and export, which is the path a real C run takes when a processor was still behind."""
        from benchbook.hooks import subagent as hooks

        # The spool is keyed by the database target, which this process shares with the replay; only the state
        # directory is the run's own.
        spool = self.out / "state" / "benchbook" / "spool" / hooks._spool_dir().name
        spool.mkdir(parents=True, exist_ok=True)
        for worker, text in self.stopped:
            data = {"hook_event_name": "SubagentStop", "session_id": self.trace.session_id,
                    "agent_id": worker.agent_id, "agent_type": "benchbook:benchbook-worker",
                    "last_assistant_message": text}
            hooks._write_spool(spool, {"event": "subagent-stop", "project": self.pid, "data": data,
                                       "spooled_at": time.time(), "accepted": True})


def run_dry(mode: str, out: Path, run: int, args: Any) -> RunMetrics:
    sim = Sim(mode, out, run, args)
    for _ in range(args.rounds):
        if sim.over():
            break
        sim.round()
    return sim.finish()
