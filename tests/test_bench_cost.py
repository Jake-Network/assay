"""The cost instrumentation of the real benchmark harness and bench.analyze_cost, on a stored stream.

tests/fixtures/bench/ holds two small runs in the shapes Claude Code 2.1.330 writes (event keys, one record per
content block, the output count of a stream message taken when its response started, the final result event's
`usage` for the lead and `modelUsage` for the whole session). C-1 also has the saved session transcripts, with the
final output counts, a placeholder message and a torn last line. The token numbers are made up so that every total
below can be checked by hand:

    C-1 lead calls       prompt 12003, 12404, 12705   (input 5, cache creation 12700, cache read 24407)
    C-1 subagent calls   prompt 6002, 6253, 6554, 6755 (input 5, cache creation 6750, cache read 18809)
    output               stream 17 + 13, transcripts 300 + 415
    A-1 lead / subagent  prompt 9003, 9504 / 5002, 5203; output stream 14, final 500
"""
from __future__ import annotations

import json
import shutil
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from bench import analyze_cost, real

FIXTURES = Path(__file__).parent / "fixtures" / "bench"
C1, A1 = FIXTURES / "C-1", FIXTURES / "A-1"


def classes(inp: int, cc: int, cr: int, out: int) -> dict[str, int]:
    return {"input_tokens": inp, "cache_creation_input_tokens": cc, "cache_read_input_tokens": cr,
            "output_tokens": out, "total": inp + cc + cr + out}


def stream_of(run: Path) -> list[tuple[int, dict[str, Any]]]:
    return real._read_stream(run)


# ---------------------------------------------------------------------------------------------- usage reconciliation


def test_tally_counts_each_response_once() -> None:
    tally = real.tally_usage(real.stream_messages(stream_of(C1)))
    # msg_L1 and msg_W3 arrive as two records each (one per content block): 9 records, 7 responses.
    assert sum(1 for _ in real.stream_messages(stream_of(C1))) == 9
    assert tally["messages"] == 7 and tally["lead_messages"] == 3 and tally["unusable_records"] == 0
    assert tally["lead"] == [5, 12700, 24407, 17]
    assert tally["session"] == [10, 19450, 43216, 30]
    assert tally["models"] == {"claude-sonnet-5-5": [10, 19450, 43216, 30]}


def test_tally_keeps_the_larger_count_and_skips_placeholders() -> None:
    def msg(msg_id: str, out: int, **extra: Any) -> tuple[bool, dict[str, Any]]:
        usage = {"input_tokens": 2, "cache_creation_input_tokens": 10, "cache_read_input_tokens": 100,
                 "output_tokens": out}
        return True, {"id": msg_id, "model": "m", "usage": usage, **extra}

    tally = real.tally_usage([
        msg("a", 1), msg("a", 40), msg("a", 7),  # the late record has the final output count
        msg("p", 0, model=real.SYNTHETIC_MODEL),  # Claude Code's placeholder: no API call
        (False, {"id": "b", "model": "m", "usage": {"output_tokens": 3}}),  # no input count: unusable
        (False, {"model": "m", "usage": {"input_tokens": 1, "output_tokens": 1}}),  # no ID: unusable
    ])
    assert tally["messages"] == 1 and tally["unusable_records"] == 2
    assert tally["lead"] == tally["session"] == [2, 10, 100, 40]


def test_usage_check_reconciles_prompt_classes_from_the_stream_and_all_classes_from_transcripts() -> None:
    check = real.usage_check(C1)
    assert check["final"]["lead"] == classes(5, 12700, 24407, 300)
    assert check["final"]["session"] == classes(10, 19450, 43216, 715)
    stream = check["sources"]["stream"]
    assert stream["matches"] == {"lead": False, "session": False, "prompt_side": True}
    assert stream["difference"]["lead"] == classes(0, 0, 0, -283)
    assert stream["difference"]["session"] == classes(0, 0, 0, -685)
    saved = check["sources"]["transcripts"]
    # The placeholder message and the torn last line of the subagent transcript add nothing.
    assert saved["messages"] == 7 and saved["unusable_records"] == 0
    assert saved["lead"] == classes(5, 12700, 24407, 300) and saved["session"] == classes(10, 19450, 43216, 715)
    assert saved["matches"] == {"lead": True, "session": True, "prompt_side": True}
    assert check["reconciled"] is True and check["source"] == "transcripts"


def test_usage_check_without_transcripts_reports_the_output_gap() -> None:
    check = real.usage_check(A1)
    assert check["sources"]["transcripts"] is None
    assert check["reconciled"] is False and check["source"] is None
    assert check["sources"]["stream"]["matches"]["prompt_side"] is True
    assert check["sources"]["stream"]["difference"]["session"] == classes(0, 0, 0, -486)


def test_usage_check_names_a_model_billed_without_messages(tmp_path: Path) -> None:
    run = tmp_path / "C-1"
    shutil.copytree(C1, run)
    rows = (run / "lead.stream.jsonl").read_text().splitlines()
    result = json.loads(rows[-1])
    side = {"inputTokens": 900, "outputTokens": 40, "cacheReadInputTokens": 0, "cacheCreationInputTokens": 0}
    result["modelUsage"]["claude-haiku-4-5"] = side
    (run / "lead.stream.jsonl").write_text("\n".join([*rows[:-1], json.dumps(result)]) + "\n")
    check = real.usage_check(run)
    saved = check["sources"]["transcripts"]
    assert saved["matches"] == {"lead": True, "session": False, "prompt_side": True}
    assert saved["models_without_messages"] == {"claude-haiku-4-5": classes(900, 0, 0, 40)}
    assert check["reconciled"] is False


# ---------------------------------------------------------------------------------------------- analysis


def test_attribution_of_the_fixture_run() -> None:
    run = analyze_cost.analyze_run(C1)
    lead, subs = run["lead"], run["subagents"]
    assert (lead["calls"], subs["calls"], subs["agents"]) == (3, 4, 1)
    assert lead["prompt_tokens"] == 12003 + 12404 + 12705 and subs["prompt_tokens"] == 6002 + 6253 + 6554 + 6755
    assert (lead["start_prompt"], subs["start_prompt"]) == (12003, 6002)
    growth = analyze_cost._merge([lead, subs])["growth"]
    # Increases with a single category need no split: brief (lead, +401), check_tried (+251) and Bash (+301).
    assert growth["benchbook brief"]["measured"] == growth["benchbook brief"]["pure"] == 401
    assert growth["benchbook brief"]["carried"] == 401 * 2  # read again by the lead's two later calls
    assert growth["benchbook check_tried"]["pure"] == 251
    assert growth["evaluation Bash"]["measured"] == growth["evaluation Bash"]["pure"] == 301
    # The record and the second lookup share one increase (+201), split by their chars/4 estimates.
    assert growth["benchbook record_experiment"]["measured"] + growth["benchbook check_tried"]["measured"] == \
        pytest.approx(251 + 201)
    assert growth["benchbook record_experiment"]["estimated"] > 0
    # Every prompt token is accounted for: the first prompts re-read by each call plus each increase times the
    # calls that read it give the measured prompt total exactly.
    for part in (lead, subs):
        carried = sum(row.get("carried", 0) for row in part["growth"].values())
        assert part["start_reread"] + carried == pytest.approx(part["prompt_tokens"])
    turns = analyze_cost._merge([lead, subs])["turns"]
    assert turns["benchbook check_tried"] == {"calls": 1.5, "prompt_tokens": 6253 + 6755 / 2}
    assert turns["benchbook record_experiment"] == {"calls": 0.5, "prompt_tokens": 6755 / 2}
    assert turns["benchbook brief"] == {"calls": 1, "prompt_tokens": 12404}
    assert turns["subagent delegation"] == {"calls": 1, "prompt_tokens": 12705}
    assert turns["start of an agent"] == {"calls": 2, "prompt_tokens": 12003 + 6002}
    assert run["largest_lead_prompt"]["prompt"] == 12705
    assert run["largest_lead_prompt"]["growth"]["benchbook brief"] == 401
    assert run["session"]["total"] == 63391 and run["lead_usage"]["total"] == 37412 and run["cost_usd"] == 0.0612
    assert run["tool_calls"]["benchbook check_tried"] == 2 and run["tool_calls"]["benchbook record_experiment"] == 1
    assert run["attachments"] == {"skill_listing": {"lines": 1, "estimated": 24}}


def test_categories_cover_both_plugin_names() -> None:
    for name in ("mcp__plugin_benchbook_benchbook__benchbook_check_tried",
                 "mcp__plugin_assay_assay__assay_check_tried"):
        assert analyze_cost.tool_category(name, {}) == "benchbook check_tried"
    assert analyze_cost.tool_category("mcp__plugin_assay_assay__assay_status", {}) == "benchbook other"
    assert analyze_cost.tool_category("Bash", {"command": "python tools/evaluate.py --seed 1"}) == "evaluation Bash"
    assert analyze_cost.tool_category("Bash", {"command": "ls results"}) == "other Bash"
    assert analyze_cost.tool_category("mcp__other__tool", {}) == "other tool"


X = {"init": "hex", "optimizer": "slsqp", "iters": 1000, "perturb": 0.01}


def evaluation(n: int, seed: int = 0, **config: Any) -> dict[str, Any]:
    return {"eval_id": f"{n:032x}", "config": {**X, **config}, "code_ref": "c", "seed": seed, "finished_ns": n}


def test_repeated_evaluations_are_told_apart_from_near_configs() -> None:
    evals = [evaluation(1), evaluation(2), evaluation(3, seed=1), evaluation(4, iters=1080),
             evaluation(5, iters=1300), evaluation(6, optimizer="penalty", iters=1000), evaluation(7, perturb=0.0)]
    assert analyze_cost.classify_evals(evals, 0.1) == {"exact_repeat": 1, "replicate": 1, "near": 1, "new": 4}
    # 1300 is within 25% of 1080 and 1000; a different optimizer is never near; perturb 0 against 0.01 is a 100%
    # relative difference at any tolerance.
    assert analyze_cost.classify_evals(evals, 0.25) == {"exact_repeat": 1, "replicate": 1, "near": 2, "new": 3}
    assert analyze_cost.near_config(X, {**X, "iters": 1100}, 0.1)
    assert not analyze_cost.near_config(X, X, 0.5)  # identical is a repeat, not near


def test_hypothesis_verdicts_are_computed_from_the_numbers() -> None:
    runs = [analyze_cost.analyze_run(path) for path in (A1, C1)]
    summary = analyze_cost.summarize(runs)
    found = {h["id"]: h for h in analyze_cost.hypotheses(summary, 0.25)}
    gap = 63391 - 29212
    assert found[1]["share"] == pytest.approx((6253 + 6755) / gap)
    assert found[1]["verdict"] == "partly supported"
    assert found[2]["share"] == pytest.approx(1 / 3) and found[2]["verdict"] == "partly supported"
    assert found[3]["share"] == pytest.approx(401 / (12705 - 9504)) and found[3]["verdict"] == "contradicted"

    # The same code reaches the other verdicts when the numbers change.
    summary["C"]["turn_tokens"]["benchbook record_experiment"] = gap
    summary["C"]["lead_max_growth"]["benchbook brief"] = 3000
    summary["C"]["near"]["0.25"] = 0
    found = {h["id"]: h for h in analyze_cost.hypotheses(summary, 0.25)}
    assert [found[i]["verdict"] for i in (1, 2, 3)] == ["supported", "contradicted", "supported"]
    b = summary["B"] = dict(summary["C"], near={"0.1": 0, "0.25": 5, "0.5": 5})
    summary["C"]["near"] = {"0.1": 0, "0.25": 4, "0.5": 4}
    found = {h["id"]: h for h in analyze_cost.hypotheses(summary, 0.25)}
    assert found[2]["share"] == 0 and "B does not have fewer" in found[2]["lines"][3]
    b["near"] = {"0.1": 0, "0.25": 1, "0.5": 1}
    assert analyze_cost.hypotheses(summary, 0.25)[1]["verdict"] == "supported"  # 4 near of 2 + 4
    del summary["A"]
    assert [h["verdict"] for h in analyze_cost.hypotheses(summary, 0.25)][0::2] == ["undetermined"] * 2


def test_cost_breakdown_document(tmp_path: Path) -> None:
    earlier = {"runs": [{"config": "C", "evaluations": 4, "duplicate_ratio": 0.25, "lead_context_max": 10000,
                         "tokens_total": 30000, "cost_usd": 0.05}]}
    (tmp_path / "metrics.json").write_text(json.dumps(earlier))
    out = tmp_path / "cost-breakdown.md"
    assert analyze_cost.main([str(FIXTURES), "--out", str(out), "--compare", str(tmp_path / "metrics.json")]) == 0
    doc = out.read_text()
    assert "| C-1 | 3 + 4 | 63,391 | 37,412 | yes | yes | transcripts |" in doc
    assert "| A-1 | 2 + 2 | 29,212 | 18,707 | yes | no | stream (output_tokens -486) |" in doc
    assert "| C | tokens total (lead, as in report.md) | 30,000 | 37,412 | 1.25 |" in doc
    assert "| C | lead context max (tokens) | 10,000 | 12,705 | 1.27 |" in doc
    for heading in ("Hypothesis 1", "Hypothesis 2", "Hypothesis 3"):
        assert f"### {heading}" in doc
    assert doc.count("**Verdict: ") == 3 and "(measured)" in doc and "estimated" in doc
    assert analyze_cost.main([str(tmp_path / "nothing-here")]) == 1


# ---------------------------------------------------------------------------------------------- the harness


def args(**overrides: object) -> SimpleNamespace:
    defaults = {"model": "sonnet", "rounds": 1, "workers": 1, "evals": 1, "max_turns": 20, "max_budget_usd": 0.75,
                "wall_clock": 2, "execute": False}
    return SimpleNamespace(**{**defaults, **overrides})


def test_session_is_saved_under_an_id_chosen_in_advance(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    out = tmp_path / "A-1"
    assert real.run_real("A", out, 1, args()) is None
    meta = json.loads((out / "command.json").read_text())
    cmd = meta["cmd"]
    assert "--no-session-persistence" not in cmd
    assert cmd[cmd.index("--session-id") + 1] == meta["session_id"] == str(uuid.UUID(meta["session_id"]))
    expected = meta["transcripts"]["expected"]
    assert expected["lead"] == f"{tmp_path / 'claude'}/projects/*/{meta['session_id']}.jsonl"
    assert expected["subagents"].endswith(f"/{meta['session_id']}/subagents/agent-*.jsonl")


@pytest.mark.parametrize("mode", ["A", "B", "C"])
def test_no_arm_loads_the_accounts_connectors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    """C keeps the plugin's MCP server, so it cannot use --strict-mcp-config; the connectors of the account that
    runs the benchmark are switched off for every arm instead."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    monkeypatch.setenv("ENABLE_CLAUDEAI_MCP_SERVERS", "true")  # whatever the shell says
    prepared = real.prepare(mode, tmp_path / f"{mode}-1", args())
    assert prepared.env["ENABLE_CLAUDEAI_MCP_SERVERS"] == "false"
    assert prepared.command_env()["ENABLE_CLAUDEAI_MCP_SERVERS"] == "false"  # shown in the preview and command.json
    assert ("--strict-mcp-config" in prepared.cmd) == (mode != "C")


def test_foreign_mcp_servers_of_a_session_are_named(tmp_path: Path) -> None:
    init = {"type": "system", "subtype": "init", "tools": ["Agent", "mcp__claude_ai_Example__x"],
            "mcp_servers": [{"name": "plugin:benchbook:benchbook", "status": "connected"},
                            {"name": "claude.ai Example", "status": "connected"}]}
    (tmp_path / "lead.stream.jsonl").write_text("not json\n" + json.dumps(init) + "\n")
    assert real.session_mcp_servers(tmp_path) == {
        "servers": ["plugin:benchbook:benchbook", "claude.ai Example"], "foreign": ["claude.ai Example"], "tools": 2}
    assert real.session_mcp_servers(tmp_path / "missing") == {"servers": [], "foreign": [], "tools": 0}


def place_transcripts(config_dir: Path, cwd: Path, session_id: str) -> Path:
    """Claude Code's layout: projects/<cwd with every other character as '-'>/<session>.jsonl, and the subagents in
    <session>/subagents/agent-<id>.jsonl with a .meta.json beside each."""
    folder = config_dir / "projects" / "".join(ch if ch.isalnum() else "-" for ch in str(cwd))
    (folder / session_id / "subagents").mkdir(parents=True)
    shutil.copy(C1 / "transcripts" / "lead.jsonl", folder / f"{session_id}.jsonl")
    for path in (C1 / "transcripts" / "subagents").iterdir():
        shutil.copy(path, folder / session_id / "subagents" / path.name)
    return folder


def test_collect_transcripts(tmp_path: Path) -> None:
    sid = str(uuid.uuid4())
    folder = place_transcripts(tmp_path / "claude", tmp_path / "work", sid)
    out = tmp_path / "run"
    found = real.collect_transcripts(tmp_path / "claude", sid, out)
    assert found["lead"] == str(folder / f"{sid}.jsonl") and found["copy"] == "transcripts"
    assert found["subagents"] == [str(folder / sid / "subagents" / "agent-a1f2e3d4c5b6a7980.jsonl")]
    assert (out / "transcripts" / "lead.jsonl").read_text() == (C1 / "transcripts" / "lead.jsonl").read_text()
    assert (out / "transcripts" / "subagents" / "agent-a1f2e3d4c5b6a7980.meta.json").is_file()
    missing = real.collect_transcripts(tmp_path / "claude", str(uuid.uuid4()), tmp_path / "other")
    assert missing["lead"] is None and "found 0" in missing["error"]


STAND_IN = r'''
import json, os, shutil, sys, time
from pathlib import Path

fixture, cwd = Path(sys.argv[1]), Path.cwd()
session_id = sys.argv[sys.argv.index("--session-id") + 1]
with open(os.environ["BENCH_EVALS"], "a") as f:
    for line in (fixture / "evals.jsonl").read_text().splitlines():
        f.write(json.dumps(dict(json.loads(line), finished_ns=time.monotonic_ns())) + "\n")
folder = Path(os.environ["CLAUDE_CONFIG_DIR"]) / "projects" / "".join(c if c.isalnum() else "-" for c in str(cwd))
(folder / session_id).mkdir(parents=True)
shutil.copy(fixture / "transcripts" / "lead.jsonl", folder / f"{session_id}.jsonl")
shutil.copytree(fixture / "transcripts" / "subagents", folder / session_id / "subagents")
if os.environ.get("BENCHBOOK_PROJECT"):  # what the plugin's SubagentStop hook leaves when its processor is behind
    spool = Path(os.environ["XDG_STATE_HOME"]) / "benchbook" / "spool" / "stand-in"
    spool.mkdir(parents=True)
    data = {"hook_event_name": "SubagentStop", "session_id": session_id, "agent_id": "a1f2e3d4c5b6a7980",
            "agent_type": "benchbook:benchbook-worker", "last_assistant_message": "Recorded e:1a2b3c4d: 2.6359317682."}
    (spool / "entry.json").write_text(json.dumps({"event": "subagent-stop", "project": os.environ["BENCHBOOK_PROJECT"],
                                                  "data": data, "spooled_at": time.time(), "accepted": True}))
for line in (fixture / "lead.stream.jsonl").read_text().splitlines():
    print(line, flush=True)
'''


@pytest.mark.parametrize("mode", ["A", "C"])
def test_real_run_keeps_transcripts_usage_check_and_ledger(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                           db_url: str, mode: str) -> None:
    """run_real end to end with a stand-in for `claude` that replays the C-1 fixture."""
    monkeypatch.setenv("BENCHBOOK_DATABASE_URL", db_url)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    stand_in = tmp_path / "claude_stand_in.py"
    stand_in.write_text(STAND_IN)
    prepare = real.prepare

    def prepare_stand_in(*a: Any) -> real.PreparedRun:
        prepared = prepare(*a)
        assert prepared.cmd[0] == "claude"
        prepared.cmd[:1] = [sys.executable, str(stand_in), str(C1)]  # never the real Claude Code
        return prepared

    monkeypatch.setattr(real, "prepare", prepare_stand_in)
    out = tmp_path / f"{mode}-1"
    metrics = real.run_real(mode, out, 1, args(execute=True))
    assert metrics is not None and metrics.tokens_total == 37412  # bench.report's number is unchanged
    assert metrics.usage_check is not None
    assert metrics.usage_check["reconciled"] is True and metrics.usage_check["source"] == "transcripts"
    meta = json.loads((out / "command.json").read_text())
    transcripts = meta["transcripts"]
    assert Path(transcripts["lead"]).name == f"{meta['session_id']}.jsonl"
    assert Path(transcripts["lead"]).is_relative_to(tmp_path / "claude" / "projects")
    assert [Path(p).name for p in transcripts["subagents"]] == ["agent-a1f2e3d4c5b6a7980.jsonl"]
    assert (out / "transcripts" / "subagents" / "agent-a1f2e3d4c5b6a7980.jsonl").is_file()
    if mode == "A":
        assert "ledger" not in meta and not (out / "ledger.jsonl").exists()
        return
    ledger = meta["ledger"]
    assert ledger["hook_entries_snapshotted"] == 1 and ledger["export"] == "ledger.jsonl"
    assert json.loads((out / "hook-spool" / "stand-in" / "entry.json").read_text())["event"] == "subagent-stop"
    assert not list((out / "state" / "benchbook" / "spool" / "stand-in").glob("*.json"))  # replayed
    records = [json.loads(line) for line in (out / "ledger.jsonl").read_text().splitlines()]
    assert len(records) == ledger["records"]
    assert records[0] == {"type": "project", "record": records[0]["record"]}
    assert records[0]["record"]["id"] == meta["project_id"]
    notes = [r["record"] for r in records if r["type"] == "claim"]
    assert len(notes) == 1 and "auto-captured" in notes[0]["tags"]


def test_dry_run_leaves_what_a_real_run_leaves(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, db_url: str) -> None:
    pytest.importorskip("scipy")
    from bench.dry import run_dry

    monkeypatch.setenv("BENCHBOOK_DATABASE_URL", db_url)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg-state"))
    dry_args = SimpleNamespace(workers=2, rounds=1, evals=2, budget=40000, drop_rate=0.0, compact_at=3000, seed=0)
    for mode in ("A", "C"):
        out = tmp_path / "batch" / f"{mode}-1"
        metrics = run_dry(mode, out, 1, dry_args)
        assert metrics.usage_check is not None and metrics.usage_check["reconciled"] is True
        assert metrics.usage_check["sources"]["stream"]["matches"]["prompt_side"] is True
        meta = json.loads((out / "command.json").read_text())
        assert len(meta["transcripts"]["subagents"]) == 2 and (out / "transcripts" / "lead.jsonl").is_file()
    meta = json.loads((tmp_path / "batch" / "C-1" / "command.json").read_text())
    assert meta["ledger"]["hook_entries_snapshotted"] == 2 and meta["ledger"]["records"] > 2
    assert analyze_cost.main([str(tmp_path / "batch")]) == 0
    doc = (tmp_path / "batch" / "cost-breakdown.md").read_text()
    assert "benchbook check_tried" in doc and doc.count("**Verdict: ") == 3
