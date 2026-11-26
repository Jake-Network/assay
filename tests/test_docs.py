"""Every SQL example in docs/schema.md runs through benchbook_sql."""
from __future__ import annotations

import re
from pathlib import Path

from benchbook import Ledger

DOC = Path(__file__).resolve().parent.parent / "docs" / "schema.md"


def test_schema_examples(lead: Ledger, worker: Ledger) -> None:
    w2 = lead.as_agent("worker-2")
    ids = []
    for s, (w, opt) in enumerate([(worker, "slsqp"), (w2, "slsqp"), (worker, "penalty")]):
        ids.append(w.record_experiment({"init": "hex", "optimizer": opt}, {"score": 2.0 + s}, seed=s).id)
    worker.record_experiment({"init": "grid", "optimizer": "penalty"}, {}, run_status="error", notes="boom")
    c = worker.propose_claim("hypothesis", "hex helps", evidence=[(ids[0], "supports")])  # type: ignore[list-item]
    w2.propose_claim("hypothesis", "hex helps", evidence=[(ids[1], "supports")])  # type: ignore[list-item]
    lead.decide(c.id, "supported", "metric", "ok", ids[:2])  # type: ignore[arg-type]
    queries = re.findall(r"```sql\n(.*?)```", DOC.read_text(), re.S)
    assert len(queries) == 10
    for q in queries:
        r = lead.sql(q)
        assert "rows" in r, q
    assert lead.sql(queries[0])["rows"][0]["n_support_authors"] == 2


def test_autoresearch_template_matches_package() -> None:
    root = Path(__file__).resolve().parent.parent
    packaged = root / "src" / "benchbook" / "integrations" / "autoresearch" / "templates" / "program.benchbook.md"
    copy = root / "integrations" / "autoresearch" / "template" / "program.benchbook.md"
    assert packaged.read_text() == copy.read_text()
    text = packaged.read_text()
    assert "MIT" in text and "0. benchbook ar replicate --auto" in text and "NEVER STOP" in text


def test_readmes_say_what_benchbook_fits() -> None:
    root = Path(__file__).resolve().parent.parent
    en = (root / "README.md").read_text()
    ko = (root / "README.ko.md").read_text()
    assert en.index("## What it fits") < en.index("## What it is not")
    assert ko.index("## 맞는 일") < ko.index("## 무엇이 아닌가")
    for words in (["A number per attempt", "Re-runnable", "One space, explored together", "code implementation"],
                  ["시도마다 숫자 지표", "재실행 가능", "같은 공간의 동시 탐색", "코드 구현"]):
        text = en if words[0].isascii() else ko
        assert all(w in text for w in words), words


def test_benchmark_tables_are_filled_with_a_gate_verdict() -> None:
    root = Path(__file__).resolve().parent.parent
    text = (root / "docs" / "results.md").read_text()
    section = text[text.index("## Benchmark"):]
    section = section[:section.index("\n## ", 1)]
    assert "_tbd_" not in section and "Release gate" in section
    assert "bench/results/" in section and section.count("**") >= 6
    report = re.search(r"(bench/results/\S+/report\.md)", section)
    assert report and (root / report.group(1)).is_file()
    for name in ("README.md", "README.ko.md"):
        assert "docs/results.md" in (root / name).read_text(), name


def test_results_link_every_boundary_pilot() -> None:
    """Each published boundary pilot is linked from docs/results.md, so a later pilot cannot replace an earlier one."""
    root = Path(__file__).resolve().parent.parent
    pilots = sorted(p.name for p in (root / "bench" / "results").iterdir() if "boundary-pilot" in p.name)
    assert pilots
    text = (root / "docs" / "results.md").read_text()
    assert [p for p in pilots if f"bench/results/{p}/" not in text] == []


def test_worker_instructions_prefer_exp_run_and_keep_the_mcp_pair() -> None:
    """Workers with a shell check, run and record in one command; the MCP tools stay as the fallback."""
    root = Path(__file__).resolve().parent.parent
    plugin = (root / "integrations/claude-code-mcp/agents/benchbook-worker.md").read_text()
    manual = (root / "integrations/claude-code/manual/agents/benchbook-worker.md").read_text()
    body = plugin.split("\n---\n", 1)[1]
    assert manual.split("\n---\n", 1)[1] == body  # the two installs differ only in tool names
    assert "benchbook --agent <name> exp run --config '<json>' --seed <n> --code-ref <ref> -- " in body
    assert "benchbook: not run: duplicate of" in body and "benchbook: recorded e:" in body
    assert body.index("exp run") < body.index("Only when you cannot") < body.index("benchbook_check_tried")
    for prefix, text in (("mcp__plugin_benchbook-mcp_benchbook__", plugin), ("mcp__benchbook__", manual)):
        tools = text.split("\ntools: ", 1)[1].split("\n", 1)[0]
        assert "Bash" in tools and f"{prefix}benchbook_record_experiment" in tools
    for rel in ("integrations/claude-code-mcp/skills/benchbook/SKILL.md", "integrations/agents-md/AGENTS.md",
                "docs/claude-code.md"):
        text = (root / rel).read_text()
        assert "exp run --config '<json>' --seed <n>" in text, rel
        assert text.index("exp run") < text.index("benchbook_record_experiment"), rel
    assert "## Check, run and record in one command" in (root / "docs/sdk.md").read_text()
