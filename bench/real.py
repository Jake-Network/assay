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
from collections.abc import Callable, Iterable, Iterator, Mapping
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
        "/benchbook-mcp:benchbook-lead {task} Run {rounds} rounds with {workers} workers per round and about {evals} "
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
# The same four classes as the final result event names them per model (result.modelUsage), in TOKEN_KEYS order.
MODEL_USAGE_KEYS = ("inputTokens", "cacheCreationInputTokens", "cacheReadInputTokens", "outputTokens")
SYNTHETIC_MODEL = "<synthetic>"


class BenchRunError(RuntimeError):
    """A run lacks the evidence required for comparison."""


@dataclass
class PreparedRun:
    work: Path
    cmd: list[str]
    env: dict[str, str]
    init: list[str] | None
    project_id: str | None
    session_id: str

    def command_env(self) -> dict[str, str]:
        names = ("BENCH_ROOT", "BENCH_EVALS", "CLAUDE_CODE_SUBAGENT_MODEL", "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS",
                 "ENABLE_CLAUDEAI_MCP_SERVERS")
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
           # The account's claude.ai connectors are not part of any arm. A and B drop every MCP server with
           # --strict-mcp-config, but C must keep the plugin's server, and without this it also loaded the connectors
           # of whoever ran it: 8 tools in the 2026-11-20 batch, 90 in the 2026-11-21 rerun (+50k tokens in the
           # lead's first prompt). --setting-sources '' does not cover them; this variable is the documented switch.
           "ENABLE_CLAUDEAI_MCP_SERVERS": "false",
           "PATH": os.pathsep.join((str(Path(sys.executable).parent), os.environ.get("PATH", "")))}
    for inherited in ("BENCHBOOK_PROJECT", "ASSAY_PROJECT"):  # the pre-rename name is read as a fallback
        env.pop(inherited, None)
    prompt = LEAD[mode].format(task=TASK, rounds=args.rounds, workers=args.workers, evals=args.evals) + REPORT
    tools = "Agent,Bash(python tools/evaluate.py:*),Read,Write,Glob"
    # Sessions are saved: a stream message carries the output count from the start of its response (the input classes
    # are final), and only the saved transcripts have the final output counts and the subagents' own files. The ID is
    # chosen here because the transcript folder is named after the working directory by an undocumented rule; the ID
    # finds it without guessing.
    session_id = str(uuid4())
    cmd = [
        claude_bin(args), "-p", prompt, "--output-format", "stream-json", "--verbose", "--forward-subagent-text",
        "--session-id", session_id, "--setting-sources", "", "--model", args.model, "--max-turns", str(args.max_turns),
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
        # 0.2: hooks in one plugin, the MCP server, worker and lead skill in benchbook-mcp
        cmd.extend(["--plugin-dir", str(ROOT / "integrations" / "claude-code"),
                    "--plugin-dir", str(ROOT / "integrations" / "claude-code-mcp")])
        tools += ",mcp__plugin_benchbook-mcp_benchbook__*"
    else:
        cmd.append("--strict-mcp-config")
    if mode == "B":
        (work / "results").mkdir()
    cmd.extend(["--allowedTools", tools])
    return PreparedRun(work, cmd, env, init, project_id, session_id)


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


PLUGIN_MCP_SERVER = "plugin:benchbook-mcp:benchbook"
OWN_SERVERS = (PLUGIN_MCP_SERVER, "plugin:benchbook:benchbook")  # the second: runs from before the plugin split


def session_mcp_servers(out: Path) -> dict[str, Any]:
    """The MCP servers the lead's session started with, from the stream's init event, and those among them that
    are not the arm's own (A and B: any; C: any but the plugin's). A foreign server's tool definitions sit in every
    prompt of the lead, so a run that has one does not measure the arm alone."""
    names: list[str] = []
    tools = 0
    try:
        with (out / "lead.stream.jsonl").open(encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if isinstance(event, dict) and event.get("type") == "system" and event.get("subtype") == "init":
                    servers = event.get("mcp_servers")
                    names = [str(s.get("name")) for s in servers if isinstance(s, dict)] if isinstance(
                        servers, list) else []
                    listed = event.get("tools")
                    tools = len(listed) if isinstance(listed, list) else 0
                    break
    except OSError:
        pass
    return {"servers": names, "foreign": [n for n in names if n not in OWN_SERVERS], "tools": tools}


CLAUDE_VERSION = re.compile(r"^(\d+)\.(\d+)\.(\d+)(?:\s|$)")


def version_arg(value: str) -> str:
    """argparse type of --claude-version: X.Y.Z."""
    import argparse

    if not re.fullmatch(r"\d+\.\d+\.\d+", value):
        raise argparse.ArgumentTypeError("use the version number Claude Code prints, such as 2.1.330")
    return value


def claude_bin(args: Any) -> str:
    """The CLI a driver runs: --claude-bin (such as ~/.local/share/claude/versions/2.1.330, which the auto-updater
    leaves in place), or claude on PATH."""
    return str(getattr(args, "claude_bin", None) or "claude")


def pin_claude(args: Any) -> str:
    """`claude --version` of the CLI a paid run will use, checked before its first paid call. With --claude-version
    a different version stops the run (ValueError); without it, a warning. The installed CLI updates itself between
    runs: 2.1.330 became 2.1.331 on 2026-11-24 during the H5 sweep, and DISABLE_AUTOUPDATER in a run's environment
    does not hold back the claude on PATH that the next run starts. Keep the checked executable for all calls."""
    requested = claude_bin(args)
    executable = shutil.which(requested)
    if executable is None:
        raise ValueError(f"could not find an executable for {requested} --version")
    try:
        binary = str(Path(executable).resolve())
        found = subprocess.run([binary, "--version"], capture_output=True, check=True, timeout=60,
                               text=True).stdout.strip()
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError(f"could not read {requested} --version: {exc}") from exc
    expected = getattr(args, "claude_version", None)
    match = CLAUDE_VERSION.match(found)
    if expected is None:
        print(f"warning: no --claude-version: running {binary} {found!r} unchecked", file=sys.stderr)
    elif match is None or ".".join(match.groups()) != expected:
        raise ValueError(f"--claude-version {expected}: {binary} --version says {found!r}; nothing was run "
                         "(pin the CLI with --claude-bin ~/.local/share/claude/versions/<version>)")
    args.claude_bin = binary
    return found


def claude_config_dir(env: Mapping[str, str]) -> Path:
    """Where Claude Code keeps its sessions: $CLAUDE_CONFIG_DIR, or ~/.claude."""
    return Path(env.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")


def collect_transcripts(config_dir: Path, session_id: str, out: Path) -> dict[str, Any]:
    """Find the session's saved transcripts and copy them next to the stream.

    Claude Code writes <config>/projects/<folder named after the cwd>/<session id>.jsonl for the lead and, beside
    it, <session id>/subagents/agent-<agent id>.jsonl for each subagent, with an agent-<agent id>.meta.json that
    names the Agent tool call (toolUseId) and the agent type. The copy is what the analysis reads: the retention
    sweep deletes the originals after cleanupPeriodDays. A missing transcript is reported, not raised; the stream is
    still a complete record of the run, only without final per-message output counts."""
    found: dict[str, Any] = {"config_dir": str(config_dir), "lead": None, "subagents": [], "copy": None}
    try:
        leads = sorted((config_dir / "projects").glob(f"*/{session_id}.jsonl"))
        if len(leads) != 1:
            found["error"] = f"expected one transcript named {session_id}.jsonl under projects/*/, found {len(leads)}"
            return found
        lead = leads[0]
        agents = sorted((lead.parent / session_id / "subagents").glob("agent-*.jsonl"))
        dest = out / "transcripts"
        (dest / "subagents").mkdir(parents=True, exist_ok=True)
        shutil.copy2(lead, dest / "lead.jsonl")
        for agent in agents:
            shutil.copy2(agent, dest / "subagents" / agent.name)
            meta = agent.with_suffix(".meta.json")
            if meta.is_file():
                shutil.copy2(meta, dest / "subagents" / meta.name)
        found.update(lead=str(lead), subagents=[str(agent) for agent in agents], copy="transcripts")
    except OSError as exc:
        found["error"] = f"{type(exc).__name__}: {exc}"
    return found


def snapshot_hook_spool(state_home: Path, out: Path) -> int:
    """Copy the hook entries that are still on disk (queued, dead or quarantined) into hook-spool/ and return how
    many. The replay that follows deletes each entry it records, and an entry is the only copy of what Claude Code
    handed the hook. Entries the background processor already recorded are gone by now; their trace is the line in
    state/benchbook/hook.log and the claim in the ledger export."""
    root = state_home / "benchbook" / "spool"
    copied = 0
    for path in sorted(root.rglob("*")) if root.is_dir() else ():
        if path.is_file() and path.suffix in (".json", ".invalid"):
            target = out / "hook-spool" / path.relative_to(root)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
            copied += 1
    return copied


def export_ledger(out: Path, env: Mapping[str, str], cwd: Path) -> int:
    """Write `benchbook export` of the run's project (BENCHBOOK_PROJECT in env) to ledger.jsonl; returns the record
    count. Nothing in the harness deletes the project, so its records also stay in the database the run used, but
    that database belongs to one machine and the export travels with the run directory."""
    target = out / "ledger.jsonl"
    with target.open("wb") as stdout:
        subprocess.run([sys.executable, "-m", "benchbook", "export"], cwd=cwd, env=dict(env), stdout=stdout,
                       check=True, timeout=120)
    return sum(1 for line in target.read_text(encoding="utf-8").splitlines() if line.strip())


def keep_ledger_evidence(kept: dict[str, Any], out: Path, env: Mapping[str, str], cwd: Path,
                         flush: Callable[[], None]) -> None:
    """Configuration C's persistent record, kept in the run directory and described in `kept` as it is written: what
    the hooks still held, then (after `flush` replayed it) the whole project. The export runs even when the replay
    fails, because that is the run whose records need looking at. The hook state itself needs no copy:
    XDG_STATE_HOME is <run>/state."""
    snapshotted = snapshot_hook_spool(Path(env["XDG_STATE_HOME"]), out)
    kept.update(hook_state="state/benchbook", hook_spool_snapshot="hook-spool" if snapshotted else None,
                hook_entries_snapshotted=snapshotted)
    try:
        flush()
    finally:
        kept.update(export="ledger.jsonl", records=export_ledger(out, env, cwd))


def run_real(mode: str, out: Path, run: int, args: Any) -> RunMetrics | None:
    prepared = prepare(mode, out, args)
    config_dir = claude_config_dir(prepared.env)
    meta: dict[str, Any] = {
        "cmd": prepared.cmd, "cwd": str(prepared.work), "env": prepared.command_env(),
        "init": prepared.init, "project_id": prepared.project_id, "max_budget_usd": args.max_budget_usd,
        "session_id": prepared.session_id,
        # Filled in after the run. Until then: where the transcripts will appear.
        "transcripts": {"config_dir": str(config_dir), "lead": None, "subagents": [], "copy": None,
                        "expected": {"lead": f"{config_dir}/projects/*/{prepared.session_id}.jsonl",
                                     "subagents": f"{config_dir}/projects/*/{prepared.session_id}/subagents/"
                                                  "agent-*.jsonl"}},
    }

    def write_meta() -> None:
        (out / "command.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")

    write_meta()
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
        try:
            _capture(prepared, out, args.wall_clock)
        finally:
            # A run that failed keeps its transcripts too: they show what the subagents were doing when it stopped.
            meta["transcripts"] = collect_transcripts(config_dir, prepared.session_id, out)
            meta["mcp"] = session_mcp_servers(out)
            write_meta()
            if meta["mcp"]["foreign"]:
                print(f"[{mode} run {run}] MCP servers that are not part of the arm were loaded: "
                      f"{', '.join(meta['mcp']['foreign'])}", file=sys.stderr)
            if error := meta["transcripts"].get("error"):
                print(f"[{mode} run {run}] no session transcripts: {error}", file=sys.stderr)
        if prepared.project_id:
            meta["ledger"] = {}
            try:
                keep_ledger_evidence(meta["ledger"], out, prepared.env, prepared.work, lambda: _flush_hooks(prepared))
            finally:
                write_meta()
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


def stream_messages(events: Iterable[tuple[int, dict[str, Any]]]) -> Iterator[tuple[bool, dict[str, Any]]]:
    """(is the lead's, API message) for every assistant event of the stream."""
    for _, event in events:
        if event.get("type") == "assistant" and isinstance(event.get("message"), dict):
            yield not event.get("parent_tool_use_id"), event["message"]


def worker_reports(events: list[tuple[int, dict[str, Any]]]) -> tuple[int | None, int]:
    """Estimate the size of actual Agent/Task results shown to the lead, once per tool call."""
    from benchbook.core.tokens import estimate_tokens

    agent_calls: set[str] = set()
    reports: dict[str, int] = {}
    for _, event in events:
        if event.get("parent_tool_use_id") or event.get("type") not in {"assistant", "user"}:
            continue
        message = event.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        for block in content if isinstance(content, list) else ():
            if not isinstance(block, dict):
                continue
            if (event["type"] == "assistant" and block.get("type") == "tool_use"
                    and block.get("name") in ("Agent", "Task") and isinstance(block.get("id"), str)):
                agent_calls.add(block["id"])
            ref = block.get("tool_use_id")
            if (event["type"] == "user" and block.get("type") == "tool_result" and isinstance(ref, str)
                    and ref in agent_calls and block.get("is_error") is not True):
                payload = block.get("content")
                if payload:
                    body = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
                    reports[ref] = max(reports.get(ref, 0), estimate_tokens(body))
    return (sum(reports.values()) if reports else None), len(reports)


NO_MODEL_PARAM = "(none)"


def model_counts(events: list[tuple[int, dict[str, Any]]]) -> tuple[dict[str, int], dict[str, int]]:
    """(subagent API messages per model, the lead's Agent/Task calls per `model` argument, NO_MODEL_PARAM for a call
    without one). A message streamed as several events (one per content block) counts once, by its id."""
    messages: dict[str, set[str]] = {}
    calls: dict[str, set[str]] = {}
    for n, (_, event) in enumerate(events):
        message = event.get("message")
        if event.get("type") != "assistant" or not isinstance(message, dict):
            continue
        if event.get("parent_tool_use_id"):
            if isinstance(model := message.get("model"), str) and model != SYNTHETIC_MODEL:
                messages.setdefault(model, set()).add(str(message.get("id") or f"event-{n}"))
            continue
        content = message.get("content")
        for block in content if isinstance(content, list) else ():
            if (isinstance(block, dict) and block.get("type") == "tool_use" and block.get("name") in ("Agent", "Task")
                    and isinstance(block.get("id"), str)):
                given = block.get("input", {}).get("model") if isinstance(block.get("input"), dict) else None
                calls.setdefault(given if isinstance(given, str) else NO_MODEL_PARAM, set()).add(block["id"])
    return ({k: len(v) for k, v in sorted(messages.items())}, {k: len(v) for k, v in sorted(calls.items())})


def transcript_messages(out: Path) -> Iterator[tuple[bool, dict[str, Any]]] | None:
    """The same pairs from the copied session transcripts, or None when the run has none."""
    root = out / "transcripts"
    if not (root / "lead.jsonl").is_file():
        return None

    def rows() -> Iterator[tuple[bool, dict[str, Any]]]:
        for path in (root / "lead.jsonl", *sorted((root / "subagents").glob("agent-*.jsonl"))):
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    row = json.loads(line)
                except ValueError:
                    continue  # transcripts are appended asynchronously; a torn last line carries no usage
                if isinstance(row, dict) and row.get("type") == "assistant" and isinstance(row.get("message"), dict):
                    yield path.name == "lead.jsonl" and not row.get("isSidechain"), row["message"]

    return rows()


def _class_counts(raw: Any, keys: tuple[str, ...] = TOKEN_KEYS) -> list[int] | None:
    if not isinstance(raw, dict):
        return None
    counts = [raw.get(key, 0) for key in keys]
    return None if any(type(n) is not int or n < 0 for n in counts) else counts


def _column_sums(rows: Iterable[list[int]]) -> list[int]:
    sums = [0] * len(TOKEN_KEYS)
    for row in rows:
        sums = [a + b for a, b in zip(sums, row, strict=True)]
    return sums


def tally_usage(messages: Iterable[tuple[bool, dict[str, Any]]]) -> dict[str, Any]:
    """Token classes per scope and per model with every API response counted once.

    One response arrives as several records (one per content block) that all repeat its message ID and usage, so the
    key is the message ID and a repeat never adds. Where records of one ID disagree, the larger count per class
    wins: an early record has the output count from the start of the response, a late one the final count.
    Claude Code's own placeholder messages (model "<synthetic>": an interrupted tool call, an API error) made no API
    call and are left out."""
    per_message: dict[str, tuple[bool, str, list[int]]] = {}
    unusable = 0
    for is_lead, message in messages:
        if message.get("model") == SYNTHETIC_MODEL:
            continue
        msg_id = message.get("id")
        counts = _class_counts(message.get("usage")) if _usage(message.get("usage")) is not None else None
        if counts is None or not isinstance(msg_id, str):
            unusable += 1
            continue
        model = str(message.get("model"))
        if msg_id in per_message:
            is_lead, model, seen = per_message[msg_id]
            counts = [max(a, b) for a, b in zip(seen, counts, strict=True)]
        per_message[msg_id] = is_lead, model, counts
    models: dict[str, list[int]] = {}
    for _, model, counts in per_message.values():
        models[model] = _column_sums([models.get(model, [0] * len(TOKEN_KEYS)), counts])
    return {"messages": len(per_message), "unusable_records": unusable,
            "lead_messages": sum(is_lead for is_lead, _, _ in per_message.values()),
            "lead": _column_sums(counts for is_lead, _, counts in per_message.values() if is_lead),
            "session": _column_sums(counts for _, _, counts in per_message.values()), "models": models}


def _named(counts: list[int] | None) -> dict[str, int] | None:
    return None if counts is None else {**dict(zip(TOKEN_KEYS, counts, strict=True)), "total": sum(counts)}


def _delta(got: list[int], want: list[int] | None) -> list[int] | None:
    return None if want is None else [a - b for a, b in zip(got, want, strict=True)]


def usage_check(out: Path, events: list[tuple[int, dict[str, Any]]] | None = None) -> dict[str, Any]:
    """Reconcile per-message usage with the totals of the final result event.

    The result event reports two totals: `usage` covers the lead's own API calls and `modelUsage` covers the whole
    session per model, subagents included (checked on the first benchmark's streams: the stream's lead messages add
    up to `usage` and all its messages to `modelUsage` in the three input classes). Each source of per-message usage
    (the stream, and the saved transcripts when the run has them) is compared with both, class by class, and per
    model. `reconciled` is true when one source matches every class of both totals. `prompt_side` says whether the
    three input classes match for the lead and for every model the source has messages of, which is what the
    per-turn context analysis needs; a model in `modelUsage` without any message (a side call of Claude Code's that
    no transcript shows) is listed under `models_without_messages` instead of failing it."""
    events = _read_stream(out) if events is None else events
    final = next((event for _, event in reversed(events) if event.get("type") == "result"), {})
    lead_total = _class_counts(final.get("usage")) if _usage(final.get("usage")) is not None else None
    per_model = final.get("modelUsage")
    model_totals = ({str(name): _class_counts(row, MODEL_USAGE_KEYS) for name, row in per_model.items()}
                    if isinstance(per_model, dict) else {})
    usable = {name: counts for name, counts in model_totals.items() if counts is not None}
    session_total = _column_sums(usable.values()) if usable and len(usable) == len(model_totals) else None

    def compare(tally: dict[str, Any]) -> dict[str, Any]:
        difference = {"lead": _delta(tally["lead"], lead_total), "session": _delta(tally["session"], session_total)}
        models = {name: _delta(counts, usable.get(name)) for name, counts in tally["models"].items()}
        matches: dict[str, bool | None] = {scope: None if diff is None else not any(diff)
                                           for scope, diff in difference.items()}
        prompt = [diff[:3] for diff in (difference["lead"], *models.values()) if diff is not None]
        unmatched_model = any(name not in usable for name in models) and bool(usable)
        matches["prompt_side"] = None if not prompt else not unmatched_model and not any(map(any, prompt))
        return {"messages": tally["messages"], "lead_messages": tally["lead_messages"],
                "unusable_records": tally["unusable_records"],
                "lead": _named(tally["lead"]), "session": _named(tally["session"]),
                "difference": {scope: _named(diff) for scope, diff in difference.items()},
                "models": {name: _named(diff) for name, diff in models.items()},
                "models_without_messages": {name: _named(usable[name]) for name in sorted(usable.keys()
                                                                                          - models.keys())},
                "matches": matches}

    sources: dict[str, dict[str, Any] | None] = {"stream": compare(tally_usage(stream_messages(events)))}
    saved = transcript_messages(out)
    sources["transcripts"] = compare(tally_usage(saved)) if saved is not None else None
    source = next((name for name in ("transcripts", "stream")
                   if (found := sources[name]) and found["matches"]["lead"] and found["matches"]["session"]), None)
    return {"final": {"lead": _named(lead_total), "session": _named(session_total),
                      "models": {name: _named(counts) for name, counts in model_totals.items()}},
            "sources": sources, "reconciled": source is not None, "source": source}


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
            " FROM benchbook.experiments WHERE project_id=%s"
            # runs recorded before the rename say assay-bench
            " AND trace_ref->>'system' IN ('benchbook-bench', 'assay-bench')", (project_id,))
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
    report_tokens, delivered_reports = worker_reports(events)
    subagent_models, agent_model_params = model_counts(events)
    return RunMetrics(mode, run, len(evals), duplicate_ratio(evals), lost, ctx, usage[1], final_best, curve,
                      cost_usd=cost, curve_source=curve_source, usage_check=usage_check(out, events),
                      lead_report_tokens=report_tokens, delivered_worker_reports=delivered_reports,
                      subagent_models=subagent_models, agent_model_params=agent_model_params)
