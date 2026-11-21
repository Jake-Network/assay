"""Where a benchmark run's tokens went, and what that says about the three cost hypotheses (spec §10).

    python -m bench.analyze_cost bench/out/<batch>                 # every <config>-<n> run directory below it
    python -m bench.analyze_cost bench/out/<batch>/A-1 .../C-1     # or the run directories themselves
    python -m bench.analyze_cost bench/out/<batch> --out bench/results/<name>/cost-breakdown.md

Reads what bench.real leaves in a run directory: lead.stream.jsonl (every model call of the lead and of the
subagents, with its usage), evals.jsonl and, when the run saved its sessions, transcripts/. Writes cost-breakdown.md.

How the attribution works. A model call's prompt is input + cache-creation + cache-read tokens. Between two calls
of the same agent the prompt grows by what entered the context in between: the blocks the model wrote (text, tool
calls) and the blocks that came back (tool results). That increase is measured. Inside one increase the split between
blocks is by their share of the chars/4 estimate, which is an estimate. A block that entered before call k of N is
billed again in every later call, so its tokens are counted N - k times ("carried"); together with the starting
prompt, re-read by every call, the carried tokens add up to the measured prompt total exactly.
"""
from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from benchbook.dedup import canonical_json
from benchbook.tokens import estimate_tokens

from .common import duplicate_ratio, load_evals
from .real import SYNTHETIC_MODEL, TOKEN_KEYS, BenchRunError, _read_stream, _usage, usage_check

# A hypothesis is "supported" when its mechanism accounts for at least SUPPORT of the gap it is meant to explain,
# "contradicted" below CONTRADICT, "partly supported" in between. The report prints both next to every verdict.
SUPPORT = 0.5
CONTRADICT = 0.2
REL_TOLERANCES = (0.1, 0.25, 0.5)  # "numerically close": every numeric field within this relative difference

BENCHBOOK_TOOLS = {
    "check_tried": "benchbook check_tried", "record_experiment": "benchbook record_experiment",
    "brief": "benchbook brief",
    "propose_claim": "benchbook claims", "decide": "benchbook claims", "link": "benchbook claims",
    "create_tasks": "benchbook tasks", "next_task": "benchbook tasks", "complete_task": "benchbook tasks",
    "cancel_task": "benchbook tasks", "heartbeat": "benchbook tasks",
}
OTHER_BENCHBOOK = "benchbook other"  # status, search, sql, q, get
LOOKUP_AND_RECORD = ("benchbook check_tried", "benchbook record_experiment")
EVAL_BASH, OTHER_BASH = "evaluation Bash", "other Bash"
FILE_READ, FILE_WRITE = "file Read", "file Write"
DELEGATION, REPORT, FINAL = "subagent delegation", "subagent report", "final report"
MODEL_TEXT, INJECTED, OTHER_TOOL = "model text and thinking", "skill and system text", "other tool"
UNSEEN = "not in the stream"  # the prompt grew and no block of the stream accounts for it
SHRINK = "context shrink"  # the prompt got smaller between two calls (compaction or cleared tool results)
START = "start of an agent"
NO_TOOL = "continuation without a tool call"
CATEGORY_ORDER = (
    "benchbook check_tried", "benchbook record_experiment", "benchbook brief", "benchbook claims", "benchbook tasks",
    OTHER_BENCHBOOK, EVAL_BASH, OTHER_BASH, FILE_READ, FILE_WRITE, DELEGATION, REPORT, FINAL, MODEL_TEXT, INJECTED,
    OTHER_TOOL, UNSEEN, SHRINK, START, NO_TOOL,
)


def is_benchbook(category: str) -> bool:
    return category.startswith("benchbook ")


def tool_category(name: str, tool_input: Any) -> str:
    """The cost category of one tool call. MCP tools are named mcp__<server>__<tool>; before the rename the plugin's
    server was plugin_assay_assay and its tools assay_<tool>."""
    if name.startswith("mcp__"):
        server, _, tool = name.removeprefix("mcp__").rpartition("__")
        if "benchbook" in server or "assay" in server:
            for prefix in ("benchbook_", "assay_"):
                tool = tool.removeprefix(prefix)
            return BENCHBOOK_TOOLS.get(tool, OTHER_BENCHBOOK)
        return OTHER_TOOL
    if name == "Bash":
        command = tool_input.get("command") if isinstance(tool_input, dict) else None
        return EVAL_BASH if isinstance(command, str) and "tools/evaluate.py" in command else OTHER_BASH
    return {"Read": FILE_READ, "Glob": FILE_READ, "Grep": FILE_READ, "Write": FILE_WRITE, "Edit": FILE_WRITE,
            "Agent": DELEGATION, "Task": DELEGATION, "StructuredOutput": FINAL, "Skill": INJECTED}.get(name, OTHER_TOOL)


@dataclass
class Block:
    category: str
    estimate: int  # chars/4 estimate of the block's text
    tool_call: bool = False


@dataclass
class Call:
    """One API response of one agent, with everything that enters the context before that agent's next call."""
    message_id: str
    prompt: int  # input + cache-creation + cache-read tokens
    entering: list[Block] = field(default_factory=list)


def _text_of(content: Any) -> str:
    """The text of a tool result or message content, however the harness nested it."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(_text_of(item) for item in content)
    if isinstance(content, dict) and isinstance(content.get("text"), str):
        return str(content["text"])
    return "" if content is None else json.dumps(content, ensure_ascii=False)


def read_calls(events: Iterable[tuple[int, dict[str, Any]]]) -> dict[str | None, list[Call]]:
    """Model calls per agent in stream order. The key is the Agent tool call that started a subagent
    (parent_tool_use_id), None for the lead. Blocks that precede an agent's first call are its starting prompt and
    are not listed: the first call's measured prompt already contains them."""
    calls: dict[str | None, list[Call]] = {}
    by_id: dict[tuple[str | None, str], Call] = {}
    tools: dict[str, str] = {}  # tool_use id -> category of the call
    for _, event in events:
        kind, message = event.get("type"), event.get("message")
        if kind not in ("assistant", "user") or not isinstance(message, dict):
            continue
        agent = event.get("parent_tool_use_id") or None
        content = message.get("content")
        blocks: list[Block] = []
        for block in content if isinstance(content, list) else [{"type": "text", "text": _text_of(content)}]:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                category = tool_category(str(block.get("name")), block.get("input"))
                tools[str(block.get("id"))] = category
                text = str(block.get("name")) + json.dumps(block.get("input"), ensure_ascii=False)
                blocks.append(Block(category, estimate_tokens(text), tool_call=True))
            elif block.get("type") == "tool_result":
                category = tools.get(str(block.get("tool_use_id")), OTHER_TOOL)
                # The lead reads a subagent's report as the result of the Agent call that delegated to it.
                category = REPORT if category == DELEGATION else category
                blocks.append(Block(category, estimate_tokens(_text_of(block.get("content")))))
            elif block.get("type") == "text":
                category = MODEL_TEXT if kind == "assistant" else INJECTED
                blocks.append(Block(category, estimate_tokens(_text_of(block.get("text")))))
            elif block.get("type") == "thinking":
                # Usually empty in the stream (only the signature is forwarded); its tokens then land on the other
                # blocks of the same increase, or on UNSEEN when there are none.
                blocks.append(Block(MODEL_TEXT, estimate_tokens(_text_of(block.get("thinking")))))
        if kind == "assistant":
            usage, msg_id = _usage(message.get("usage")), message.get("id")
            if usage is None or not isinstance(msg_id, str) or message.get("model") == SYNTHETIC_MODEL:
                # No API call to measure (Claude Code's placeholder messages) or none recorded. Its blocks join the
                # previous call's, so the next measured increase still covers them.
                if calls.get(agent):
                    calls[agent][-1].entering.extend(blocks)
                continue
            call = by_id.get((agent, msg_id))
            if call is None:
                call = by_id[agent, msg_id] = Call(msg_id, usage[0])
                calls.setdefault(agent, []).append(call)
            call.entering.extend(blocks)
        elif calls.get(agent):
            calls[agent][-1].entering.extend(blocks)
    return calls


def _add(table: dict[str, dict[str, float]], category: str, **amounts: float) -> None:
    row = table.setdefault(category, {})
    for key, amount in amounts.items():
        row[key] = row.get(key, 0) + amount


def attribute(calls: list[Call]) -> dict[str, Any]:
    """One agent's prompt tokens by category.

    growth[category]: blocks, estimated (chars/4), measured (its share of the measured increases), pure (the part
    of measured that came from increases with a single category, where no split was needed) and carried (measured
    times the number of calls that read it). turns[category]: calls triggered by that kind of tool call and the
    prompt tokens those calls were billed. start_reread is the first prompt times the number of calls."""
    growth: dict[str, dict[str, float]] = {}
    turns: dict[str, dict[str, float]] = {}
    total = len(calls)
    _add(turns, START, calls=1, prompt_tokens=calls[0].prompt)
    for k in range(1, total):
        before, call = calls[k - 1], calls[k]
        delta, reads = call.prompt - before.prompt, total - k
        weight = sum(block.estimate for block in before.entering)
        for block in before.entering:
            _add(growth, block.category, blocks=1, estimated=block.estimate)
        if delta < 0:
            _add(growth, SHRINK, measured=delta, pure=delta, carried=delta * reads)
        elif not weight:
            _add(growth, UNSEEN, measured=delta, pure=delta, carried=delta * reads)
        else:
            single = len({block.category for block in before.entering if block.estimate}) == 1
            for block in before.entering:
                share = delta * block.estimate / weight
                _add(growth, block.category, measured=share, pure=share if single else 0, carried=share * reads)
        triggers = [block.category for block in before.entering if block.tool_call] or [NO_TOOL]
        for category in triggers:
            _add(turns, category, calls=1 / len(triggers), prompt_tokens=call.prompt / len(triggers))
    return {"agents": 1, "calls": total, "start_prompt": calls[0].prompt, "start_reread": calls[0].prompt * total,
            "prompt_tokens": sum(call.prompt for call in calls), "growth": growth, "turns": turns,
            # Written after the last call (a closing report, the structured output): output only, never in a prompt.
            "never_read": sum(block.estimate for block in calls[-1].entering)}


def _merge(parts: list[dict[str, Any]]) -> dict[str, Any]:
    merged: dict[str, Any] = {"growth": {}, "turns": {}}
    for key in ("agents", "calls", "start_prompt", "start_reread", "prompt_tokens", "never_read"):
        merged[key] = sum(part[key] for part in parts)
    for table in ("growth", "turns"):
        for part in parts:
            for category, row in part[table].items():
                _add(merged[table], category, **row)
    return merged


def largest_lead_prompt(calls: list[Call]) -> dict[str, Any]:
    """What the lead's largest prompt was made of: its first prompt plus the increases up to that call."""
    top = max(range(len(calls)), key=lambda k: calls[k].prompt)
    return {"prompt": calls[top].prompt, "start_prompt": calls[0].prompt,
            "growth": {category: row["measured"] for category, row in attribute(calls[:top + 1])["growth"].items()
                       if row.get("measured")}}


# ---------------------------------------------------------------------------------------------- repeated evaluations


def _numeric(value: Any) -> bool:
    return type(value) in (int, float)


def near_config(a: Any, b: Any, rel_tol: float) -> bool:
    """Same keys and same non-numeric values, at least one numeric field different, and every numeric difference
    within rel_tol of the larger magnitude."""
    if not isinstance(a, dict) or not isinstance(b, dict) or a.keys() != b.keys():
        return False
    differs = False
    for key in a:
        x, y = a[key], b[key]
        if _numeric(x) and _numeric(y):
            if x != y:
                differs = True
                if abs(x - y) > rel_tol * max(abs(x), abs(y)):
                    return False
        elif canonical_json(x) != canonical_json(y):
            return False
    return differs


def classify_evals(evals: list[dict[str, Any]], rel_tol: float) -> dict[str, int]:
    """Each evaluation against the ones that finished before it, taking the strongest match:
    exact_repeat (same config, code and seed), replicate (same config and code, a seed not used for it before),
    near (no earlier evaluation of the same config, but one within rel_tol on its numeric fields), new."""
    seeds: dict[str, set[Any]] = {}
    earlier: dict[Any, list[Any]] = {}
    counts = {"exact_repeat": 0, "replicate": 0, "near": 0, "new": 0}
    for row in sorted(evals, key=lambda row: row.get("finished_ns", 0)):
        key = canonical_json([row["config"], row["code_ref"]])
        if key in seeds:
            counts["exact_repeat" if row["seed"] in seeds[key] else "replicate"] += 1
        elif any(near_config(row["config"], config, rel_tol) for config in earlier.get(row["code_ref"], ())):
            counts["near"] += 1
        else:
            counts["new"] += 1
        if key not in seeds:
            earlier.setdefault(row["code_ref"], []).append(row["config"])
        seeds.setdefault(key, set()).add(row["seed"])
    return counts


def repeats(evals: list[dict[str, Any]]) -> dict[str, Any]:
    return {"evaluations": len(evals), "duplicate_ratio": duplicate_ratio(evals),
            "by_tolerance": {str(tol): classify_evals(evals, tol) for tol in REL_TOLERANCES}}


# ---------------------------------------------------------------------------------------------- one run


def attachment_census(run_dir: Path) -> dict[str, dict[str, int]] | None:
    """Context the harness injected (system reminders, skill and agent listings), from the saved transcripts: lines
    of type "attachment" by attachment type, with a chars/4 estimate of the text as rendered into the prompt when
    the transcript records it. None when the run has no transcripts."""
    root = run_dir / "transcripts"
    if not (root / "lead.jsonl").is_file():
        return None
    census: dict[str, dict[str, int]] = {}
    for path in (root / "lead.jsonl", *sorted((root / "subagents").glob("agent-*.jsonl"))):
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if not isinstance(row, dict) or row.get("type") != "attachment":
                continue
            attachment = row.get("attachment")
            kind = str(attachment.get("type")) if isinstance(attachment, dict) else "unknown"
            entry = census.setdefault(kind, {"lines": 0, "estimated": 0})
            entry["lines"] += 1
            entry["estimated"] += estimate_tokens(_text_of(row["rendered"])) if "rendered" in row else 0
    return census


def run_config(run_dir: Path) -> str:
    match = re.fullmatch(r"([A-Za-z]+)-\d+", run_dir.name)
    return match.group(1) if match else run_dir.name


def analyze_run(run_dir: Path) -> dict[str, Any]:
    events = _read_stream(run_dir)
    calls = read_calls(events)
    if not calls.get(None):
        raise BenchRunError(f"{run_dir}: the stream has no model call of the lead")
    check = usage_check(run_dir, events)
    final, stream = check["final"], check["sources"]["stream"]
    tool_calls = Counter(block.category for agent_calls in calls.values() for call in agent_calls
                         for block in call.entering if block.tool_call)
    workers = [attribute(agent_calls) for agent, agent_calls in calls.items() if agent is not None]
    result = next((event for _, event in reversed(events) if event.get("type") == "result"), {})
    cost = result.get("total_cost_usd")
    return {
        "name": run_dir.name, "config": run_config(run_dir), "usage_check": check,
        # The final result event is the authority for totals; the stream's own sum stands in when it has none.
        "session": final["session"] or stream["session"], "lead_usage": final["lead"] or stream["lead"],
        "lead": attribute(calls[None]), "subagents": _merge(workers),
        "largest_lead_prompt": largest_lead_prompt(calls[None]), "tool_calls": dict(tool_calls),
        "repeats": repeats(load_evals(run_dir / "evals.jsonl")), "attachments": attachment_census(run_dir),
        "cost_usd": cost if type(cost) in (int, float) else None,
    }


# ---------------------------------------------------------------------------------------------- across runs


def _mean(values: Iterable[float]) -> float:
    listed = list(values)
    return statistics.mean(listed) if listed else 0.0


def _whole(run: dict[str, Any]) -> dict[str, Any]:
    return _merge([run["lead"], run["subagents"]])


def _table_mean(runs: list[dict[str, Any]], pick: Callable[[dict[str, Any]], dict[str, dict[str, float]]],
                key: str) -> dict[str, float]:
    """Mean over runs of table[category][key]; a run without the category counts as zero."""
    tables = [pick(run) for run in runs]
    categories = {category for table in tables for category in table}
    return {category: _mean(table.get(category, {}).get(key, 0) for table in tables) for category in categories}


def _key_mean(runs: list[dict[str, Any]], pick: Callable[[dict[str, Any]], dict[str, float]]) -> dict[str, float]:
    rows = [pick(run) for run in runs]
    return {key: _mean(row.get(key, 0) for row in rows) for key in {key for row in rows for key in row}}


def summarize(runs: list[dict[str, Any]]) -> dict[str, Any]:
    """Per-configuration means of everything the tables and the hypotheses use."""
    out: dict[str, Any] = {}
    for config in sorted({run["config"] for run in runs}):
        rs = [run for run in runs if run["config"] == config]
        first_tolerance = str(REL_TOLERANCES[0])  # exact repeats and replicates do not depend on the tolerance
        out[config] = {
            "runs": len(rs),
            "session_tokens": _mean(r["session"]["total"] for r in rs),
            "session_classes": {key: _mean(r["session"][key] for r in rs) for key in TOKEN_KEYS},
            "cost_usd": _mean(r["cost_usd"] for r in rs) if all(r["cost_usd"] is not None for r in rs) else None,
            "lead_tokens": _mean(r["lead_usage"]["total"] for r in rs),
            "output_tokens": _mean(r["session"]["output_tokens"] for r in rs),
            "prompt_tokens": {scope: _mean(r[scope]["prompt_tokens"] for r in rs) for scope in ("lead", "subagents")},
            "calls": {scope: _mean(r[scope]["calls"] for r in rs) for scope in ("lead", "subagents")},
            "agents": _mean(r["subagents"]["agents"] for r in rs),
            "start_prompt": {"lead": _mean(r["lead"]["start_prompt"] for r in rs),
                             "subagents": _mean(r["subagents"]["start_prompt"] / max(r["subagents"]["agents"], 1)
                                                for r in rs)},
            "start_reread": {scope: _mean(r[scope]["start_reread"] for r in rs) for scope in ("lead", "subagents")},
            "carried": _table_mean(rs, lambda r: _whole(r)["growth"], "carried"),
            "turn_tokens": _table_mean(rs, lambda r: _whole(r)["turns"], "prompt_tokens"),
            "turn_calls": _table_mean(rs, lambda r: _whole(r)["turns"], "calls"),
            "growth": {key: _table_mean(rs, lambda r: _whole(r)["growth"], key)
                       for key in ("blocks", "estimated", "measured", "pure")},
            "never_read": _mean(r["lead"]["never_read"] + r["subagents"]["never_read"] for r in rs),
            "lead_max": _mean(r["largest_lead_prompt"]["prompt"] for r in rs),
            "lead_max_growth": _key_mean(rs, lambda r: r["largest_lead_prompt"]["growth"]),
            "tool_calls": _key_mean(rs, lambda r: r["tool_calls"]),
            "evaluations": _mean(r["repeats"]["evaluations"] for r in rs),
            "duplicate_ratio": _mean(r["repeats"]["duplicate_ratio"] for r in rs),
            "exact_repeat": _mean(r["repeats"]["by_tolerance"][first_tolerance]["exact_repeat"] for r in rs),
            "replicate": _mean(r["repeats"]["by_tolerance"][first_tolerance]["replicate"] for r in rs),
            "near": {tol: _mean(r["repeats"]["by_tolerance"][tol]["near"] for r in rs)
                     for tol in map(str, REL_TOLERANCES)},
        }
    return out


def _verdict(share: float | None) -> str:
    if share is None:
        return "undetermined"
    return "supported" if share >= SUPPORT else "contradicted" if share < CONTRADICT else "partly supported"


def _pct(part: float, whole: float) -> str:
    return f"{part / whole:.1%}" if whole else "n/a"


def hypotheses(summary: dict[str, Any], rel_tol: float) -> list[dict[str, Any]]:
    """The three hypotheses of spec §10 with the numbers that decide each. Every verdict is computed here from the
    summary; nothing about the outcome is written down in advance."""
    a, b, c = summary.get("A"), summary.get("B"), summary.get("C")
    found: list[dict[str, Any]] = []

    # 1. Recording and looking up spend agent turns.
    lines: list[str] = []
    share: float | None = None
    if c:
        narrow = sum(c["turn_tokens"].get(category, 0) for category in LOOKUP_AND_RECORD)
        narrow_calls = sum(c["turn_calls"].get(category, 0) for category in LOOKUP_AND_RECORD)
        broad = sum(tokens for category, tokens in c["turn_tokens"].items() if is_benchbook(category))
        broad_calls = sum(n for category, n in c["turn_calls"].items() if is_benchbook(category))
        all_calls = c["calls"]["lead"] + c["calls"]["subagents"]
        tool_calls = sum(c["tool_calls"].get(category, 0) for category in LOOKUP_AND_RECORD)
        lines += [
            f"C made {c['tool_calls'].get('benchbook check_tried', 0):.1f} check_tried and "
            f"{c['tool_calls'].get('benchbook record_experiment', 0):.1f} record_experiment tool calls for "
            f"{c['evaluations']:.1f} evaluations per run (measured). They were followed by {narrow_calls:.1f} model "
            f"calls, {_ratio(tool_calls, narrow_calls)} tool calls per model call: "
            + ("the agents batch several lookups or records into one turn."
               if narrow_calls and tool_calls / narrow_calls >= 1.5 else "close to one turn for each."),
            f"Model calls triggered by check_tried or record_experiment: {narrow_calls:.1f} of {all_calls:.1f} per "
            f"run ({_pct(narrow_calls, all_calls)}), billed {narrow:,.0f} prompt tokens "
            f"({_pct(narrow, c['session_tokens'])} of C's session tokens; measured).",
            f"Model calls triggered by any benchbook tool: {broad_calls:.1f} per run "
            f"({_pct(broad_calls, all_calls)}), billed {broad:,.0f} prompt tokens "
            f"({_pct(broad, c['session_tokens'])} of C's session tokens; measured).",
        ]
        if a:
            excess = c["session_tokens"] - a["session_tokens"]
            lines += [
                f"C used {excess:,.0f} more session tokens per run than A ({c['session_tokens']:,.0f} against "
                f"{a['session_tokens']:,.0f}; measured). The calls that follow a lookup or a record account for "
                f"{_pct(narrow, excess)} of that gap, the calls that follow any benchbook tool for "
                f"{_pct(broad, excess)}.",
                f"Model calls per run: C {all_calls:.1f}, A {a['calls']['lead'] + a['calls']['subagents']:.1f}. "
                f"First prompt of a subagent: C {c['start_prompt']['subagents']:,.0f}, "
                f"A {a['start_prompt']['subagents']:,.0f} tokens (measured), read again by every one of its calls.",
            ]
            share = narrow / excess if excess > 0 else None
        else:
            lines.append("No A run among the inputs: the gap this hypothesis explains cannot be computed.")
    else:
        lines.append("No C run among the inputs.")
    found.append({"id": 1, "claim": "Recording and looking up spend agent turns: every attempt costs a separate "
                                    "check_tried and a separate record call.",
                  "measure": "prompt tokens of the model calls that follow check_tried or record_experiment, as a "
                             "share of C's session-token gap over A",
                  "share": share, "verdict": _verdict(share), "lines": lines})

    # 2. The exact hash misses nearby configurations; agents that read result files avoid them by eye.
    lines, share = [], None
    tol = str(rel_tol)
    if c:
        same = c["exact_repeat"] + c["replicate"]
        near = c["near"][tol]
        share = near / (same + near) if same + near else None
        lines += [
            f"C per run: {c['exact_repeat']:.1f} exact repeats (same config, code and seed), {c['replicate']:.1f} "
            f"replicates (same config and code, a new seed) and {near:.1f} evaluations within {rel_tol:.0%} of an "
            f"earlier config on every numeric field, out of {c['evaluations']:.1f} (measured from evals.jsonl).",
            f"Of C's evaluations that repeat or nearly repeat an earlier one, {_pct(near, same + near)} are near "
            "configs, which an exact hash cannot see; the rest have an identical config, which it does see.",
            f"The benchmark's duplicate ratio ({c['duplicate_ratio']:.1%} in C) counts an evaluation when its "
            "(config, code) occurs twice, whatever the seed: replicates raise it and near configs never do.",
        ]
        if b:
            fewer = b["near"][tol] < near
            lines.append(
                f"Near configs per run at {rel_tol:.0%}: B {b['near'][tol]:.1f}, C {near:.1f}"
                + (f", A {a['near'][tol]:.1f}" if a else "") + ". "
                + ("B has fewer, as the hypothesis expects of agents that read the result files." if fewer else
                   "B does not have fewer, so reading the result files did not make agents avoid them."))
            if not fewer and share is not None:
                share = 0.0  # the comparison the hypothesis rests on points the other way
        lines.append("Near configs per run by tolerance: " + "; ".join(
            f"{float(t):.0%}: " + ", ".join(f"{name} {summary[name]['near'][t]:.1f}" for name in sorted(summary))
            for t in map(str, REL_TOLERANCES)) + ".")
    else:
        lines.append("No C run among the inputs.")
    found.append({"id": 2, "claim": "The exact hash misses nearby configurations; agents that read result files "
                                    "avoid similar configurations by eye.",
                  "measure": f"near configs (within {rel_tol:.0%}) as a share of C's repeated or nearly repeated "
                             "evaluations, set to zero when B has at least as many near configs as C",
                  "share": share, "verdict": _verdict(share), "lines": lines})

    # 3. The brief grows the lead's context.
    lines, share = [], None
    if c:
        brief = c["lead_max_growth"].get("benchbook brief", 0)
        tools = sum(tokens for category, tokens in c["lead_max_growth"].items() if is_benchbook(category))
        lines += [
            f"C's largest lead prompt: {c['lead_max']:,.0f} tokens per run (measured), of which "
            f"{c['start_prompt']['lead']:,.0f} were there at the first call (system prompt, tool definitions, the "
            "lead skill and the task).",
            f"Of the growth after that, brief calls and results account for {brief:,.0f} tokens, all benchbook tool "
            f"calls and results for {tools:,.0f}, subagent reports for {c['lead_max_growth'].get(REPORT, 0):,.0f} "
            "(measured increases, split inside an increase by character share).",
        ]
        if a:
            excess = c["lead_max"] - a["lead_max"]
            first = c["start_prompt"]["lead"] - a["start_prompt"]["lead"]
            lines.append(
                f"C's largest lead prompt exceeds A's by {excess:,.0f} tokens ({a['lead_max']:,.0f} in A). The brief "
                f"accounts for {_pct(brief, excess)} of that gap, all benchbook tool calls and results for "
                f"{_pct(tools, excess)}, and the larger first prompt ({first:+,.0f} tokens) for "
                f"{_pct(first, excess)}. The parts need not add up to the gap: A's lead prompt holds "
                f"{a['lead_max_growth'].get(REPORT, 0):,.0f} tokens of subagent reports against C's "
                f"{c['lead_max_growth'].get(REPORT, 0):,.0f}.")
            share = brief / excess if excess > 0 else None
        else:
            lines.append("No A run among the inputs: the gap this hypothesis explains cannot be computed.")
    else:
        lines.append("No C run among the inputs.")
    found.append({"id": 3, "claim": "The brief grows the lead's context.",
                  "measure": "tokens of brief calls and results in the lead's largest prompt, as a share of the gap "
                             "between C's and A's largest lead prompt",
                  "share": share, "verdict": _verdict(share), "lines": lines})
    return found


# ---------------------------------------------------------------------------------------------- the document


def _ratio(part: float, whole: float) -> str:
    return f"{part / whole:.1f}" if whole else "n/a"


def _ordered(categories: Iterable[str]) -> list[str]:
    known = {name: i for i, name in enumerate(CATEGORY_ORDER)}
    return sorted(set(categories), key=lambda name: (known.get(name, len(known)), name))


def _yes(value: bool | None) -> str:
    return "n/a" if value is None else "yes" if value else "no"


def _gap(source: dict[str, Any]) -> str:
    """Why a run does not reconcile, in a few words: the class differences of the whole session (or of the lead)."""
    diff = source["difference"].get("session") or source["difference"].get("lead")
    if not diff:
        return " (no final total to compare with)"
    off = [f"{key} {value:+,}" for key, value in diff.items() if key != "total" and value]
    return f" ({', '.join(off)})" if off else ""


COMPARED = (("evaluations", "evaluations"), ("duplicate ratio", "duplicate_ratio"),
            ("lead context max (tokens)", "lead_context_max"), ("tokens total (lead, as in report.md)", "tokens_total"),
            ("API cost (USD)", "cost_usd"))


def report_values(run: dict[str, Any]) -> dict[str, float | None]:
    """This run's values of the report.md columns that the analysis can recompute, under report.md's definitions."""
    return {"evaluations": run["repeats"]["evaluations"], "duplicate_ratio": run["repeats"]["duplicate_ratio"],
            "lead_context_max": run["largest_lead_prompt"]["prompt"], "tokens_total": run["lead_usage"]["total"],
            "cost_usd": run["cost_usd"]}


def _num(value: float) -> str:
    return f"{value:,.0f}" if abs(value) >= 1000 else f"{value:.4g}"


def comparison(runs: list[dict[str, Any]], earlier: dict[str, Any]) -> list[str]:
    """Per-configuration means of this batch against an earlier batch's metrics.json (bench.run's output)."""
    lines = ["## Against an earlier batch", "",
             "Mean per run. The earlier values are that batch's metrics.json; these are recomputed from the runs "
             "above with the same definitions (measured).", "",
             "| configuration | metric | earlier | this batch | this / earlier |", "|---|---|---|---|---|"]
    before = [row for row in earlier.get("runs", []) if isinstance(row, dict)]
    for config in sorted({run["config"] for run in runs} & {str(row.get("config")) for row in before}):
        now = [report_values(run) for run in runs if run["config"] == config]
        then = [row for row in before if row.get("config") == config]
        for label, key in COMPARED:
            old = [row[key] for row in then if type(row.get(key)) in (int, float)]
            new = [row[key] for row in now if row[key] is not None]
            if not old or not new:
                continue
            o, n = _mean(old), _mean(new)
            lines.append(f"| {config} | {label} | {_num(o)} | {_num(n)} | {f'{n / o:.2f}' if o else 'n/a'} |")
    return [*lines, ""]


def _side_calls(source: dict[str, Any]) -> str:
    """Models the final total bills that no message of the source shows (side calls of Claude Code's)."""
    unseen = source.get("models_without_messages") or {}
    return "".join(f"; {name} has {row['total']:,} tokens and no message" for name, row in unseen.items())


def render(runs: list[dict[str, Any]], rel_tol: float, earlier: dict[str, Any] | None = None) -> str:
    summary = summarize(runs)
    configs = sorted(summary)
    head = " | ".join(configs)
    rule = "|---|" + "---|" * len(configs)
    doc = ["# Benchmark cost breakdown", "",
           "Written by `python -m bench.analyze_cost`. Every number is labelled. **Measured** numbers come from the "
           "API usage in the run's stream or from evals.jsonl. **Estimated** numbers are the chars/4 estimate of "
           "block text (`benchbook.tokens.estimate_tokens`). A measured increase of an agent's prompt between two "
           "model calls is split between the blocks that entered in between by their share of the estimate. That "
           "split is the only estimated part of a measured figure, and the `unsplit` column of section 4 shows "
           "how much needed none.", ""]

    doc += ["## Runs", "",
            "| run | model calls (lead + subagents) | session tokens (measured) | lead tokens (measured) | "
            "prompt classes reconcile | all classes reconcile | usage source |", "|---|---|---|---|---|---|---|"]
    for run in runs:
        check = run["usage_check"]
        best = check["sources"].get("transcripts") or check["sources"]["stream"]
        doc.append(f"| {run['name']} | {run['lead']['calls']} + {run['subagents']['calls']} | "
                   f"{run['session']['total']:,} | {run['lead_usage']['total']:,} | "
                   f"{_yes(check['sources']['stream']['matches']['prompt_side'])} | {_yes(check['reconciled'])} | "
                   f"{'transcripts' if check['sources'].get('transcripts') else 'stream'}"
                   f"{'' if check['reconciled'] else _gap(best)}{_side_calls(best)} |")
    reconciled = sum(run["usage_check"]["reconciled"] for run in runs)
    prompt_ok = sum(bool(run["usage_check"]["sources"]["stream"]["matches"]["prompt_side"]) for run in runs)
    doc += ["",
            f"{reconciled} of {len(runs)} runs reconcile per-message usage with the final totals in every token class. "
            f"{prompt_ok} of {len(runs)} reconcile the three prompt classes, which is what the attribution below "
            "uses. Session tokens are the final result event's `modelUsage` (lead and subagents). Lead tokens are "
            "its `usage`, which covers the lead's own API calls only and is what `tokens total` in report.md "
            "shows. A stream message carries the output count from the start of its response, so output tokens "
            "reconcile only from saved transcripts.", ""]

    if earlier is not None:
        doc += comparison(runs, earlier)

    doc += ["## 1. Session totals per configuration", "", "Mean per run. All measured.", "",
            "| configuration | runs | session tokens | lead prompt tokens | subagent prompt tokens | output tokens | "
            "lead calls | subagent calls | subagents | first prompt of the lead | first prompt of a subagent |",
            "|---|---|---|---|---|---|---|---|---|---|---|"]
    for config in configs:
        s = summary[config]
        doc.append(f"| {config} | {s['runs']} | {s['session_tokens']:,.0f} | {s['prompt_tokens']['lead']:,.0f} | "
                   f"{s['prompt_tokens']['subagents']:,.0f} | {s['output_tokens']:,.0f} | {s['calls']['lead']:.1f} | "
                   f"{s['calls']['subagents']:.1f} | {s['agents']:.1f} | {s['start_prompt']['lead']:,.0f} | "
                   f"{s['start_prompt']['subagents']:,.0f} |")
    doc.append("")

    doc += ["Token classes of the session, mean per run (measured). Cache reads are billed at a fraction of the other "
            "input classes, so the token ratio between configurations is not their cost ratio.", "",
            "| configuration | input | cache creation | cache read | output | cache read share | API cost (USD) |",
            "|---|---|---|---|---|---|---|"]
    for config in configs:
        s = summary[config]
        k = s["session_classes"]
        cost = "n/a" if s["cost_usd"] is None else f"{s['cost_usd']:.4f}"
        doc.append(f"| {config} | {k['input_tokens']:,.0f} | {k['cache_creation_input_tokens']:,.0f} | "
                   f"{k['cache_read_input_tokens']:,.0f} | {k['output_tokens']:,.0f} | "
                   f"{_pct(k['cache_read_input_tokens'], s['session_tokens'])} | {cost} |")
    doc.append("")

    prompt_total = {c: summary[c]["prompt_tokens"]["lead"] + summary[c]["prompt_tokens"]["subagents"] for c in configs}

    def cells(values: dict[str, float]) -> str:
        return " | ".join(f"{values[c]:,.0f} ({_pct(values[c], prompt_total[c])})" for c in configs)

    doc += ["## 2. Prompt tokens by what was read", "",
            "Mean per run, with the share of the configuration's prompt tokens. A block is counted once for every "
            "model call whose prompt contained it. Totals are measured; the split inside one increase is by "
            "character share.", "", f"| what | {head} |", rule,
            f"| first prompt of the lead, read by each of its calls | "
            f"{cells({c: summary[c]['start_reread']['lead'] for c in configs})} |",
            f"| first prompt of each subagent, read by each of its calls | "
            f"{cells({c: summary[c]['start_reread']['subagents'] for c in configs})} |"]
    for category in _ordered(cat for c in configs for cat in summary[c]["carried"]):
        values = {c: summary[c]["carried"].get(category, 0) for c in configs}
        if any(values.values()):
            doc.append(f"| {category} | {cells(values)} |")
    doc += [f"| **all prompt tokens (measured)** | {' | '.join(f'{prompt_total[c]:,.0f}' for c in configs)} |", ""]

    doc += ["## 3. Model calls by what triggered them", "",
            "Mean per run: model calls, and the prompt tokens those calls were billed (measured). A call is "
            "triggered by the tool calls of the response before it; a response with several kinds of tool call "
            "splits its follow-up call evenly between them.", "", f"| trigger | {head} |", rule]
    for category in _ordered(cat for c in configs for cat in summary[c]["turn_calls"]):
        doc.append(f"| {category} | " + " | ".join(
            f"{summary[c]['turn_calls'].get(category, 0):.1f} calls, {summary[c]['turn_tokens'].get(category, 0):,.0f}"
            f" ({_pct(summary[c]['turn_tokens'].get(category, 0), prompt_total[c])})" for c in configs) + " |")
    doc.append("")

    doc += ["## 4. Context growth by block category", "",
            "Mean per run. `estimated` is chars/4 of the blocks, `measured` is their share of the measured prompt "
            "increases, `unsplit` is the part of `measured` from increases with a single category.", "",
            "| configuration | category | blocks | estimated tokens | measured tokens | unsplit | "
            "measured / estimated |", "|---|---|---|---|---|---|---|"]
    for config in configs:
        g = summary[config]["growth"]
        for category in _ordered(g["blocks"].keys() | g["measured"].keys()):
            est, measured = g["estimated"].get(category, 0), g["measured"].get(category, 0)
            doc.append(f"| {config} | {category} | {g['blocks'].get(category, 0):.1f} | {est:,.0f} | {measured:,.0f} | "
                       f"{g['pure'].get(category, 0):,.0f} | {f'{measured / est:.2f}' if est else 'n/a'} |")
    doc.append("")

    doc += ["## 5. The lead's largest prompt", "",
            "Mean per run: the first prompt plus the measured increases up to the largest prompt, split by "
            "character share.", "", f"| part | {head} |", rule,
            "| first prompt (measured) | " + " | ".join(f"{summary[c]['start_prompt']['lead']:,.0f}" for c in configs)
            + " |"]
    for category in _ordered(cat for c in configs for cat in summary[c]["lead_max_growth"]):
        doc.append(f"| {category} | " + " | ".join(f"{summary[c]['lead_max_growth'].get(category, 0):,.0f}"
                                                    for c in configs) + " |")
    doc += ["| **largest prompt (measured)** | " + " | ".join(f"{summary[c]['lead_max']:,.0f}" for c in configs)
            + " |", ""]

    doc += ["## 6. Repeated and nearby evaluations", "",
            "From evals.jsonl (measured). Each evaluation is compared with the ones that finished before it in the "
            "same run. `near` counts evaluations with no earlier identical config but an earlier one with the same "
            "non-numeric fields and every numeric field within the tolerance.", "",
            "| run | evaluations | duplicate ratio | exact repeat | replicate | "
            + " | ".join(f"near at {tol:.0%}" for tol in REL_TOLERANCES) + " |",
            "|---|---|---|---|---|" + "---|" * len(REL_TOLERANCES)]
    for run in runs:
        d = run["repeats"]
        first = d["by_tolerance"][str(REL_TOLERANCES[0])]
        doc.append(f"| {run['name']} | {d['evaluations']} | {d['duplicate_ratio']:.1%} | {first['exact_repeat']} | "
                   f"{first['replicate']} | "
                   + " | ".join(str(d["by_tolerance"][str(tol)]["near"]) for tol in REL_TOLERANCES) + " |")
    doc.append("")

    doc += ["## Hypotheses (spec §10)", "",
            f"A verdict is computed from the stated measure: supported at {SUPPORT:.0%} or more, contradicted below "
            f"{CONTRADICT:.0%}, partly supported in between, undetermined when an input it needs is missing.", ""]
    for h in hypotheses(summary, rel_tol):
        value = "n/a" if h["share"] is None else f"{h['share']:.1%}"
        doc += [f"### Hypothesis {h['id']}: {h['claim']}", "",
                f"**Verdict: {h['verdict']}** ({h['measure']}: {value}).", ""]
        doc += [f"- {line}" for line in h["lines"]]
        doc.append("")

    doc += ["## What these numbers do not cover", "",
            "- System prompt, tool definitions, skill text and system reminders are not in the stream. They are "
            "inside each agent's first prompt (measured as a whole) and inside the increases, where they are split "
            "over the visible blocks or, with none, counted as `not in the stream`.",
            "- Thinking is forwarded without its text, so its tokens are split over the visible blocks of the same "
            "increase.",
            "- Output tokens are reported per session and per lead only. Blocks written after an agent's last "
            "model call (a subagent's closing report, the lead's structured output) were never part of a prompt "
            "and are left out of the tables; they come to "
            + ", ".join(f"{c} {summary[c]['never_read']:,.0f}" for c in configs)
            + " estimated tokens per run."]
    census = [(run["name"], run["attachments"]) for run in runs if run["attachments"]]
    if census:
        doc += ["", "### Injected context seen in the saved transcripts", "",
                "Lines of type `attachment` by attachment type, summed over the lead and its subagents. "
                "`estimated` is chars/4 of the text the transcript records as rendered into the prompt (0 when it "
                "records none).", "", "| run | attachment type | lines | estimated tokens |", "|---|---|---|---|"]
        for name, kinds in census:
            for kind, entry in sorted(kinds.items()):
                doc.append(f"| {name} | {kind} | {entry['lines']} | {entry['estimated']:,} |")
    return "\n".join(doc) + "\n"


def run_dirs(paths: list[Path]) -> list[Path]:
    """The given run directories; a directory without a stream stands for the run directories directly below it."""
    found: list[Path] = []
    for path in paths:
        if (path / "lead.stream.jsonl").is_file():
            found.append(path)
            continue
        below = sorted(child for child in path.iterdir() if (child / "lead.stream.jsonl").is_file()) \
            if path.is_dir() else []
        if not below:
            raise BenchRunError(f"{path}: no lead.stream.jsonl here or one level below")
        found.extend(below)
    return found


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("runs", nargs="+", type=Path, help="run directories, or a directory that holds them")
    ap.add_argument("--out", type=Path, default=None,
                    help="where to write the document (default: cost-breakdown.md beside the run directories)")
    ap.add_argument("--compare", type=Path, default=None, metavar="METRICS_JSON",
                    help="an earlier batch's metrics.json to compare with (bench/results/<name>/metrics.json)")
    ap.add_argument("--rel-tol", type=float, default=0.25, choices=REL_TOLERANCES,
                    help="relative tolerance that decides hypothesis 2 (default 0.25)")
    args = ap.parse_args(argv)
    try:
        earlier = json.loads(args.compare.read_text(encoding="utf-8")) if args.compare else None
        dirs = run_dirs(args.runs)
        runs = [analyze_run(path) for path in dirs]
        if len({run["name"] for run in runs}) < len(runs):  # the same <config>-<n> from two batches
            for run, path in zip(runs, dirs, strict=True):
                run["name"] = f"{path.resolve().parent.name}/{path.name}"
    except (BenchRunError, OSError, ValueError, KeyError) as exc:
        print(f"analyze_cost: {exc}", file=sys.stderr)
        return 1
    parents = {path.resolve().parent for path in dirs}
    out = args.out or (parents.pop() if len(parents) == 1 else Path.cwd()) / "cost-breakdown.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(runs, args.rel_tol, earlier), encoding="utf-8")
    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
