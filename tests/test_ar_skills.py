"""PR 2-R1: the run records of the autoresearch skills become experiments, and `benchbook ar gate on`.

The fixtures under tests/fixtures/autoresearch/skills/ are in each skill's real format (see the README there for
the upstream commits). Their commit hashes are placeholders that the integration tests swap for real commits.
"""
from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

import pytest

from benchbook import Ledger
from benchbook.errors import BenchbookError
from benchbook.integrations.autoresearch import gate, ops, skills
from tests.test_ar_ops import checkout, ctx, git, set_knob, sim_env  # noqa: F401  (sim_env: autouse fixture)

FIX = Path(__file__).resolve().parent / "fixtures" / "autoresearch" / "skills"
RUN_STATUSES = {"ok", "error", "timeout", "invalid"}
OUTCOMES = {"improved", "no_change", "worse", "failed", "unscored"}  # the CHECK constraint in 0001_core.sql


def head(root: Path, n: int = 7) -> str:
    return git(root, "rev-parse", "HEAD").strip()[:n]


def history(root: Path) -> dict[str, str]:
    """Three commits as the fixtures name them: baseline, MATRIX_LR 0.06, WEIGHT_DECAY 0.1."""
    shas = {"aaa1111": head(root)}
    set_knob(root, "MATRIX_LR", "0.06")
    shas["bbb2222"] = head(root)
    set_knob(root, "WEIGHT_DECAY", "0.1")
    shas["ccc3333"] = head(root)
    return shas


def fixture_with(src: Path, dest: Path, shas: dict[str, str]) -> Path:
    text = src.read_text()
    for placeholder, sha in shas.items():
        text = text.replace(placeholder, sha)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(text)
    return dest


def imported(lg: Ledger) -> list[dict[str, Any]]:
    return lg.sql_internal(
        "SELECT id, author, seed, run_status, outcome, primary_value, code_ref, config, trace_ref, notes"
        " FROM benchbook.experiments WHERE project_id=%s ORDER BY created_at, id", (lg.project_id,))


@pytest.fixture
def lab(tmp_path: Path) -> tuple[str, Path]:
    pid = f"sk-{uuid.uuid4().hex[:10]}"
    root = checkout(tmp_path, "a")
    ops.init(pid, start=root)
    return pid, root


# ------------------------------------------------------------------------------------------ parsing, per adapter

def test_pi_log_rows() -> None:
    log = skills.read_pi(FIX / "pi-autoresearch" / "log.jsonl")
    rows = log.rows
    assert [r.label for r in rows] == [f"run {n}" for n in range(1, 9)]  # hook entries are not runs
    assert [(r.status, r.run_status, r.kept) for r in rows[:5]] == [
        ("keep", "ok", True), ("keep", "ok", True), ("discard", "ok", False), ("crash", "error", False),
        ("checks_failed", "invalid", False)]
    # only a keep is committed; for the others `commit` is the HEAD the reverted change started from
    assert rows[1].commit == "bbb2222" and rows[2].commit is None and rows[2].parent_commit == "bbb2222"
    assert rows[3].value is None  # pi logs 0 for a crash; that is not a measurement
    assert rows[6].metrics == {"peak_vram_mb": 45060.2, "training_seconds": 612.4}
    assert {(r.metric, r.direction) for r in rows[:7]} == {("val_bpb", "min")}
    assert (rows[7].metric, rows[7].direction) == ("peak_vram_mb", "min")  # a second segment, another metric
    assert len({r.key for r in rows}) == len(rows) and skills.read_pi(FIX / "pi-autoresearch" / "log.jsonl") == log


def test_uditgoenka_tsv_rows() -> None:
    log = skills.read_uditgoenka(FIX / "uditgoenka" / "results.tsv")
    got = [(r.label, r.status, r.run_status, r.kept, r.commit) for r in log.rows]
    assert got == [
        ("iteration 0", "baseline", "ok", True, "aaa1111"),
        ("iteration 1", "keep", "ok", True, "bbb2222"),
        ("iteration 2", "discard", "ok", False, None),
        ("iteration 3", "crash", "error", False, None),
        ("iteration 5", "discard", "invalid", False, None),  # improved, but the guard failed
        ("iteration 6", "keep (reworked)", "ok", True, "ccc3333"),
        ("iteration 7", "metric-error", "error", False, None)]
    assert log.skipped == 2  # no-op and hook-blocked measured nothing
    assert {r.direction for r in log.rows} == {"min"} and {r.metric for r in log.rows} == {None}
    assert log.rows[2].parent_commit == "bbb2222" and log.rows[6].parent_commit == "ccc3333"


def test_uditgoenka_refuses_other_subcommands_tsv(tmp_path: Path) -> None:
    """/autoresearch:fix writes a TSV with a metric and a status too, but its metric is an error count."""
    fix = tmp_path / "fix-results.tsv"
    fix.write_text("# metric_direction: lower_is_better\niteration\ttimestamp\terror_type\terror_fixed\tcommit\t"
                   "metric\tdelta\tguard\tstatus\tdescription\n1\t-\tTS2322\ttrue\tabc1234\t3\t-1\tpass\tkeep\tx\n")
    with pytest.raises(BenchbookError, match="error_type"):
        skills.read_uditgoenka(fix)


def test_codex_events_rows() -> None:
    log = skills.read_codex(FIX / "codex-autoresearch" / "events.jsonl")
    got = [(r.label, r.status, r.run_status, r.kept, r.value) for r in log.rows]
    assert got == [
        ("baseline", "baseline", "ok", True, 1.0021),
        ("iteration 1", "keep", "ok", True, 0.9981),
        ("iteration 2", "discard", "ok", False, 1.0004),
        ("iteration 3", "crash", "error", False, None),
        ("iteration 4", "discard", "invalid", False, 0.9979),  # an improvement discarded because the guard failed
        ("iteration 5", "keep", "ok", True, 0.995)]
    # codex commits every trial before measuring it and reverts with git revert: discards keep their code
    assert log.rows[2].commit == "3" * 40 and log.rows[0].commit == "1" * 40
    assert {(r.metric, r.direction) for r in log.rows} == {("val_bpb", "min")}


def test_codex_needs_its_run_file(tmp_path: Path) -> None:
    (tmp_path / "events.jsonl").write_text((FIX / "codex-autoresearch" / "events.jsonl").read_text())
    with pytest.raises(BenchbookError, match=r"run\.json is missing"):
        skills.read_codex(tmp_path / "events.jsonl")
    run = json.loads((FIX / "codex-autoresearch" / "run.json").read_text())
    (tmp_path / "run.json").write_text(json.dumps({**run, "schema_version": 1}))
    with pytest.raises(BenchbookError, match="schema_version"):
        skills.read_codex(tmp_path / "events.jsonl")


def test_locate_finds_the_log_as_the_skill_does(tmp_path: Path) -> None:
    (tmp_path / ".auto").mkdir()
    (tmp_path / ".auto" / "log.jsonl").write_text("")
    assert skills.locate("pi-autoresearch", tmp_path) == tmp_path / ".auto" / "log.jsonl"
    (tmp_path / "autoresearch.jsonl").write_text("")  # legacy flat layout loses to .auto/
    assert skills.locate("pi-autoresearch", tmp_path) == tmp_path / ".auto" / "log.jsonl"
    (tmp_path / "autoresearch-results").mkdir()
    (tmp_path / "autoresearch-results" / "events.jsonl").write_text("")
    assert skills.locate("codex-autoresearch", tmp_path) == tmp_path / "autoresearch-results" / "events.jsonl"
    loop = tmp_path / "autoresearch" / "loop-261121-0900"
    loop.mkdir(parents=True)
    (loop / "a-results.tsv").write_text("")
    assert skills.locate("uditgoenka", loop) == loop / "a-results.tsv"
    (loop / "b-results.tsv").write_text("")
    with pytest.raises(BenchbookError, match="2 .tsv files"):
        skills.locate("uditgoenka", loop)


# ------------------------------------------------------------------------------------------ import into the ledger

def test_import_pi_is_idempotent_and_never_a_replication(lab: tuple[str, Path]) -> None:
    pid, root = lab
    shas = history(root)
    log = fixture_with(FIX / "pi-autoresearch" / "log.jsonl", root / ".auto" / "log.jsonl", shas)
    a = ctx(pid, root, "a")
    r = ops.import_results(a, root, hw="test-cpu", source="pi-autoresearch")  # the directory is enough
    assert {k: r[k] for k in ("rows", "imported", "duplicates", "missing_commit", "other_metric", "skipped")} == {
        "rows": 8, "imported": 6, "duplicates": 1, "missing_commit": 0, "other_metric": 1, "skipped": 0}
    exps = imported(a.lg)
    assert len(exps) == 6
    assert {e["run_status"] for e in exps} <= RUN_STATUSES and {e["outcome"] for e in exps} <= OUTCOMES
    assert [(e["trace_ref"]["status"], e["run_status"]) for e in exps] == [
        ("keep", "ok"), ("keep", "ok"), ("discard", "invalid"), ("crash", "error"), ("checks_failed", "invalid"),
        ("keep", "invalid")]
    # run 6 is pi re-running the code of run 2 by itself: it is not recorded, so it cannot pass for a re-run
    assert {e["seed"] for e in exps} == {0}
    assert sum(1 for e in exps if e["trace_ref"].get("commit") == shas["bbb2222"]) == 1
    # the evaluator version (prepare.py hash) is part of the identity: it names the partition
    assert {e["trace_ref"]["evaluator"] for e in exps} == {a.partition.rsplit("/", 1)[-1]}
    assert {e["config"]["partition"] for e in exps} == {a.partition}
    # a reverted discard is recorded with its value and description, but without code it is not a result
    assert exps[2]["code_ref"] is None and exps[2]["primary_value"] is None and exps[2]["outcome"] == "failed"
    assert "reverted this change" in exps[2]["notes"]

    again = ops.import_results(a, log, hw="test-cpu", source="pi-autoresearch")
    assert again["imported"] == 0 and again["duplicates"] == 7
    # another author importing the same log adds nothing either: an import cannot become an independent run
    b = ctx(pid, root, "b")
    assert ops.import_results(b, log, hw="test-cpu", source="pi-autoresearch")["imported"] == 0
    assert len(imported(a.lg)) == 6
    assert not a.lg.sql_internal("SELECT 1 FROM benchbook.v_claim_status WHERE project_id=%s AND status='verified'",
                                 (pid,))


def test_imported_runs_get_the_time_budget_check(lab: tuple[str, Path]) -> None:
    """check_budget runs on what the log holds: pi's run 7 reports 612 s of training against a 3 s budget."""
    pid, root = lab
    log = fixture_with(FIX / "pi-autoresearch" / "log.jsonl", root / "log.jsonl", history(root))
    a = ctx(pid, root, "a")
    ops.import_results(a, log, hw="test-cpu", source="pi-autoresearch")
    run7 = [e for e in imported(a.lg) if e["trace_ref"].get("run") == 7][0]
    assert run7["run_status"] == "invalid" and run7["outcome"] == "failed"
    assert "over the time budget" in run7["notes"] and "training_seconds 612.4" in run7["notes"]


def test_imported_keeps_are_candidates_for_others_to_rerun(lab: tuple[str, Path]) -> None:
    """The gate applies to imports: the baseline is calibrated and the keep replicated by agents other than the
    importer, through the referee's existing tasks."""
    pid, root = lab
    shas = history(root)
    tsv = fixture_with(FIX / "uditgoenka" / "results.tsv", root / "autoresearch" / "loop-1" / "x-results.tsv", shas)
    a = ctx(pid, root, "a")
    r = ops.import_results(a, tsv.parent, hw="test-cpu", source="uditgoenka")
    assert (r["rows"], r["imported"], r["skipped"]) == (7, 7, 2)
    exps = {e["trace_ref"]["iteration"]: e for e in imported(a.lg)}
    assert exps["0"]["outcome"] == "improved" and exps["1"]["outcome"] == "improved"
    tasks = a.lg.sql_internal("SELECT tags, spec FROM benchbook.tasks WHERE project_id=%s ORDER BY created_at",
                              (pid,))
    by_origin = {(t["tags"][0], t["spec"]["origin"]) for t in tasks}
    assert ("calibration", str(exps["0"]["id"])) in by_origin and ("replicate", str(exps["1"]["id"])) in by_origin
    assert all(t["spec"]["exclude_agents"] == ["a"] for t in tasks)
    assert ops.import_results(a, tsv, hw="test-cpu", source="uditgoenka")["imported"] == 0


def test_import_codex_records_the_code_of_every_trial(lab: tuple[str, Path]) -> None:
    pid, root = lab
    full = lambda: git(root, "rev-parse", "HEAD").strip()  # noqa: E731
    shas = {"1" * 40: full()}
    set_knob(root, "MATRIX_LR", "0.06")
    shas["2" * 40] = full()
    for trial, revert, knob in (("3", "a", ("DEPTH", "10")), ("4", "b", ("DEPTH", "14")),
                                ("5", "c", ("WARMDOWN_RATIO", "0.4"))):
        set_knob(root, *knob)
        shas[trial * 40] = full()
        git(root, "revert", "--no-edit", "HEAD")
        shas[revert * 40] = full()
    set_knob(root, "WEIGHT_DECAY", "0.1")
    shas["6" * 40] = full()
    out = root / "autoresearch-results"
    fixture_with(FIX / "codex-autoresearch" / "events.jsonl", out / "events.jsonl", shas)
    fixture_with(FIX / "codex-autoresearch" / "run.json", out / "run.json", shas)
    a = ctx(pid, root, "a")
    r = ops.import_results(a, out, hw="test-cpu", source="codex-autoresearch")
    assert (r["rows"], r["imported"], r["missing_commit"]) == (6, 6, 0)
    exps = imported(a.lg)
    assert all(e["code_ref"] for e in exps)  # discards and crashes too: their trial commits stay in history
    assert [(e["trace_ref"]["status"], e["run_status"]) for e in exps] == [
        ("baseline", "ok"), ("keep", "ok"), ("discard", "ok"), ("crash", "error"), ("discard", "invalid"),
        ("keep", "ok")]
    assert exps[2]["config"]["changed"] == {"DEPTH": [8, 10]}
    assert ops.import_results(a, out, hw="test-cpu", source="codex-autoresearch")["imported"] == 0


def test_import_refuses_a_log_of_another_metric(lab: tuple[str, Path]) -> None:
    pid, root = lab
    shas = history(root)
    out = root / "autoresearch-results"
    fixture_with(FIX / "codex-autoresearch" / "events.jsonl", out / "events.jsonl", shas)
    run = json.loads((FIX / "codex-autoresearch" / "run.json").read_text())
    (out / "run.json").write_text(json.dumps({**run, "metric": {**run["metric"], "name": "tokens_per_s",
                                                                 "direction": "higher"}}))
    a = ctx(pid, root, "a")
    with pytest.raises(BenchbookError, match="tokens_per_s"):
        ops.import_results(a, out, hw="test-cpu", source="codex-autoresearch")
    assert imported(a.lg) == []


def test_a_reverted_change_never_becomes_the_best(lab: tuple[str, Path]) -> None:
    """A run whose code the skill reverted cannot be re-run, so it is recorded but compared with nothing: it is
    neither a candidate nor the value later runs must beat."""
    pid, root = lab
    base = head(root)
    tsv = root / "r.tsv"
    tsv.write_text("# metric_direction: lower_is_better\n"
                   "iteration\ttimestamp\tcommit\tmetric\tdelta\tguard\tguard-metric\tstatus\tdescription\n"
                   "1\t-\t-\t0.9000\t-\tpass\t-\tdiscard\tlost change\n"
                   f"0\t-\t{base}\t1.0000\t0\tpass\t-\tbaseline\tb\n")
    a = ctx(pid, root, "a")
    ops.import_results(a, tsv, hw="test-cpu", source="uditgoenka")
    lost, baseline = imported(a.lg)
    assert lost["code_ref"] is None and lost["run_status"] == "invalid" and lost["outcome"] == "failed"
    assert baseline["outcome"] == "improved"  # compared with nothing, not with the 0.9 nobody can reproduce
    assert a.lg.baseline(a.partition)[0] == 1.0
    tasks = a.lg.sql_internal("SELECT spec FROM benchbook.tasks WHERE project_id=%s", (pid,))
    assert {t["spec"]["origin"] for t in tasks} == {str(baseline["id"])}


def test_import_refuses_while_holding_a_task(lab: tuple[str, Path]) -> None:
    pid, root = lab
    log = fixture_with(FIX / "pi-autoresearch" / "log.jsonl", root / "log.jsonl", history(root))
    Ledger.open(project=pid, agent="referee").create_tasks([{"title": "t", "tags": ["replicate"], "spec": {}}])
    a = ctx(pid, root, "a")
    assert a.lg.next_task() is not None
    with pytest.raises(BenchbookError, match="live lease"):
        ops.import_results(a, log, hw="test-cpu", source="pi-autoresearch")


# ------------------------------------------------------------------------------------------ ar gate on

def test_gate_on_restores_the_gate_and_is_idempotent(lab: tuple[str, Path]) -> None:
    pid, root = lab
    human = Ledger.open(project=pid, agent="human")
    human.update_settings({"outcome_baseline": "best", "replicates": 0, "rerun_min_authors": 1,
                           "rerun_min_seeds": 1})
    r = gate.gate_on(pid, start=root)
    assert r["changed"] == {"outcome_baseline": "verified", "replicates": 2, "rerun_min_authors": 2,
                            "rerun_min_seeds": 3}
    assert {k: human.refresh().settings[k] for k in r["changed"]} == r["changed"]
    assert gate.gate_on(pid, start=root)["changed"] == {}
    human.update_settings({"replicates": 5, "rerun_min_authors": 3})  # stricter values stay
    assert gate.gate_on(pid, start=root)["changed"] == {}


def test_gate_on_creates_the_project_and_writes_the_fragment(tmp_path: Path) -> None:
    root = checkout(tmp_path, "x")
    pid = f"sk-{uuid.uuid4().hex[:10]}"
    r = gate.gate_on(pid, start=root, skill="pi-autoresearch", hw="test-cpu")
    assert r["created"] and r["wrote_fragment"]
    assert not (root / "program.benchbook.md").exists()  # the skill keeps its own loop
    frag = (root / "program.benchbook.pi-autoresearch.md").read_text()
    assert "benchbook ar import --from pi-autoresearch .auto/log.jsonl --hw test-cpu" in frag
    assert r["import"] == "benchbook ar import --from pi-autoresearch .auto/log.jsonl --hw test-cpu"
    settings = Ledger.open(project=pid, agent="human").project.settings
    assert settings["outcome_baseline"] == "verified" and settings["replicates"] == 2
    assert r["time_budget_s"] == 3.0  # check_budget is on: TIME_BUDGET of the checkout's prepare.py
    for skill in skills.SKILLS:
        text = skills.fragment(skill, "h100-80gb")
        assert f"benchbook ar import --from {skill}" in text and "{hw}" not in text


def test_gate_cli(lab: tuple[str, Path]) -> None:
    import os
    import subprocess
    import sys

    pid, root = lab
    Ledger.open(project=pid, agent="human").update_settings({"replicates": 0})
    env = {**os.environ, "BENCHBOOK_PROJECT": pid}
    r = subprocess.run([sys.executable, "-m", "benchbook.cli", "--json", "ar", "gate", "on", "--from", "uditgoenka"],
                       cwd=root, capture_output=True, text=True, env=env, timeout=120)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert out["changed"] == {"replicates": 2} and out["project"] == pid
    assert (root / "program.benchbook.uditgoenka.md").is_file()
    r = subprocess.run([sys.executable, "-m", "benchbook.cli", "ar", "gate", "on"], cwd=root, capture_output=True,
                       text=True, env=env, timeout=120)
    assert r.returncode == 0 and "nothing, the gate was already on" in r.stdout, r.stdout + r.stderr
    loop = root / "autoresearch" / "loop-1"
    fixture_with(FIX / "uditgoenka" / "results.tsv", loop / "x-results.tsv", history(root))
    cmd = [sys.executable, "-m", "benchbook.cli", "--agent", "a", "ar", "import", "--from", "uditgoenka",
           str(loop), "--hw", "test-cpu"]
    r = subprocess.run(cmd, cwd=root, capture_output=True, text=True, env=env, timeout=120)
    assert r.returncode == 0 and "imported 7 of 7 rows" in r.stdout and "2 not runs" in r.stdout, r.stdout + r.stderr
    r = subprocess.run(cmd, cwd=root, capture_output=True, text=True, env=env, timeout=120)
    assert r.returncode == 0 and "imported 0 of 7 rows (7 already recorded" in r.stdout, r.stdout + r.stderr
