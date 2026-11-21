"""Matched-budget Claude Code runs and their post-run evidence."""

from __future__ import annotations

import json
import math
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO
from uuid import uuid4

from .common import CODE_REF, ROOT, RunMetrics, duplicate_ratio, load_evals

GOAL = "Maximize the sum of radii of 26 non-overlapping circles in the unit square."
TASK = (
    f"{GOAL} Evaluate configurations only with python tools/evaluate.py --config '<json>' --seed <n> "
    f"--agent <your-name>. Our code_ref is {CODE_REF}. Config keys are init (grid|hex|random|lattice_perturb), "
    "optimizer (slsqp|penalty), iters (10..5000), and perturb (0..0.2). Include all four keys, including when "
    "perturb is zero. The wrapper prints a JSON "
    "receipt with eval_id, config, code_ref, seed, run_status, value and notes. Keep a receipt for failures too. "
    "Never read the harness evaluation log. The wrapper stdout is how workers learn the outcome."
)
REPORT = (
    " Your final structured report must include a summary and an evaluations array. Include exact receipts for "
    "the individual results you know that are missing from the assigned persistent storage. For A, which uses "
    "messages only, include every result you still know. Copy eval_id, config, code_ref, seed, run_status and "
    "value without rounding. Include unsuccessful results. Omit results whose receipt you cannot report."
)
LEAD = {
    "A": (
        "{task} Run {rounds} rounds with {workers} general-purpose subagents in parallel per round. Ask "
        "each to explore a different part of the space with about {evals} evaluations and return its receipts "
        "to you as text. Plan from the reports. Use only messages for sharing results."
    ),
    "B": (
        "{task} Run {rounds} rounds with {workers} general-purpose subagents in parallel per round. Ask "
        "each to explore a different part of the space with about {evals} evaluations and write each wrapper "
        "receipt to results/<eval_id>.json right after the evaluation. Read results/ to plan the next round."
    ),
    "C": (
        "/benchbook:benchbook-lead {task} Run {rounds} rounds with {workers} workers per round and about {evals} "
        "evaluations per task. Include the wrapper command and these recording instructions in the task specs. "
        'Workers must pass code_ref both when checking and recording. Copy each receipt ID into trace_ref as '
        '{{"system":"benchbook-bench","trace_id":"<eval_id>"}}. Copy config, seed and run_status verbatim and,'
        ' when value '
        'is present, set metrics.sum_radii to that value. Use the ledger and brief to plan rounds. '
        'If a worker cannot record any result, its fallback handback should be a compact JSON object '
        '{{"evaluations":[<exact receipts>]}} so the hook can preserve those receipts.'
    ),
}
STATUSES = ("ok", "error", "timeout", "invalid")
RECEIPT_FIELDS = ("eval_id", "config", "code_ref", "seed", "run_status", "value")
REPORT_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["summary", "evaluations"],
    "properties": {
        "summary": {"type": "string"},
        "evaluations": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": list(RECEIPT_FIELDS),
            "properties": {
                "eval_id": {"type": "string"},
                "config": {
                    "type": "object", "additionalProperties": False,
                    "required": ["init", "optimizer", "iters", "perturb"],
                    "properties": {"init": {"type": "string"}, "optimizer": {"type": "string"},
                                   "iters": {"type": "integer"}, "perturb": {"type": "number"}},
                },
                "code_ref": {"type": "string"},
                "seed": {"type": "integer"},
                "run_status": {"enum": list(STATUSES)},
                "value": {"anyOf": [{"type": "number"}, {"type": "null"}]},
            },
        }},
    },
}
TOKEN_KEYS = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens")


class BenchRunError(RuntimeError):
    """A run lacks the evidence required for comparison."""


@dataclass
class PreparedRun:
    work: Path
    cmd: list[str]
    env: dict[str, str]
    init: list[str] | None
    project_id: str | None

    def command_env(self) -> dict[str, str]:
        names = ("BENCH_ROOT", "BENCH_EVALS", "CLAUDE_CODE_SUBAGENT_MODEL", "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS")
        overrides = {name: self.env[name] for name in names}
        if self.project_id:
            overrides["BENCHBOOK_PROJECT"] = self.project_id
            overrides["XDG_STATE_HOME"] = self.env["XDG_STATE_HOME"]
        return overrides


def prepare(mode: str, out: Path, args: Any) -> PreparedRun:
    out = out.resolve()
    work = out / "work"
    # Refuse an existing run directory. A replay gets fresh telemetry and a fresh project.
    out.mkdir(parents=True, exist_ok=False)
    (work / "tools").mkdir(parents=True)
    shutil.copy(ROOT / "bench" / "tools" / "evaluate.py", work / "tools" / "evaluate.py")
    env = {**os.environ, "BENCH_ROOT": str(ROOT), "BENCH_EVALS": str(out / "evals.jsonl"),
           "CLAUDE_CODE_SUBAGENT_MODEL": args.model,
           # Newer Claude Code starts subagents in the background by default; the lead then ends its turn before the
           # workers report and the stream carries one result event per wake-up. Every arm waits for its workers.
           "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS": "1",
           "PATH": os.pathsep.join((str(Path(sys.executable).parent), os.environ.get("PATH", "")))}
    env.pop("BENCHBOOK_PROJECT", None)
    prompt = LEAD[mode].format(task=TASK, rounds=args.rounds, workers=args.workers, evals=args.evals) + REPORT
    tools = "Agent,Bash(python tools/evaluate.py:*),Read,Write,Glob"
    cmd = [
        "claude", "-p", prompt, "--output-format", "stream-json", "--verbose", "--forward-subagent-text",
        "--no-session-persistence", "--setting-sources", "", "--model", args.model, "--max-turns", str(args.max_turns),
        "--max-budget-usd", str(args.max_budget_usd), "--tools", "Agent,Bash,Read,Write,Glob",
        "--json-schema", json.dumps(REPORT_SCHEMA),
    ]
    project_id = None
    init = None
    if mode == "C":
        project_id = f"bench-{uuid4().hex}"
        env["BENCHBOOK_PROJECT"] = project_id
        env["XDG_STATE_HOME"] = str(out / "state")
        init = ["benchbook", "init", "--project", project_id, "--goal", GOAL, "--metric", "sum_radii",
                "--direction", "max"]
        cmd.extend(["--plugin-dir", str(ROOT / "integrations" / "claude-code")])
        tools += ",mcp__plugin_benchbook_benchbook__*"
    else:
        cmd.append("--strict-mcp-config")
    if mode == "B":
        (work / "results").mkdir()
    cmd.extend(["--allowedTools", tools])
    return PreparedRun(work, cmd, env, init, project_id)


def _capture_stream(stdout: BinaryIO, stream: BinaryIO, timestamps: BinaryIO) -> None:
    for line in stdout:
        observed = time.monotonic_ns()
        stream.write(line)
        timestamps.write(f"{observed}\n".encode())


def _kill_group(pid: int) -> None:
    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _capture(prepared: PreparedRun, out: Path, wall_clock_minutes: int) -> None:
    with (
        (out / "lead.stream.jsonl").open("wb") as stream,
        (out / "lead.timestamps").open("wb") as timestamps,
        (out / "lead.stderr.log").open("wb") as stderr,
        subprocess.Popen(prepared.cmd, cwd=prepared.work, env=prepared.env, stdout=subprocess.PIPE, stderr=stderr,
                         start_new_session=True) as proc,
        ThreadPoolExecutor(max_workers=1) as reader,
    ):
        assert proc.stdout is not None
        drain = reader.submit(_capture_stream, proc.stdout, stream, timestamps)
        try:
            try:
                returncode = proc.wait(timeout=wall_clock_minutes * 60)
            except subprocess.TimeoutExpired as exc:
                raise BenchRunError(f"Claude timed out after {wall_clock_minutes} minutes") from exc
            if returncode != 0:
                raise BenchRunError(f"Claude exited with status {returncode}. See the lead stream and stderr logs")
            try:
                drain.result(timeout=5)
            except TimeoutError as exc:
                raise BenchRunError("Claude stream did not close after process exit") from exc
        except BaseException:
            _kill_group(proc.pid)
            raise


def _flush_hooks(prepared: PreparedRun) -> None:
    root = Path(prepared.env["XDG_STATE_HOME"]) / "benchbook" / "spool"
    for spool in root.iterdir() if root.is_dir() else ():
        if not spool.is_dir():
            continue
        subprocess.run([sys.executable, "-m", "benchbook.hooks", str(spool)], cwd=prepared.work, env=prepared.env,
                       check=True, timeout=130)
        if next(spool.glob("*.json"), None):
            raise BenchRunError("hook receipts are still queued after replay")


def run_real(mode: str, out: Path, run: int, args: Any) -> RunMetrics | None:
    prepared = prepare(mode, out, args)
    meta = {"cmd": prepared.cmd, "cwd": str(prepared.work), "env": prepared.command_env(),
            "init": prepared.init, "project_id": prepared.project_id, "max_budget_usd": args.max_budget_usd}
    (out / "command.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    if not args.execute:
        commands = [f"cd {shlex.quote(str(prepared.work))}"]
        commands.append("export " + shlex.join([f"{k}={v}" for k, v in prepared.command_env().items()]))
        if prepared.init:
            commands.append(shlex.join(prepared.init))
        commands.append(shlex.join(prepared.cmd))
        print(f"[{mode} run {run}] " + " && ".join(commands))
        return None

    try:
        if prepared.init:
            subprocess.run(prepared.init, cwd=prepared.work, env=prepared.env, check=True, timeout=120)
        _capture(prepared, out, args.wall_clock)
        if prepared.project_id:
            _flush_hooks(prepared)
        return parse(mode, out, run, prepared.project_id, args.max_budget_usd)
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        raise BenchRunError(str(exc)) from exc


def _usage(raw: Any) -> tuple[int, int] | None:
    """Return the model's input context and all billed token classes."""
    if not isinstance(raw, dict) or any(k not in raw for k in ("input_tokens", "output_tokens")):
        return None
    counts = [raw.get(k, 0) for k in TOKEN_KEYS]
    if any(type(n) is not int or n < 0 for n in counts):
        return None
    return sum(counts[:3]), sum(counts)


def _read_stream(out: Path) -> list[tuple[int, dict[str, Any]]]:
    rows = (out / "lead.stream.jsonl").read_text(encoding="utf-8").splitlines()
    stamps = (out / "lead.timestamps").read_text(encoding="utf-8").splitlines()
    if len(rows) != len(stamps) or not rows:
        raise BenchRunError("missing or incomplete Claude stream telemetry")
    observed = list(map(int, stamps))
    if observed[0] <= 0 or observed != sorted(observed):
        raise BenchRunError("malformed Claude stream timestamps")
    events = [(stamp, json.loads(row)) for stamp, row in zip(observed, rows, strict=True)]
    if any(not isinstance(event, dict) for _, event in events):
        raise BenchRunError("malformed Claude stream event")
    return events


def _number(value: Any) -> bool:
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


def _receipt(row: Any) -> tuple[Any, ...] | None:
    if not isinstance(row, dict):
        return None
    if (not isinstance(row.get("eval_id"), str) or not re.fullmatch(r"[0-9a-f]{32}", row["eval_id"])
            or not isinstance(row.get("config"), dict) or not isinstance(row.get("code_ref"), str)
            or type(row.get("seed")) is not int or row.get("run_status") not in STATUSES or "value" not in row):
        return None
    value = row["value"]
    if (row["run_status"] == "ok" and not _number(value)) or (row["run_status"] != "ok" and value is not None):
        return None
    return (row["eval_id"], row["config"], row["code_ref"], row["seed"], row["run_status"], value)


def _file_receipts(out: Path) -> list[Any]:
    receipts = []
    for path in (out / "work" / "results").glob("*.json"):
        try:
            receipts.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, UnicodeError, ValueError):
            # Files are model-authored evidence; a corrupt record cannot prove preservation.
            continue
    return receipts


def _ledger_receipts(project_id: str) -> list[Any]:
    import psycopg

    from benchbook import Ledger
    from benchbook.errors import BenchbookError

    try:
        lg = Ledger.open(project=project_id, agent="human")
        receipts = lg.sql_internal(
            "SELECT trace_ref->>'trace_id' AS eval_id, config, code_ref, seed, run_status, primary_value AS value"
            " FROM benchbook.experiments WHERE project_id=%s AND trace_ref->>'system'='benchbook-bench'", (project_id,))
        notes = lg.sql_internal(
            "SELECT payload->>'text' AS body FROM benchbook.claims WHERE project_id=%s AND kind='note'"
            " AND author='hook' AND 'auto-captured'=ANY(tags)", (project_id,))
    except (BenchbookError, psycopg.Error) as exc:
        raise BenchRunError(f"could not read benchmark project {project_id}") from exc
    captures: list[Any] = []
    for note in notes:
        try:
            body = json.loads(note["body"] or "")
        except ValueError:
            continue
        if isinstance(body, dict) and isinstance(body.get("evaluations"), list):
            captures.extend(body["evaluations"])
    return [*receipts, *captures]


def _retained(evals: list[dict[str, Any]], claimed: list[Any]) -> int:
    by_id = {row["eval_id"]: _receipt(row) for row in evals}
    retained = set()
    for item in claimed:
        if receipt := _receipt(item):
            eval_id = receipt[0]
            if by_id.get(eval_id) == receipt:
                retained.add(eval_id)
    return len(retained)


def _observed_curve(events: list[tuple[int, dict[str, Any]]], evals: list[dict[str, Any]],
                    total: int, final_best: float | None) -> tuple[int, list[tuple[int, float]], str]:
    per_message: dict[str, tuple[int, int]] = {}
    lead_seen: set[str] = set()
    lead_missing: set[str] = set()
    lead_context = 0
    consistent = True
    for observed, event in events:
        if event.get("type") != "assistant":
            continue
        msg = event.get("message")
        usage = _usage(msg.get("usage")) if isinstance(msg, dict) else None
        msg_id = msg.get("id") if isinstance(msg, dict) else None
        is_lead = not event.get("parent_tool_use_id")
        if usage is None:
            consistent = False
            if is_lead:
                if not isinstance(msg_id, str):
                    raise BenchRunError("missing top-level model context usage")
                lead_missing.add(msg_id)
            continue
        context, tokens = usage
        if is_lead:
            lead_context = max(lead_context, context)
            if isinstance(msg_id, str):
                lead_seen.add(msg_id)
        if not isinstance(msg_id, str):
            consistent = False
            continue
        if msg_id not in per_message:
            per_message[msg_id] = observed, tokens
        elif per_message[msg_id][1] != tokens:
            consistent = False
    if not lead_context or lead_missing - lead_seen:
        raise BenchRunError("missing top-level model context usage")
    final = [(total, final_best)] if final_best is not None else []
    if not consistent or sum(tokens for _, tokens in per_message.values()) != total:
        return lead_context, final, "final aggregate only"

    # Score evaluations already completed when each reconciled API usage checkpoint was observed.
    cumulative = 0
    next_eval = 0
    best = None
    curve = []
    for observed, tokens in sorted(per_message.values()):
        cumulative += tokens
        while next_eval < len(evals) and evals[next_eval]["finished_ns"] <= observed:
            value = evals[next_eval]["value"]
            if value is not None and (best is None or value > best):
                best = value
            next_eval += 1
        if best is not None and cumulative:
            curve.append((cumulative, best))
    if final and (not curve or curve[-1] != final[0]):
        curve.extend(final)
    return lead_context, curve, "reconciled assistant usage"


def parse(mode: str, out: Path, run: int, project_id: str | None, budget_usd: float) -> RunMetrics:
    events = _read_stream(out)
    results = [(observed, event) for observed, event in events if event.get("type") == "result"]
    if len(results) != 1:
        raise BenchRunError(f"expected one final Claude result event, found {len(results)}")
    finished, result = results[0]
    if result.get("subtype") != "success" or result.get("is_error") is not False:
        raise BenchRunError(f"Claude did not finish successfully ({result.get('subtype')})")
    usage = _usage(result.get("usage"))
    cost = result.get("total_cost_usd")
    if usage is None or not usage[1] or not _number(cost) or cost <= 0:
        raise BenchRunError("missing or invalid final token or dollar usage")
    if cost > budget_usd:
        raise BenchRunError(f"reported cost ${cost:.4f} exceeded the ${budget_usd:.4f} budget")
    report = result.get("structured_output")
    if (not isinstance(report, dict) or not isinstance(report.get("summary"), str)
            or not isinstance(report.get("evaluations"), list)):
        raise BenchRunError("missing structured final report")

    evals = load_evals(out / "evals.jsonl")
    if not evals:
        raise BenchRunError("no evaluations logged")
    if any(_receipt(row) is None or type(row.get("finished_ns")) is not int or not 0 < row["finished_ns"] <= finished
           for row in evals):
        raise BenchRunError("malformed or late evaluator telemetry")
    if len({row["eval_id"] for row in evals}) != len(evals):
        raise BenchRunError("repeated evaluator receipt ID")
    evals.sort(key=lambda row: row["finished_ns"])
    claimed = list(report["evaluations"])
    if mode == "B":
        claimed.extend(_file_receipts(out))
    if mode == "C":
        if not project_id:
            raise BenchRunError("missing benchmark project ID")
        claimed.extend(_ledger_receipts(project_id))
    lost = len(evals) - _retained(evals, claimed)
    final_best = max((e["value"] for e in evals if e["value"] is not None), default=None)
    ctx, curve, curve_source = _observed_curve(events, evals, usage[1], final_best)
    return RunMetrics(mode, run, len(evals), duplicate_ratio(evals), lost, ctx, usage[1], final_best, curve,
                      cost_usd=cost, curve_source=curve_source)
