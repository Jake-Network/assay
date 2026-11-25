"""Run the coding boundary benchmark. Preview by default; --dry runs scripted agents locally."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import socket
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

from benchbook.db.conn import stop_embedded
from benchbook.errors import BenchbookError

from .. import real
from ..code.run import git, ledger_env, write_json
from . import arms, probes, report
from .tasks import Phase, Task, input_tasks, inspect

REGISTRATION = Path(__file__).with_name("PREREGISTRATION.md")
MIN_CLAUDE_VERSION = (2, 1, 277)
USD_TOLERANCE = 1e-9
# A call's own usage in a result event (Claude Code 2.1.332), whether or not a resume restored the session's totals.
OWN_USAGE = "model_usage_since_last_result"
# Summed costUSD rows against total_cost_usd: equal in every receipt of the pilots, up to float addition.
OWN_USAGE_TOLERANCE = 1e-6
WORK_SCOPE = ("Work only in the task checkout and use the arm's provided mechanism for prior context. "
              "Leave the benchmark's evaluator, run logs, and oracle receipts to the harness.")


@dataclass
class Batch:
    args: argparse.Namespace
    out: Path
    execute_call: Callable[[real.PreparedRun, Path, int], None]
    spent: float = 0.0
    # Each session's totals after its last call: (cost, tokens) as counted here, then (total_cost_usd, tokens) as that
    # call's receipt reported them. The two differ after a resume that did not restore the session's earlier totals;
    # own_spend says which receipts it accepts.
    sessions: dict[str, tuple[float, int, float, int]] = field(default_factory=dict)

    def capture(
        self, arm: str, work: Path, env: dict[str, str], target: Path, prompt: str, session: str, *,
        resume: bool, probe: bool,
    ) -> list[tuple[int, dict[str, Any]]]:
        cap = self.args.probe_budget_usd if probe else self.args.max_budget_usd
        if self.spent + cap > self.args.total_budget_usd + USD_TOLERANCE:
            raise real.BenchRunError(f"total budget reached before {target.name}")
        cmd = arms.command(arm, prompt, session, resume=resume, model=self.args.model, budget=cap,
                           max_turns=self.args.probe_turns if probe else self.args.max_turns,
                           claude_mem=self.args.claude_mem_plugin, schema=probes.SCHEMA if probe else None)
        cmd[0] = real.claude_bin(self.args)
        target.mkdir()
        write_json(target / "command.json", {"cmd": cmd, "cwd": str(work), "session": session, "resume": resume})
        self.spent += cap  # reserve the full cap until a valid cost receipt is available
        self.record_spend()
        started = time.monotonic()
        self.execute_call(real.PreparedRun(work, cmd, env, None, None, session), target, self.args.wall_clock)
        events = real._read_stream(target)
        metrics = own_spend(read_call(events, session), self.sessions.get(session) if resume else None)
        self.sessions[session] = (metrics["session_cost_usd"], metrics["session_tokens"],
                                  metrics["reported_session_cost_usd"], metrics["reported_session_tokens"])
        self.spent += metrics["cost_usd"] - cap
        self.record_spend()
        metrics["seconds"] = time.monotonic() - started
        write_json(target / "metrics.json", metrics)
        if metrics["cost_usd"] > cap + USD_TOLERANCE:
            raise real.BenchRunError(f"{target.name} exceeded its cap")
        return events

    def record_spend(self) -> None:
        write_json(self.out / "spend.json", {"counted_usd": round(self.spent, 8)})


def read_call(events: list[tuple[int, dict[str, Any]]], session: str) -> dict[str, Any]:
    results = [e for _, e in events if e.get("type") == "result"]
    if len(results) != 1 or results[0].get("session_id") != session:
        raise real.BenchRunError("missing, duplicate, or mismatched session receipt")
    result = results[0]
    if result.get("subtype") != "success" or result.get("is_error") is not False:
        raise real.BenchRunError(f"Claude invocation ended with {result.get('subtype')}")
    model_usage = result.get("modelUsage")
    costs = result.get("total_cost_usd")
    counts = [real._class_counts(usage, real.MODEL_USAGE_KEYS)
              for usage in model_usage.values()] if isinstance(model_usage, dict) else []
    contexts = [usage[0] for lead, msg in real.stream_messages(events)
                if lead and (usage := real._usage(msg.get("usage"))) is not None]
    if not real._number(costs) or costs < 0 or not counts or None in counts or not contexts:
        raise real.BenchRunError("Claude invocation lacks full cost, session usage, or lead context evidence")
    receipt = {"cost_usd": costs, "tokens": sum(sum(n) for n in counts if n is not None),
               "lead_context_max": max(contexts)}
    if OWN_USAGE in result:
        own = usage_totals(result[OWN_USAGE])
        if own is None:
            raise real.BenchRunError(f"Claude invocation has an unreadable {OWN_USAGE}")
        receipt["own"] = own
    return receipt


def usage_totals(rows: Any) -> tuple[float, int] | None:
    """Cost and tokens summed over a per-model usage map, or None when any row lacks them. An empty map is None too:
    read_call also requires the lead's context, so the call made at least one API request."""
    if not isinstance(rows, dict) or not rows:
        return None
    costs = [row.get("costUSD") if isinstance(row, dict) else None for row in rows.values()]
    counts = [real._class_counts(row, real.MODEL_USAGE_KEYS) for row in rows.values()]
    if not all(real._number(c) and c >= 0 for c in costs) or None in counts:
        return None
    return sum(costs), sum(sum(n) for n in counts if n is not None)


def own_spend(receipt: dict[str, Any], before: tuple[float, int, float, int] | None) -> dict[str, Any]:
    """A call's own cost and tokens, with the session totals beside them (before: Batch.sessions' entry).

    A receipt that reports the call's own usage (OWN_USAGE) is read from that, since a resumed session's totals cover
    the whole session only when Claude Code restored the earlier ones (on 2026-11-25 one resume did not). The own
    usage must fit the receipt one of two ways: the receipt's totals are the own usage (a first call, or a resume
    that restored nothing) or the previous receipt's totals plus it (a resume that restored them). When own usage
    is absent, the call's spend is the difference from the previous receipt. reported_session_* preserve the
    receipt's totals, and session_* accumulate the spend of each call across resets."""
    cost, tokens, said_cost, said_tokens = before or (0.0, 0, 0.0, 0)
    rest = {key: value for key, value in receipt.items() if key != "own"}
    reported = {"reported_session_cost_usd": receipt["cost_usd"], "reported_session_tokens": receipt["tokens"]}
    if (own := receipt.get("own")) is not None:
        own_cost, own_tokens = own
        bases = [(0.0, 0)] + ([(said_cost, said_tokens)] if before else [])
        if not any(abs(receipt["cost_usd"] - (c + own_cost)) <= OWN_USAGE_TOLERANCE
                   and receipt["tokens"] == t + own_tokens for c, t in bases):
            raise real.BenchRunError(f"a call's {OWN_USAGE} fits neither its receipt's totals nor the previous "
                                     "receipt's totals plus it")
    else:
        if receipt["cost_usd"] < said_cost - USD_TOLERANCE or receipt["tokens"] < said_tokens:
            raise real.BenchRunError("a resumed session reported less spend than its previous receipt")
        own_cost = max(receipt["cost_usd"] - said_cost, 0.0)
        own_tokens = receipt["tokens"] - said_tokens
    return {**rest, **reported, "cost_usd": own_cost, "tokens": own_tokens,
            "session_cost_usd": cost + own_cost, "session_tokens": tokens + own_tokens}


def compile_env(arm: str, target: Path, args: argparse.Namespace) -> dict[str, str]:
    env = ledger_env(target, None)
    for key in ("CLAUDE_CODE_AUTO_COMPACT_WINDOW", "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE", "DISABLE_AUTOUPDATER"):
        env.pop(key, None)
    env.update(
        CLAUDE_CODE_AUTO_COMPACT_WINDOW=str(args.compact_window),
        CLAUDE_CODE_SUBAGENT_MODEL=args.model,
        CLAUDE_CODE_DISABLE_BACKGROUND_TASKS="1",
        ENABLE_CLAUDEAI_MCP_SERVERS="false",
        DISABLE_AUTOUPDATER="1",
        PATH=os.pathsep.join((str(Path(sys.executable).parent), os.environ.get("PATH", ""))),
    )
    if arm == "D":
        env["CLAUDE_MEM_DATA_DIR"] = str(target / "claude-mem")
        env.pop("CLAUDE_MEM_WORKER_URL", None)
        env.update(CLAUDE_MEM_WORKER_HOST="127.0.0.1", CLAUDE_MEM_WORKER_AUTOSTART="true")
        if not args.dry:
            with socket.socket() as local:
                local.bind(("127.0.0.1", 0))
                env["CLAUDE_MEM_WORKER_PORT"] = str(local.getsockname()[1])
    if arm == "E":
        env["BENCH_BOUNDARY_PAST_EVENTS"] = str(target / "past-events")
    if arm == "F":
        env["BENCHBOOK_PROJECT"] = "boundary"
    return env


@dataclass
class Trial:
    batch: Batch
    task: Task
    arm: str
    target: Path
    work: Path
    env: dict[str, str]
    run_data: dict[str, Any] = field(default_factory=lambda: {"calls": [], "phases": [], "probes": []})

    def invoke(self, name: str, prompt: str, session: str, *, resume: bool,
               probe: bool = False) -> list[tuple[int, dict[str, Any]]]:
        path = self.target / name
        new_call = not path.exists()
        try:
            events = self.batch.capture(
                self.arm, self.work, self.env, path, prompt, session, resume=resume, probe=probe,
            )
        except BaseException:
            if new_call and (path / "command.json").is_file():
                try:
                    self.transcripts(session, path)
                except OSError as exc:
                    print(f"could not preserve transcripts for {name}: {exc}", file=sys.stderr)
            raise
        self.transcripts(session, path)
        measured = json.loads((path / "metrics.json").read_text(encoding="utf-8"))
        self.run_data["calls"].append({**measured, "kind": "probe" if probe else "work"})
        return events

    def probe(self, name: str, session: str, phase: Phase, expected: dict[str, str], *, resume: bool,
              kind: str) -> None:
        instructions = probes.prompt(phase.stale_piece, phase.fresh_piece)
        if self.arm == "C":
            instructions += "\nYou may read claude-progress.txt, feature-list.json, and the repository."
        if self.arm == "E":
            instructions += f"\nSearch prior raw events with `{arms.SEARCH} QUERY`."
        events = self.invoke(name, instructions, session, resume=resume, probe=True)
        answers = next(event for _, event in events if event.get("type") == "result").get("structured_output")
        if not isinstance(answers, dict) or set(answers) != set(probes.QUESTIONS):
            raise real.BenchRunError("state probe result is missing its five structured answers")
        scored = {"boundary": kind, "expected": expected, "answers": answers,
                  "correct": probes.score(expected, answers)}
        self.run_data["probes"].append(scored)
        write_json(self.target / name / "score.json", scored)

    def run(self) -> dict[str, Any]:
        arm = arms.ARMS[self.arm]
        session, resume = str(uuid4()), False
        for i, phase in enumerate(self.task.phases, 1):
            name = f"phase-{i:02d}"
            prompt = "\n\n".join(part for part in (WORK_SCOPE, arm.instructions, phase.prompt) if part)
            events = self.invoke(name, prompt, session, resume=resume)
            if self.arm == "F" and not self.batch.args.dry:
                real._flush_hooks(real.PreparedRun(self.work, [], self.env, None, "boundary", session))
            state = inspect(self.task, self.work, i, "checkpoint", self.target)
            compact_at = [index for index, (_, e) in enumerate(events)
                          if (e.get("type"), e.get("subtype")) == ("system", "compact_boundary")
                          and e.get("session_id") == session]
            state["compactions"] = len(compact_at)
            if compact_at:
                # Only the CLI emits the boundary event. The next invocation is a --resume probe.
                state["post_compaction_turns"] = sum(
                    lead for lead, _ in real.stream_messages(events[compact_at[-1] + 1:])
                )
                kind = "delayed compaction" if state["post_compaction_turns"] else "compaction"
                self.probe(f"{name}-compact", session, phase, state["expected"], resume=True, kind=kind)
            self.run_data["phases"].append(state)

            before, after = probes.inject(self.work, phase.stale_path, phase.injection)
            stale = inspect(self.task, self.work, i, "injected", self.target, before)
            state["injection"] = {"before": before, "head": after, "path": phase.stale_path}
            write_json(self.target / f"{name}-checkpoint.json", state)
            write_json(self.target / f"{name}-injected.json", stale)
            if arm.fresh_sessions:
                if self.arm == "E":
                    self.archive(session, self.target / name, i)
                session = str(uuid4())
            self.probe(f"{name}-restart", session, phase, stale["expected"], resume=not arm.fresh_sessions,
                       kind="fresh session" if arm.fresh_sessions else "resumed session")
            self.invoke(f"{name}-action", f"{WORK_SCOPE}\n\n{phase.action}", session, resume=True)
            state["action"] = inspect(self.task, self.work, i, "action", self.target, before)
            resume = True
        write_json(self.target / "trial.json", self.run_data)
        return self.run_data

    def transcripts(self, session: str, call: Path) -> None:
        """Copy the session's transcripts (lead and subagents) into call/transcripts after every call of every arm.
        The stream has the hooks' SessionStart output only, not what a PostToolUse hook added to the lead's context,
        which is where arm F's re-run verdicts arrive (the pilot's amendment asks when the lead saw them). A resumed
        session's copy holds the whole session so far. A dry run copies the scripted stream to the same place."""
        if self.batch.args.dry:
            (call / "transcripts" / "subagents").mkdir(parents=True)
            shutil.copyfile(call / "lead.stream.jsonl", call / "transcripts" / "lead.jsonl")
            found: dict[str, Any] = {"lead": "lead.stream.jsonl", "subagents": [], "copy": "transcripts"}
        else:
            found = real.collect_transcripts(real.claude_config_dir(self.env), session, call)
        write_json(call / "transcripts.json", found)  # a transcript not found is recorded, not raised

    def archive(self, session: str, work_call: Path, i: int) -> None:
        dest = Path(self.env["BENCH_BOUNDARY_PAST_EVENTS"]) / f"session-{i:02d}"
        if self.batch.args.dry:
            dest.mkdir(parents=True)
            shutil.copyfile(work_call / "lead.stream.jsonl", dest / "lead.jsonl")
        else:
            collected = real.collect_transcripts(real.claude_config_dir(self.env), session, dest)
            if collected.get("error"):
                raise real.BenchRunError(str(collected["error"]))


def trial(batch: Batch, task: Task, arm: str, run: int) -> dict[str, Any]:
    started = time.monotonic()
    target = batch.out / task.id / f"{arm}-{run}"
    target.mkdir(parents=True)
    work = target / "work"
    subprocess.run(["git", "clone", "--quiet", "--no-local", "--", str(task.source), str(work)], check=True,
                   timeout=600, capture_output=True)
    git(work, "checkout", "--quiet", "-B", "boundary-work", task.base)
    git(work, "remote", "remove", "origin")
    git(work, "branch", "boundary-base", task.base)
    env = compile_env(arm, target, batch.args)
    try:
        if arm == "F" and not batch.args.dry:
            subprocess.run(
                [sys.executable, "-m", "benchbook", "init", "--project", "boundary", "--pack", "code", "--yes"],
                cwd=work, env=env, check=True, capture_output=True, timeout=600,
            )
            # The registered manifest declares the same checks for every arm; the project mapping lives in the env.
            if git(work, "status", "--porcelain", "--untracked-files=no").stdout:
                raise ValueError("benchbook init changed tracked config; commit the registered checks and project"
                                 " mapping")
        result = Trial(batch, task, arm, target, work, env).run()
    finally:
        if arm == "D" and not batch.args.dry:
            stopped = subprocess.run(["claude-mem", "stop"], env=env, capture_output=True, timeout=30, text=True)
            (target / "memory-worker-stop.log").write_text(stopped.stdout + stopped.stderr, encoding="utf-8")
        if arm == "F" and not batch.args.dry:
            trial_error = sys.exception()
            try:
                stop_embedded(Path(env["BENCHBOOK_DATA_DIR"]))
            except (BenchbookError, OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
                message = f"could not stop the trial's embedded PostgreSQL: {exc}"
                if trial_error is None:
                    raise real.BenchRunError(message) from exc
                print(message, file=sys.stderr)
    return {"task": task.id, "arm": arm, "run": run, "wall_seconds": time.monotonic() - started, **result}


def options() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--dry", action="store_true")
    mode.add_argument("--execute", action="store_true")
    p.add_argument("--out", type=Path)
    p.add_argument("--tasks", type=Path, help="preregistered local task manifest")
    p.add_argument("--registration", type=Path, default=REGISTRATION)
    p.add_argument("--claude-mem-plugin", type=Path)
    p.add_argument("--arms", default="A,B,C,D,E,F")
    p.add_argument("--runs", type=int, default=3)
    p.add_argument("--model")
    p.add_argument("--max-budget-usd", type=float)
    p.add_argument("--probe-budget-usd", type=float)
    p.add_argument("--total-budget-usd", type=float)
    p.add_argument("--wall-clock", type=int, default=30, help="minutes per CLI invocation")
    p.add_argument("--max-turns", type=int, default=80)
    p.add_argument("--probe-turns", type=int, default=8)
    p.add_argument("--compact-window", type=int, default=100_000)
    p.add_argument("--claude-version", type=real.version_arg,
                   help="stop before any paid call unless claude --version is this (X.Y.Z)")
    p.add_argument("--claude-bin", help="the Claude Code binary to run (default: claude on PATH)")
    return p


def main() -> None:
    p = options()
    args = p.parse_args()
    chosen = args.arms.split(",")
    if not set(chosen) <= set(arms.ARMS) or len(chosen) != len(set(chosen)):
        p.error("--arms must be distinct arm letters separated by commas")
    if min(args.runs, args.wall_clock, args.max_turns, args.probe_turns) < 1 or args.compact_window < 100_000:
        p.error("run limits must be positive and --compact-window must be at least 100000")
    try:
        run(args, chosen)
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError, real.BenchRunError) as exc:
        p.exit(1, f"{exc}\n")


def run(args: argparse.Namespace, chosen: list[str]) -> None:
    out = (args.out or Path("bench/out") / f"boundary-{time.time_ns()}").resolve()
    tasks = input_tasks(args.tasks.resolve(strict=True)) if args.tasks and not args.dry else []
    version = None
    if args.execute:
        if args.out is None or not tasks or not args.model or any(
            value is None or not math.isfinite(value) or value <= 0 for value in (
            args.max_budget_usd, args.probe_budget_usd, args.total_budget_usd,
            )
        ):
            raise ValueError("--execute needs --out, --tasks, --model, and all three positive budget caps")
        registration = args.registration.read_text(encoding="utf-8")
        for field_name in ("Model", "Claude Code", "Date", "Budget", "Tasks", "Claude-mem", "Commits", "H5 budget"):
            field = re.search(rf"^\*\*{field_name}\*\*: (.+)$", registration, re.M)
            if not field or field[1].strip().startswith("["):
                raise ValueError(f"fill in the pre-registration field {field_name} before executing")
        if "D" in chosen:
            manifest = (args.claude_mem_plugin / ".claude-plugin" / "plugin.json") if args.claude_mem_plugin else None
            if manifest is None or not manifest.is_file() or shutil.which("claude-mem") is None:
                raise ValueError("--execute with arm D needs --claude-mem-plugin and the claude-mem launcher")
            args.claude_mem_plugin = args.claude_mem_plugin.resolve()
        version = real.pin_claude(args)
        match = real.CLAUDE_VERSION.match(version)
        if match is None or tuple(map(int, match.groups())) < MIN_CLAUDE_VERSION:
            required = ".".join(map(str, MIN_CLAUDE_VERSION))
            raise ValueError(f"--execute needs Claude Code {required} or newer for cumulative receipts "
                             f"(found {version!r})")
    if not args.execute and not args.dry:
        for arm in chosen:
            print(f"{arm} {arms.ARMS[arm].label}\n  {arms.ARMS[arm].instructions}")
        print(f"{len(tasks)} supplied tasks. See {REGISTRATION} and use --dry for a model-free run.")
        return

    out.mkdir(parents=True, exist_ok=False)
    data: dict[str, Any] = {"dry": args.dry, "trials": [], "fan_in": [], "compact_window": args.compact_window,
                            "arms": chosen}
    if args.dry:
        from .scripted import capture, fixture, scripted_fan_in

        args.model, args.claude_mem_plugin = "scripted", out / "fake-plugin"
        args.max_budget_usd = args.probe_budget_usd = 1
        args.total_budget_usd = math.inf
        tasks = [fixture(out)]
        data["fan_in"] = scripted_fan_in()
    else:
        capture = real._capture
        shutil.copyfile(args.registration, out / "registration.md")
        shutil.copyfile(args.tasks, out / "tasks.json")
        data.update(claude_version=version, model=args.model, repetitions=args.runs,
                    max_budget_usd=args.max_budget_usd, probe_budget_usd=args.probe_budget_usd,
                    total_budget_usd=args.total_budget_usd, wall_clock=args.wall_clock, max_turns=args.max_turns,
                    probe_turns=args.probe_turns,
                    oracle_hashes={task.id: hashlib.sha256(task.oracle.read_bytes()).hexdigest() for task in tasks})

    batch = Batch(args, out, capture)
    try:
        for task in tasks:
            for n in range(1, 2 if args.dry else args.runs + 1):
                for arm in chosen:
                    data["trials"].append(trial(batch, task, arm, n))
                    write_json(out / "metrics.json", data)
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError, real.BenchRunError) as exc:
        data["stopped"] = str(exc)
        write_json(out / "metrics.json", data)
        print(report.write(out))
        raise
    print(report.write(out))


if __name__ == "__main__":
    main()
