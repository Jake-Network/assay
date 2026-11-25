"""benchbook check verify (PR 1-1): declared checks re-run on a reported commit in a clean worktree, recorded as a
completion claim (the implementer's) and an attempt (the verifier's) linked supports/refutes."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

import pytest

from benchbook import Ledger, checks, config
from benchbook.errors import BenchbookError
from benchbook.packs import load_pack
from tests.conftest import CONFIG, TEST_EXTRA, TEST_IMPL, git, make_project, make_repo, piece

METRIC_KEYS = {"checks_passed", "checks_total", "checks", "checks_declared", "failure_signature", "guard_touched",
               "guard_files", "checks_changed", "guard_weakened"}
DOC = Path(__file__).resolve().parent.parent / "docs" / "code.md"


def assert_clean(repo: Path, vtmp: Path) -> None:
    """No worktree directory and no git worktree metadata left behind."""
    listed = [x for x in git(repo, "worktree", "list", "--porcelain").splitlines() if x.startswith("worktree ")]
    assert listed == [f"worktree {repo}"]
    meta = repo / ".git" / "worktrees"
    assert not meta.exists() or not any(meta.iterdir())
    assert list(vtmp.iterdir()) == []


@pytest.fixture
def vtmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Where verify makes its temporary worktree, so a test can see that nothing is left."""
    d = tmp_path / "vtmp"
    d.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(d))
    return d


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return make_repo(tmp_path / "repo")


@pytest.fixture
def code_project(db_url: str) -> str:
    return make_project(db_url, metric="checks_passed", direction="max", pack="code",
                        settings={"partition_key": "piece"})


@pytest.fixture
def verifier(db_url: str, code_project: str) -> Ledger:
    return Ledger.open(project=code_project, agent="verifier@test", role="harness", database_url=db_url)


def row(lg: Ledger, table: str, id: Any) -> dict[str, Any]:
    return lg.sql_internal(f"SELECT * FROM benchbook.{table} WHERE id = %s", (id,))[0]


def links_to(lg: Ledger, claim_id: Any) -> list[dict[str, Any]]:
    return lg.sql_internal("SELECT * FROM benchbook.links WHERE dst_id = %s ORDER BY created_at", (claim_id,))


# ------------------------------------------------------------------------------------------- recording

def test_passing_commit_supports_the_completion(repo: Path, vtmp: Path, verifier: Ledger) -> None:
    sha = piece(repo, "good", {"impl.txt": "fixed\n"})
    v, rec = checks.verify(verifier, "media", sha[:8], repo=repo, reported_by="impl")
    assert v.all_passed and [(r.name, r.status) for r in v.results] == [("unit", "ok"), ("value", "ok")]
    assert rec.relation == "supports" and rec.verifier == "verifier@test" and rec.reported_by == "impl"
    exp = row(verifier, "experiments", rec.experiment_id)
    assert exp["author"] == "verifier@test" and exp["code_ref"] == sha and exp["run_status"] == "ok"
    assert exp["config"] == {"piece": "media", "checks": ["unit", "value"]}
    assert set(exp["metrics"]) == METRIC_KEYS
    assert exp["metrics"] == {
        "checks_passed": 2, "checks_total": 2, "checks": {"unit": "ok", "value": "ok"},
        "checks_declared": ["unit", "value"], "failure_signature": None,
        "guard_touched": False, "guard_files": [], "checks_changed": False, "guard_weakened": False}
    claim = row(verifier, "claims", rec.claim_id)
    assert claim["author"] == "impl" and claim["kind"] == "completion"  # two different agents in the ledger
    assert claim["payload"] == {"piece": "media", "commit": sha, "reported_checks": [], "paths": ["impl.txt"],
                                "files": ["impl.txt"]}
    assert set(claim["tags"]) == {"piece:media", f"commit:{sha}", "file:impl.txt"}
    [link] = links_to(verifier, rec.claim_id)
    assert (link["src_id"], link["relation"], link["author"]) == (rec.experiment_id, "supports", "verifier@test")
    assert verifier.claim_status(rec.claim_id) == "proposed"  # no decision in this step
    assert not verifier.sql_internal("SELECT 1 FROM benchbook.decisions WHERE project_id = %s",
                                     (verifier.project_id,))
    roles = {r["id"]: r["role"] for r in verifier.sql_internal(
        "SELECT id, role FROM benchbook.agents WHERE project_id = %s", (verifier.project_id,))}
    assert roles["impl"] == "worker" and roles["verifier@test"] == "harness"
    assert_clean(repo, vtmp)


def test_failing_check_refutes_and_names_the_failing_test(repo: Path, vtmp: Path, verifier: Ledger) -> None:
    sha = piece(repo, "bad", {"impl.txt": "almost\n"})
    v, rec = checks.verify(verifier, "media", sha, repo=repo, reported_by="impl")
    unit = v.results[0]
    assert unit.status == "fail" and unit.exit_code == 1
    assert unit.failed_tests == [("tests.test_impl::test_fixed", "AssertionError")]  # from the JUnit XML
    assert "OUTPUT-MARKER-7f3a" in unit.tail  # shown to the person running verify ...
    assert not v.all_passed and rec.relation == "refutes"
    exp = row(verifier, "experiments", rec.experiment_id)
    assert exp["run_status"] == "ok"  # the checks ran to the end: a failing check is a result, not a failed run
    assert exp["metrics"]["checks"] == {"unit": "fail", "value": "ok"} and exp["metrics"]["checks_passed"] == 1
    assert exp["metrics"]["failure_signature"] == "unit:tests.test_impl::test_fixed:AssertionError"
    stored = json.dumps(verifier.sql_internal(
        "SELECT e.*, c.payload, c.statement FROM benchbook.experiments e, benchbook.claims c"
        " WHERE e.id = %s AND c.id = %s", (rec.experiment_id, rec.claim_id)), default=str)
    assert "OUTPUT-MARKER-7f3a" not in stored and "almost" not in stored  # ... never stored (spec §15)
    assert [x["relation"] for x in links_to(verifier, rec.claim_id)] == ["refutes"]
    assert_clean(repo, vtmp)


def test_failure_without_junit_is_signed_by_exit_code_and_last_line_hash(repo: Path, vtmp: Path,
                                                                          verifier: Ledger) -> None:
    sha = piece(repo, "value", {"value.txt": "nope\n", "impl.txt": "fixed\n"})
    v, rec = checks.verify(verifier, "media", sha, repo=repo, only=["value"])
    assert [r.name for r in v.results] == ["value"]
    sig = row(verifier, "experiments", rec.experiment_id)["metrics"]["failure_signature"]
    assert re.fullmatch(r"value:exit 1:(-|[0-9a-f]{16})", sig)
    assert rec.claim_id is None and rec.relation is None  # no --reported-by: an attempt without a claim


def test_reverifying_a_commit_is_recorded_and_points_to_the_first(repo: Path, vtmp: Path, verifier: Ledger) -> None:
    sha = piece(repo, "good", {"impl.txt": "fixed\n"})
    _, first = checks.verify(verifier, "media", sha, repo=repo, reported_by="impl")
    _, again = checks.verify(verifier, "media", sha, repo=repo, reported_by="impl")
    assert first.duplicate_of is None and again.duplicate_of == first.experiment_id and again.notice
    assert again.claim_id == first.claim_id and again.claim_existed
    assert [x["src_id"] for x in links_to(verifier, first.claim_id)] == [first.experiment_id, again.experiment_id]


def test_report_file_keeps_a_hash_and_the_check_names_it_mentions(repo: Path, tmp_path: Path, vtmp: Path,
                                                                 verifier: Ledger) -> None:
    sha = piece(repo, "good", {"impl.txt": "fixed\n"})
    report = tmp_path / "report.txt"
    report.write_text("Done. I ran unit and it passed; units are fine.\nAPI_TOKEN=sk-very-secret\nDONE " + sha)
    _, rec = checks.verify(verifier, "media", sha, repo=repo, reported_by="impl", report_file=report)
    payload = row(verifier, "claims", rec.claim_id)["payload"]
    assert payload == {"piece": "media", "commit": sha, "reported_checks": ["unit"], "paths": ["impl.txt"],
                       "files": ["impl.txt"], "report": {"sha256": hashlib.sha256(report.read_bytes()).hexdigest(),
                                  "bytes": len(report.read_bytes())}}
    with pytest.raises(BenchbookError, match="report-file"):
        checks.verify(verifier, "media", sha, repo=repo, reported_by="impl", report_file=tmp_path / "missing")


def test_reporter_and_verifier_must_be_different_agents(repo: Path, vtmp: Path, verifier: Ledger) -> None:
    sha = piece(repo, "good", {"impl.txt": "fixed\n"})
    with pytest.raises(BenchbookError, match="cannot verify its own report"):
        checks.verify(verifier, "media", sha, repo=repo, reported_by="verifier@test")
    assert not verifier.sql_internal("SELECT 1 FROM benchbook.experiments WHERE project_id = %s",
                                     (verifier.project_id,))
    assert list(vtmp.iterdir()) == []  # refused before anything ran


def test_usage_errors(repo: Path, tmp_path: Path, vtmp: Path, verifier: Ledger, db_url: str) -> None:
    sha = piece(repo, "good", {"impl.txt": "fixed\n"})
    with pytest.raises(BenchbookError, match="nope not declared"):
        checks.verify(verifier, "media", sha, repo=repo, only=["nope"])
    with pytest.raises(BenchbookError, match="not a commit"):
        checks.verify(verifier, "media", "no-such-ref", repo=repo)
    with pytest.raises(BenchbookError, match="not a commit"):
        checks.verify(verifier, "media", sha, repo=repo, base="origin/nowhere")
    with pytest.raises(BenchbookError, match="piece"):
        checks.verify(verifier, "two words", sha, repo=repo)
    core = Ledger.open(project=make_project(db_url), agent="v", database_url=db_url)
    with pytest.raises(BenchbookError, match="code pack"):
        checks.verify(core, "media", sha, repo=repo)
    bare = make_repo(tmp_path / "bare", 'project = "x"\n[guard]\nbase = "main"\n')
    with pytest.raises(BenchbookError, match=r"declares no \[checks"):
        checks.verify(verifier, "media", git(bare, "rev-parse", "HEAD"), repo=bare)


# ------------------------------------------------------------------------------------------- integrity

def test_a_commit_that_deletes_a_test_shows_in_guard_files(repo: Path, vtmp: Path, verifier: Ledger) -> None:
    sha = piece(repo, "drop-test", {"impl.txt": "fixed\n", "tests/test_extra.py": None})
    v, rec = checks.verify(verifier, "media", sha, repo=repo, reported_by="impl")
    assert v.all_passed and rec.relation == "supports"  # the re-run passes ...
    m = row(verifier, "experiments", rec.experiment_id)["metrics"]
    assert m["guard_touched"] is True and m["guard_files"] == ["tests/test_extra.py"]  # ... and the deletion shows
    assert m["checks_changed"] is False


def test_a_test_moved_out_of_the_guard_shows_under_its_old_path(repo: Path, vtmp: Path, verifier: Ledger) -> None:
    sha = piece(repo, "move-test", {"impl.txt": "fixed\n", "tests/test_extra.py": None, "attic/extra.py": TEST_EXTRA})
    v, _ = checks.verify(verifier, "media", sha, repo=repo)
    assert v.integrity.guard_files == ["tests/test_extra.py"]  # git would report a rename to attic/ otherwise


def test_the_checks_that_run_are_the_merge_bases_not_the_commits(repo: Path, vtmp: Path, verifier: Ledger) -> None:
    weakened = CONFIG.replace('cmd = "grep -qx ok value.txt"', 'cmd = "true"')
    sha = piece(repo, "weaken", {"impl.txt": "fixed\n", "value.txt": "broken\n", ".benchbook/config.toml": weakened})
    v, rec = checks.verify(verifier, "media", sha, repo=repo, reported_by="impl")
    assert [(r.name, r.status) for r in v.results] == [("unit", "ok"), ("value", "fail")]  # the base's grep ran
    m = row(verifier, "experiments", rec.experiment_id)["metrics"]
    assert m["checks_changed"] is True and m["guard_files"] == [".benchbook/config.toml"]
    assert rec.relation == "refutes"


def test_a_check_added_by_the_commit_does_not_run(repo: Path, vtmp: Path, verifier: Ledger) -> None:
    added = CONFIG + '\n[checks.extra]\ncmd = "false"\n'
    sha = piece(repo, "add-check", {"impl.txt": "fixed\n", ".benchbook/config.toml": added})
    v, _ = checks.verify(verifier, "media", sha, repo=repo)
    assert [r.name for r in v.results] == ["unit", "value"] and v.integrity.checks_changed


def test_reformatting_config_is_not_a_checks_change(repo: Path, vtmp: Path, verifier: Ledger) -> None:
    sha = piece(repo, "comment", {"impl.txt": "fixed\n", ".benchbook/config.toml": "# reviewed\n" + CONFIG})
    v, _ = checks.verify(verifier, "media", sha, repo=repo)
    assert not v.integrity.checks_changed and v.integrity.guard_files == [".benchbook/config.toml"]


def test_guard_is_against_the_merge_base_not_the_base_tip(repo: Path, vtmp: Path, verifier: Ledger) -> None:
    sha = piece(repo, "good", {"impl.txt": "fixed\n"})
    (repo / "tests" / "test_later.py").write_text(TEST_EXTRA)  # main moves on after the piece branched
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "later")
    v, _ = checks.verify(verifier, "media", sha, repo=repo)
    assert v.integrity.guard_files == [] and v.base.merge_base != v.base.sha


# ------------------------------------------------------------------------------------------- the worktree

def test_junit_path_is_outside_the_worktree(tmp_path: Path, vtmp: Path, verifier: Ledger,
                                            monkeypatch: pytest.MonkeyPatch) -> None:
    rec_dir = tmp_path / "seen"
    rec_dir.mkdir()
    monkeypatch.setenv("SEEN", str(rec_dir))
    cfg = 'project = "x"\n[checks.where]\ncmd = \'pwd > "$SEEN/pwd"; printf %s {junit} > "$SEEN/junit"\'\n' \
          '[guard]\nbase = "main"\n'
    r = make_repo(tmp_path / "r", cfg)
    checks.verify(verifier, "p", git(r, "rev-parse", "HEAD"), repo=r)
    wt, junit = (rec_dir / "pwd").read_text().strip(), (rec_dir / "junit").read_text()
    assert junit.endswith("where.junit.xml") and not junit.startswith(wt + "/") and not Path(wt).exists()
    assert_clean(r, vtmp)


def test_timeout_is_a_failing_check_and_leaves_no_worktree(tmp_path: Path, vtmp: Path, verifier: Ledger) -> None:
    cfg = 'project = "x"\n[checks.slow]\ncmd = "sleep 30"\ntimeout = 1\n[checks.quick]\ncmd = "true"\n' \
          '[guard]\nbase = "main"\n'
    r = make_repo(tmp_path / "r", cfg)
    t0 = time.monotonic()
    v, rec = checks.verify(verifier, "p", git(r, "rev-parse", "HEAD"), repo=r, reported_by="impl")
    assert time.monotonic() - t0 < 20
    assert [(x.name, x.status) for x in v.results] == [("slow", "timeout"), ("quick", "ok")]
    m = row(verifier, "experiments", rec.experiment_id)
    assert m["run_status"] == "ok" and m["metrics"]["failure_signature"] == "slow:timeout"
    assert rec.relation == "refutes"
    assert_clean(r, vtmp)


def test_worktree_is_removed_when_the_run_raises(repo: Path, vtmp: Path, verifier: Ledger,
                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    sha = piece(repo, "good", {"impl.txt": "fixed\n"})

    def boom(*a: Any, **k: Any) -> Any:
        raise RuntimeError("runner exploded")

    monkeypatch.setattr(checks, "run_command", boom)
    with pytest.raises(RuntimeError, match="exploded"):
        checks.verify(verifier, "media", sha, repo=repo, reported_by="impl")
    assert_clean(repo, vtmp)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")
@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT])
def test_interrupted_verify_cleans_up_and_records_the_attempt(tmp_path: Path, db_url: str, code_project: str,
                                                              sig: signal.Signals) -> None:
    vt = tmp_path / "vt"
    vt.mkdir()
    cfg = 'project = "x"\n[checks.hang]\ncmd = \'touch "$MARK" && sleep 60\'\n[checks.after]\ncmd = "true"\n' \
          '[guard]\nbase = "main"\n'
    r = make_repo(tmp_path / "r", cfg)
    mark = tmp_path / "started"
    env = {**os.environ, "BENCHBOOK_DATABASE_URL": db_url, "TMPDIR": str(vt), "MARK": str(mark)}
    for k in ("BENCHBOOK_AGENT", "ASSAY_AGENT"):
        env.pop(k, None)
    proc = subprocess.Popen(
        [sys.executable, "-m", "benchbook.cli", "--project", code_project, "check", "verify", "p", "HEAD",
         "--reported-by", "impl"], cwd=r, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        preexec_fn=lambda: signal.signal(signal.SIGINT, signal.SIG_DFL))  # Python installs its Ctrl-C handler
    deadline = time.monotonic() + 60
    while not mark.exists() and time.monotonic() < deadline and proc.poll() is None:
        time.sleep(0.05)
    assert mark.exists(), proc.communicate()
    proc.send_signal(sig)
    out, err = proc.communicate(timeout=60)
    assert proc.returncode == 128 + sig, (out, err)
    assert "interrupted" in err
    assert_clean(r, vt)
    lg = Ledger.open(project=code_project, agent="reader", database_url=db_url)
    [exp] = lg.sql_internal("SELECT * FROM benchbook.experiments WHERE project_id = %s", (code_project,))
    assert exp["run_status"] == "error" and f"interrupted ({sig.name}) after 0 of 2" in exp["notes"]
    assert exp["metrics"]["checks"] == {"hang": "not_run", "after": "not_run"}
    [claim] = lg.sql_internal("SELECT id FROM benchbook.claims WHERE project_id = %s", (code_project,))
    assert links_to(lg, claim["id"]) == []  # an unfinished re-run neither supports nor refutes


# ------------------------------------------------------------------------------------------- the numbers

def test_aggregation_query_in_docs(tmp_path: Path, vtmp: Path, db_url: str, code_project: str,
                                   verifier: Ledger) -> None:
    """docs/code.md's one statement gives the false completion rate and the guarded-path count."""
    r = make_repo(tmp_path / "agg")
    weakened = CONFIG.replace('cmd = "grep -qx ok value.txt"', 'cmd = "true"')
    shas = {
        "a": piece(r, "a", {"impl.txt": "fixed\n"}),  # passes
        "b": piece(r, "b", {"impl.txt": "nearly\n"}),  # false completion
        "c": piece(r, "c", {"impl.txt": "fixed\n", "tests/test_extra.py": None}),  # passes, deleted a test
        "d": piece(r, "d", {"impl.txt": "fixed\n", "value.txt": "x\n", ".benchbook/config.toml": weakened}),
        "e": piece(r, "e", {"impl.txt": "fixed\n"}),  # verified without a report: no completion
    }
    for name in "abcd":
        checks.verify(verifier, f"piece-{name}", shas[name], repo=r, reported_by=f"impl-{name}")
    checks.verify(verifier, "piece-a", shas["a"], repo=r, reported_by="impl-a")  # re-verified: same completion
    checks.verify(verifier, "piece-e", shas["e"], repo=r)
    numbers = DOC.read_text().split("## The numbers", 1)[1].split("\n## ", 1)[0]  # other sections have SQL too
    [query] = re.findall(r"```sql\n(.*?)```", numbers, re.S)
    lead = Ledger.open(project=code_project, agent="lead", database_url=db_url)
    assert lead.sql(query)["rows"] == [{"completions": 4, "rerun": 4, "false_completions": 2,
                                         "false_completion_rate": 0.5, "touched_guard": 2, "changed_checks": 1}]


# ------------------------------------------------------------------------------------------- CLI

@pytest.fixture
def cli_env(db_url: str, vtmp: Path) -> dict[str, str]:
    env = {**os.environ, "BENCHBOOK_DATABASE_URL": db_url, "TMPDIR": str(vtmp)}
    for k in ("BENCHBOOK_PROJECT", "ASSAY_PROJECT", "BENCHBOOK_AGENT", "ASSAY_AGENT"):
        env.pop(k, None)
    return env


def run_cli(cwd: Path, env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-m", "benchbook.cli", *args], cwd=cwd, env=env, capture_output=True,
                          text=True, timeout=180)


def test_cli_verify_exit_codes_and_output(repo: Path, vtmp: Path, code_project: str,
                                          cli_env: dict[str, str]) -> None:
    good = piece(repo, "good", {"impl.txt": "fixed\n"})
    bad = piece(repo, "bad", {"impl.txt": "no\n"})
    p = run_cli(repo, cli_env, "--project", code_project, "check", "verify", "media", "good", "--reported-by",
                "impl", "--json")
    assert p.returncode == 0, p.stderr
    out = json.loads(p.stdout)
    assert out["ok"] is True and out["commit"] == good and out["relation"] == "supports"
    assert out["verifier"] == checks.default_verifier() and out["reported_by"] == "impl"
    assert out["experiment"].startswith("e:") and out["claim"].startswith("c:")
    p = run_cli(repo, cli_env, "--project", code_project, "check", "verify", "media", bad, "--reported-by", "impl")
    assert p.returncode == 1, p.stderr
    assert "result" in p.stdout and "unit" in p.stdout and "fail" in p.stdout
    assert "failing: tests.test_impl::test_fixed (AssertionError)" in p.stdout and "refutes c:" in p.stdout
    p = run_cli(repo, cli_env, "--project", code_project, "--agent", "impl", "check", "verify", "media", good,
                "--reported-by", "impl")
    assert p.returncode == 1 and "cannot verify its own report" in p.stderr
    assert_clean(repo, vtmp)


def test_init_pack_code(tmp_path: Path, db_url: str, cli_env: dict[str, str]) -> None:
    pid = f"code-{uuid.uuid4().hex[:6]}"
    p = run_cli(tmp_path, cli_env, "init", "--project", pid, "--pack", "code", "--json")
    assert p.returncode == 0, p.stderr
    proj = Ledger.open(project=pid, agent="human", database_url=db_url).project
    assert (proj.pack, proj.primary_metric, proj.metric_direction) == ("code", "checks_passed", "max")
    assert proj.settings == {"partition_key": "piece", "worker_intents": True}
    p = run_cli(tmp_path, cli_env, "init", "--project", f"{pid}-x", "--pack", "nope")
    assert p.returncode == 1 and "unknown pack" in p.stderr


# ------------------------------------------------------------------------------------------- config and pack

def test_local_config_reads_checks_and_guard(tmp_path: Path) -> None:
    (tmp_path / ".benchbook").mkdir()
    (tmp_path / ".benchbook" / "config.toml").write_text(CONFIG)
    cfg = config.load(tmp_path)
    assert list(cfg.checks) == ["unit", "value"]
    assert cfg.checks["unit"].junit is True and cfg.checks["unit"].timeout == 120
    assert cfg.checks["value"] == config.CheckSpec("value", "grep -qx ok value.txt", False, 900.0)
    assert cfg.guard_paths == ["tests/**", "**/conftest.py", ".benchbook/config.toml"] and cfg.guard_base == "main"


@pytest.mark.parametrize(("table", "message"), [
    ({"x": {"cmd": ""}}, "cmd must be"),
    ({"x": {"cmd": "true", "timout": 5}}, "unknown key timout"),
    ({"x": {"cmd": "true", "timeout": 0}}, "timeout must be"),
    ({"x": {"cmd": "true", "junit": "yes"}}, "junit must be"),
    ({"bad name": {"cmd": "true"}}, "check name"),
    ({"x": "true"}, "must be a table"),
])
def test_bad_check_declarations(table: dict[str, Any], message: str) -> None:
    with pytest.raises(BenchbookError, match=message):
        config.parse_checks(table)


def test_bad_guard() -> None:
    with pytest.raises(BenchbookError, match="paths"):
        config.parse_guard({"paths": "tests/**"})
    with pytest.raises(BenchbookError, match="base"):
        config.parse_guard({"base": ""})


def test_loading_declarations_imports_nothing_heavy(tmp_path: Path) -> None:
    """Hooks will match shell commands against [checks] at every Bash call: config must stay standard library."""
    (tmp_path / ".benchbook").mkdir()
    (tmp_path / ".benchbook" / "config.toml").write_text(CONFIG)
    code = ("import sys; from pathlib import Path; from benchbook import config; c = config.load(Path(sys.argv[1]));"
            "assert list(c.checks) == ['unit', 'value'];"
            "print(sorted(m for m in sys.modules if m.split('.')[0] in ('psycopg', 'typer', 'jsonschema', 'pydantic')"
            " or m.startswith(('benchbook.ledger', 'benchbook.db', 'benchbook.checks'))))")
    p = subprocess.run([sys.executable, "-c", code, str(tmp_path)], capture_output=True, text=True, check=True)
    assert p.stdout.strip() == "[]"


def test_code_pack() -> None:
    pack = load_pack("code")
    assert pack.claim_kinds == ["completion", "observation", "note"]
    assert pack.dedup_ignore == ["approach", "paths", "notes"] and pack.dedup_mode == "notify"
    assert pack.project_defaults == {"metric": "checks_passed", "direction": "max",
                                     "settings": {"partition_key": "piece", "worker_intents": True}}
    assert {"experiment.config", "experiment.metrics", "claim.completion"} <= set(pack.validators)
    assert load_pack("core").project_defaults == {}


# ------------------------------------------------------------------------------------------- seen before (2-P0c)

def test_verify_names_an_earlier_attempt_that_failed_the_same_way(repo: Path, vtmp: Path, verifier: Ledger) -> None:
    """check verify shows the same notice as check run, and its failed re-runs leave the same discarded-approach
    claim: one per piece, signature and approach, the second re-run added as evidence."""
    from benchbook.check_cli import render, summary

    a = piece(repo, "bad", {"impl.txt": "almost\n"})
    v1, r1 = checks.verify(verifier, "media", a, repo=repo, reported_by="impl")
    assert r1.seen_before == [] and "seen before" not in render(v1, r1)
    assert r1.discarded_id is not None and r1.discarded_new
    b = piece(repo, "bad2", {"impl.txt": "nearly\n"})  # another commit, the same failing test
    v2, r2 = checks.verify(verifier, "media", b, repo=repo, reported_by="impl")
    assert [p.id for p in r2.seen_before] == [r1.experiment_id]
    text = render(v2, r2)
    assert f"seen before: piece media failed the same way in e:{str(r1.experiment_id)[:8]} (verifier@test" in text
    assert summary(v2, r2)["seen_before"] == [f"e:{str(r1.experiment_id)[:8]}"]
    assert r2.discarded_id == r1.discarded_id and not r2.discarded_new
    [c] = verifier.sql_internal("SELECT c.tags, c.payload, s.n_support FROM benchbook.claims c JOIN"
                                " benchbook.v_claim_status s ON s.id = c.id WHERE c.id = %s", (r1.discarded_id,))
    assert c["tags"] == ["discarded", "piece:media", "file:impl.txt"] and c["n_support"] == 2
    assert c["payload"]["signature"] == "unit:tests.test_impl::test_fixed:AssertionError"
    # another piece failing the same test was not seen before on this one
    v3, r3 = checks.verify(verifier, "upload", b, repo=repo)
    assert r3.seen_before == [] and r3.discarded_id != r1.discarded_id
    # a passing re-run leaves no discarded approach and no notice
    good = piece(repo, "good", {"impl.txt": "fixed\n"})
    v4, r4 = checks.verify(verifier, "media", good, repo=repo, reported_by="impl")
    assert v4.all_passed and r4.seen_before == [] and r4.discarded_id is None


# ------------------------------------------------------------------------------------ checks pinned in the ledger

def make_pinned_repo(root: Path, cfg: str = CONFIG) -> Path:
    """make_repo without the config committed: .benchbook/ is local and in .git/info/exclude (T01)."""
    root.mkdir(parents=True)
    git(root, "init", "-q", "-b", "main")
    for k, v in (("user.email", "t@example.com"), ("user.name", "t"), ("commit.gpgsign", "false")):
        git(root, "config", k, v)
    for path, text in {"tests/test_impl.py": TEST_IMPL, "tests/test_extra.py": TEST_EXTRA, "impl.txt": "todo\n",
                       "value.txt": "ok\n"}.items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text(text)
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "base")
    (root / ".benchbook").mkdir()
    (root / ".benchbook" / "config.toml").write_text(cfg)
    assert config.exclude_local_dir(root)
    return root


@pytest.fixture
def lead_lg(db_url: str, code_project: str) -> Ledger:
    return Ledger.open(project=code_project, agent="lead", role="orchestrator", database_url=db_url)


def pin_from_config(lg: Ledger, repo: Path, piece_name: str | None = None) -> checks.Pin:
    cfg = config.load(repo)
    return checks.pin_checks(lg, piece_name, cfg.checks, cfg.guard_paths, None, source="config")[0]


def test_pinned_checks_verify_without_a_committed_config(tmp_path: Path, vtmp: Path, lead_lg: Ledger) -> None:
    repo = make_pinned_repo(tmp_path / "repo")
    p = pin_from_config(lead_lg, repo)
    sha = piece(repo, "good", {"impl.txt": "fixed\n"})
    assert git(repo, "show", "--name-only", "--format=", sha) == "impl.txt"  # nothing of .benchbook/ committed
    v, rec = checks.verify(lead_lg, "media", sha, repo=repo, reported_by="impl")
    assert v.all_passed and v.base.source == "pin" and v.base.pin_id == p.id
    m = row(lead_lg, "experiments", rec.experiment_id)["metrics"]
    assert m["checks_source"] == "pin" and m["checks_sha256"] == p.sha256 and m["checks_changed"] is False
    assert rec.outcome is not None and rec.outcome.decision == "verified"
    # pinning the same declarations again records nothing
    assert checks.pin_checks(lead_lg, None, p.checks, p.guard_paths, None, source="config") == (p, False)


def test_a_pin_changed_under_the_work_goes_to_a_person(tmp_path: Path, vtmp: Path, lead_lg: Ledger) -> None:
    repo = make_pinned_repo(tmp_path / "repo")
    pin_from_config(lead_lg, repo)
    sha = piece(repo, "good", {"impl.txt": "fixed\n"})
    cfg = repo / ".benchbook" / "config.toml"
    cfg.write_text(cfg.read_text().replace("grep -qx ok value.txt", "true"))  # the working tree's [checks] moved
    v, rec = checks.verify(lead_lg, "media", sha, repo=repo, reported_by="impl")
    assert v.all_passed and v.integrity.checks_changed
    assert rec.outcome is not None and rec.outcome.decision is None and rec.outcome.person


def test_a_new_pin_after_the_first_attempt_goes_to_a_person(tmp_path: Path, vtmp: Path, lead_lg: Ledger) -> None:
    repo = make_pinned_repo(tmp_path / "repo")
    pin_from_config(lead_lg, repo, "media")
    bad = piece(repo, "bad", {"impl.txt": "no\n"})
    checks.verify(lead_lg, "media", bad, repo=repo, reported_by="impl")  # the first attempt, under the first pin
    checks.pin_checks(lead_lg, "media", checks.parse_check_options(["value=grep -qx ok value.txt"]), [])
    good = piece(repo, "good", {"impl.txt": "fixed\n"})
    v, rec = checks.verify(lead_lg, "media", good, repo=repo, reported_by="impl")
    assert v.all_passed and v.integrity.checks_changed and [r.name for r in v.results] == ["value"]
    assert rec.outcome is not None and rec.outcome.person


def test_a_pin_by_the_implementer_is_ignored(tmp_path: Path, vtmp: Path, db_url: str, code_project: str,
                                             lead_lg: Ledger) -> None:
    repo = make_pinned_repo(tmp_path / "repo")
    impl = Ledger.open(project=code_project, agent="impl", database_url=db_url)
    pin_from_config(impl, repo, "media")
    sha = piece(repo, "good", {"impl.txt": "fixed\n"})
    with pytest.raises(BenchbookError, match="no checks pinned"):
        checks.verify(lead_lg, "media", sha, repo=repo, reported_by="impl")
    lead_pin = pin_from_config(lead_lg, repo)  # the project's pin, by the lead, is used
    v, _ = checks.verify(lead_lg, "media", sha, repo=repo, reported_by="impl")
    assert v.base.pin_id == lead_pin.id


def test_a_committed_config_wins_over_a_pin(repo: Path, vtmp: Path, lead_lg: Ledger) -> None:
    checks.pin_checks(lead_lg, None, checks.parse_check_options(["broken=false"]), [])
    sha = piece(repo, "good", {"impl.txt": "fixed\n"})
    v, rec = checks.verify(lead_lg, "media", sha, repo=repo, reported_by="impl")
    assert v.all_passed and v.base.source == "merge-base" and [r.name for r in v.results] == ["unit", "value"]
    assert set(row(lead_lg, "experiments", rec.experiment_id)["metrics"]) == METRIC_KEYS


@pytest.mark.parametrize(("items", "message"), [(["unit"], "expected NAME=CMD"), (["a=x", "a=y"], "given twice"),
                                                (["bad name=x"], "a check name is")])
def test_bad_check_options(items: list[str], message: str) -> None:
    with pytest.raises(BenchbookError, match=message):
        checks.parse_check_options(items)


def test_cli_init_pins_and_verify_needs_no_commit(tmp_path: Path, vtmp: Path, cli_env: dict[str, str]) -> None:
    repo = make_pinned_repo(tmp_path / "repo")
    (repo / ".git" / "info" / "exclude").write_text("")  # init adds it back
    pid = f"t-{uuid.uuid4().hex[:10]}"
    r = run_cli(repo, cli_env, "--json", "init", "--project", pid, "--pack", "code")
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert out["excluded"] is True and out["pin"].startswith("c:")
    assert "/.benchbook/" in (repo / ".git" / "info" / "exclude").read_text()
    assert "[checks.unit]" in (repo / ".benchbook" / "config.toml").read_text()  # init kept the declarations
    sha = piece(repo, "good", {"impl.txt": "fixed\n"})
    assert git(repo, "log", "--format=%H", "-1", "main") != sha and git(repo, "status", "--porcelain") == ""
    r = run_cli(repo, cli_env, "--agent", "lead", "--json", "check", "verify", "media", sha, "--reported-by", "impl")
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)["decision"] == "verified"
    r = run_cli(repo, cli_env, "--agent", "lead", "--json", "piece", "start", "other", "--paths", "value.txt",
                "--check", "value=grep -qx ok value.txt")
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)["pin"].startswith("c:")


# ------------------------------------------------------------------------------------------- verify worktree (T02)

ENV_CONFIG = """project = "ignored"

[checks.secret]
cmd = "grep -qx s3cret .env"

[guard]
base = "main"
"""


def make_env_repo(root: Path, verify: str) -> Path:
    r = make_repo(root, ENV_CONFIG + verify)
    (r / ".gitignore").write_text(".env\nprepared.txt\n")
    git(r, "add", ".gitignore")
    git(r, "commit", "-q", "-m", "ignore .env")
    (r / ".env").write_text("s3cret\n")
    return r


def test_verify_copies_declared_files_into_the_worktree(tmp_path: Path, vtmp: Path, lead_lg: Ledger) -> None:
    repo = make_env_repo(tmp_path / "repo", '\n[verify]\ncopy = [".env"]\nprepare = "touch prepared.txt"\n')
    sha = piece(repo, "good", {"impl.txt": "fixed\n"})
    v, rec = checks.verify(lead_lg, "media", sha, repo=repo, reported_by="impl")
    assert v.all_passed and v.run_status == "ok"
    m = row(lead_lg, "experiments", rec.experiment_id)["metrics"]
    assert m["prepared"] == {"copied": [".env"], "prepare": True} and "environment" not in m
    assert "s3cret" not in json.dumps(row(lead_lg, "experiments", rec.experiment_id), default=str)
    assert_clean(repo, vtmp)


def test_without_copy_the_check_fails(tmp_path: Path, vtmp: Path, lead_lg: Ledger) -> None:
    repo = make_env_repo(tmp_path / "repo", "")
    sha = piece(repo, "good", {"impl.txt": "fixed\n"})
    v, _ = checks.verify(lead_lg, "media", sha, repo=repo, reported_by="impl")
    assert [r.status for r in v.results] == ["fail"]


@pytest.mark.parametrize(("verify", "error"), [
    ('copy = ["missing.env"]', "missing.env is not a regular file"),
    ('copy = ["impl.txt"]', "impl.txt is tracked"),
    ('prepare = "exit 3"', "exit 3"),
])
def test_a_failed_preparation_is_an_environment_failure(tmp_path: Path, vtmp: Path, lead_lg: Ledger, verify: str,
                                                        error: str) -> None:
    repo = make_env_repo(tmp_path / "repo", f"\n[verify]\n{verify}\n")
    sha = piece(repo, "good", {"impl.txt": "fixed\n"})
    v, rec = checks.verify(lead_lg, "media", sha, repo=repo, reported_by="impl")
    assert v.environment is not None and error in v.environment
    assert v.run_status == "error" and [r.status for r in v.results] == ["not_run"]
    assert rec.relation is None and rec.discarded_id is None and not links_to(lead_lg, rec.claim_id)
    assert row(lead_lg, "experiments", rec.experiment_id)["metrics"]["environment"] is True
    assert_clean(repo, vtmp)


@pytest.mark.parametrize(("table", "message"), [
    ({"copy": ["/etc/passwd"]}, "inside the repository"),
    ({"copy": ["../x"]}, "inside the repository"),
    ({"copy": ".env"}, "list of paths"),
    ({"prepare": ""}, "non-empty"),
    ({"prepare_timeout": 0}, "prepare_timeout"),
    ({"cpy": [".env"]}, "unknown key cpy"),
])
def test_bad_verify_tables(table: dict[str, Any], message: str) -> None:
    with pytest.raises(BenchbookError, match=message):
        config.parse_verify(table)


# ------------------------------------------------------------------------------------------- environment failures (T03)

def implementer_passed(lg: Ledger, piece_name: str, sha: str, names: list[str], repo: Path, *, agent: str = "impl",
                       run_by: str | None = None, definitions: dict[str, str] | None = None) -> None:
    """The implementer's own clean run of the commit passed these checks (check run in its checkout), under the
    definitions the checkout declares unless given."""
    defs = definitions if definitions is not None else {
        n: s.definition for n, s in config.load(repo).checks.items() if n in names}
    lg.as_agent(agent).record_experiment(
        {"piece": piece_name, "checks": names},
        {"checks_passed": len(names), "checks_total": len(names), "checks": dict.fromkeys(names, "ok"),
         "checks_declared": names, "failure_signature": None, "via": "check-run", "worktree_dirty": False,
         "check_definitions": defs, **({"run_by": run_by} if run_by else {})},
        code_ref=sha)


def test_a_check_failing_the_same_way_at_the_merge_base_is_the_environment(tmp_path: Path, vtmp: Path,
                                                                           lead_lg: Ledger) -> None:
    repo = make_env_repo(tmp_path / "repo", "")  # no copy: the clean worktree has no .env, at the base either
    sha = piece(repo, "good", {"impl.txt": "fixed\n"})
    implementer_passed(lead_lg, "media", sha, ["secret"], repo)  # its checkout has the .env
    v, rec = checks.verify(lead_lg, "media", sha, repo=repo, reported_by="impl")
    assert v.environment is not None and "secret fails the same way at the merge-base" in v.environment
    assert v.run_status == "error" and v.results[0].environment
    assert rec.relation is None and rec.discarded_id is None and not links_to(lead_lg, rec.claim_id)
    m = row(lead_lg, "experiments", rec.experiment_id)["metrics"]
    assert m["environment"] is True and m["baseline_signature"] == m["failure_signature"]
    from benchbook.check_cli import render
    assert "environment: secret fails the same way at the merge-base" in render(v, rec)


def test_failing_at_the_merge_base_without_a_pass_elsewhere_still_refutes(tmp_path: Path, vtmp: Path,
                                                                         lead_lg: Ledger) -> None:
    """The piece's goal fails at the base by design: a commit that leaves it failing is a false completion, unless
    the implementer's own run of the commit passed it (which points at the clean worktree's environment)."""
    repo = make_env_repo(tmp_path / "repo", "")
    sha = piece(repo, "good", {"impl.txt": "fixed\n"})
    v, rec = checks.verify(lead_lg, "media", sha, repo=repo, reported_by="impl")
    assert v.environment is None and rec.relation == "refutes" and rec.discarded_id is not None


def test_a_check_that_passes_at_the_merge_base_still_refutes(repo: Path, vtmp: Path, lead_lg: Ledger) -> None:
    sha = piece(repo, "bad", {"value.txt": "no\n"})
    v, rec = checks.verify(lead_lg, "media", sha, repo=repo, reported_by="impl")
    assert v.environment is None and rec.relation == "refutes" and rec.discarded_id is not None


def test_a_test_left_failing_while_others_pass_is_the_pieces(repo: Path, vtmp: Path, lead_lg: Ledger) -> None:
    """The base's test_fixed fails before the fix as well; test_extra passes, so the suite runs: a refutation."""
    sha = piece(repo, "bad", {"impl.txt": "no\n"})
    v, rec = checks.verify(lead_lg, "media", sha, repo=repo, reported_by="impl")
    assert v.environment is None and not v.results[0].environment and rec.relation == "refutes"


def test_the_merge_base_runs_fresh_for_each_environment_failure(tmp_path: Path, vtmp: Path, lead_lg: Ledger,
                                                                  monkeypatch: pytest.MonkeyPatch) -> None:
    repo = make_env_repo(tmp_path / "repo", "")
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    calls: list[str] = []
    real = checks.run_checks

    def counting(repo: Path, commit: str, *a: Any, **kw: Any) -> Any:
        calls.append(commit)
        return real(repo, commit, *a, **kw)

    monkeypatch.setattr(checks, "run_checks", counting)
    one = piece(repo, "one", {"impl.txt": "fixed\n"})
    two = piece(repo, "two", {"impl.txt": "fixed too\n"})
    implementer_passed(lead_lg, "media", one, ["secret"], repo)
    implementer_passed(lead_lg, "other", two, ["secret"], repo)
    checks.verify(lead_lg, "media", one, repo=repo, reported_by="impl")
    v, _ = checks.verify(lead_lg, "other", two, repo=repo, reported_by="impl")
    base = git(repo, "rev-parse", "main")
    assert calls == [one, base, two, base] and v.environment is not None


def service_repo(root: Path, flag: Path, extra: str = "") -> Path:
    """One check that needs a service (FLAG exists) and, with extra, more."""
    cmd = f"test -f {shlex.quote(str(flag))}" + extra
    return make_repo(root, f'project = "ignored"\n\n[checks.service]\ncmd = {json.dumps(cmd)}\n\n'
                           '[guard]\nbase = "main"\n')


def test_a_merge_base_failure_from_before_the_environment_recovered_is_not_reused(
        tmp_path: Path, vtmp: Path, lead_lg: Ledger, monkeypatch: pytest.MonkeyPatch) -> None:
    """The service is down: piece one fails at the commit and at the merge-base (an environment failure).
    The service comes back, and piece two breaks the check for real: the merge-base passes now, so it refutes."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    flag = tmp_path / "service-up"
    repo = service_repo(tmp_path / "repo", flag, " && test ! -f broken.txt")
    one = piece(repo, "one", {"impl.txt": "fixed\n"})
    implementer_passed(lead_lg, "one", one, ["service"], repo)
    v, _ = checks.verify(lead_lg, "one", one, repo=repo, reported_by="impl")
    assert v.environment is not None
    flag.write_text("")
    two = piece(repo, "two", {"broken.txt": "x\n"})
    implementer_passed(lead_lg, "two", two, ["service"], repo)  # what the implementer says it saw
    v, rec = checks.verify(lead_lg, "two", two, repo=repo, reported_by="impl")
    assert v.environment is None and rec.relation == "refutes" and rec.discarded_id is not None
    assert [link["relation"] for link in links_to(lead_lg, rec.claim_id)] == ["refutes"]


@pytest.mark.parametrize(("who", "environment"), [
    ({"agent": "bystander"}, False),  # someone else's pass is not the implementer's
    ({"definitions": {"secret": "0" * 16}}, False),  # a pass of another command under the same name
    ({"definitions": {}}, False),  # a pass that does not say what ran
    ({"agent": "hook@host", "run_by": "impl"}, True),  # the Bash hook's record of the implementer's own run
])
def test_only_the_implementers_pass_of_the_same_definition_makes_an_environment_failure(
        tmp_path: Path, vtmp: Path, lead_lg: Ledger, who: dict[str, Any], environment: bool) -> None:
    repo = make_env_repo(tmp_path / "repo", "")
    sha = piece(repo, "good", {"impl.txt": "fixed\n"})
    implementer_passed(lead_lg, "media", sha, ["secret"], repo, **who)
    v, rec = checks.verify(lead_lg, "media", sha, repo=repo, reported_by="impl")
    assert (v.environment is not None) is environment
    assert rec.relation == (None if environment else "refutes")


def test_a_retried_request_finishes_from_the_attempt_it_recorded(tmp_path: Path, vtmp: Path,
                                                                 lead_lg: Ledger) -> None:
    """The first run failed and was recorded under the request's receipt; the retry would pass (the service came
    back). The retry links and reports the stored failure, never its own run."""
    from benchbook.verify_requests import _result_line

    flag = tmp_path / "service-up"
    repo = service_repo(tmp_path / "repo", flag)
    sha = piece(repo, "flaky", {"impl.txt": "fixed\n"})
    receipt = uuid.uuid4()
    v, first = checks.verify(lead_lg, "flaky", sha, repo=repo, reported_by="impl", receipt_id=receipt)
    assert first.experiment_id == receipt and first.relation == "refutes" and not v.all_passed
    flag.write_text("")
    v, again = checks.verify(lead_lg, "flaky", sha, repo=repo, reported_by="impl", receipt_id=receipt)
    assert again.experiment_id == receipt and again.claim_id == first.claim_id and again.relation == "refutes"
    assert [r.status for r in v.results] == ["fail"] and not v.all_passed
    assert [link["relation"] for link in links_to(lead_lg, first.claim_id)] == ["refutes"]
    assert again.discarded_id == first.discarded_id
    assert "re-run failed" in _result_line(v, again)
    assert row(lead_lg, "experiments", receipt)["metrics"]["checks"] == {"service": "fail"}



def test_guard_compares_junit_counts_in_the_current_environment(
        tmp_path: Path, vtmp: Path, lead_lg: Ledger, monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.conftest import CONFIG

    repo = make_repo(tmp_path / "repo", CONFIG)
    tests = repo / "tests"
    (tests / "test_optional.py").write_text(
        "import os\nfrom pathlib import Path\n"
        "if os.getenv('BENCHBOOK_TEST_EXTRA') and not Path('disable-optional').exists():\n"
        "    def test_optional():\n        assert True\n",
        encoding="utf-8",
    )
    git(repo, "add", "tests/test_optional.py")
    git(repo, "commit", "-m", "Add an environment-dependent test")
    added = {"impl.txt": "fixed\n", "tests/test_notes.py": "# a guarded addition\n"}
    first = piece(repo, "first", added)
    monkeypatch.delenv("BENCHBOOK_TEST_EXTRA", raising=False)
    before, _ = checks.verify(lead_lg, "first", first, repo=repo, reported_by="impl")
    assert before.integrity.tests_before == before.integrity.tests_after == 2

    monkeypatch.setenv("BENCHBOOK_TEST_EXTRA", "1")
    second = piece(repo, "second", {**added, "disable-optional": ""})
    after, rec = checks.verify(lead_lg, "second", second, repo=repo, reported_by="impl")
    assert after.integrity.tests_before == 3 and after.integrity.tests_after == 2
    assert after.integrity.fewer_tests_in == ["unit"]
    assert rec.outcome is not None and rec.outcome.person


def test_verify_requests_retry_and_reuse_only_the_same_environment_failure(
        tmp_path: Path, vtmp: Path, lead_lg: Ledger, monkeypatch: pytest.MonkeyPatch) -> None:
    from benchbook import hooks, session_notes, verify_requests

    flag = tmp_path / "service-up"
    prepare = json.dumps(f"test -f {shlex.quote(str(flag))}")
    repo = make_repo(tmp_path / "repo", '[checks.unit]\ncmd = "true"\n[guard]\nbase = "main"\n'
                     f'[verify]\nprepare = {prepare}\n')
    sha = piece(repo, "media", {"impl.txt": "fixed\n"})
    data = {"piece": "media", "commit": sha, "agent_type": "impl", "repo": str(repo), "session_id": "session",
            "report_sha256": "a" * 64}
    monkeypatch.setenv("BENCHBOOK_AGENT", lead_lg.agent)
    spool = tmp_path / "spool"
    spool.mkdir()
    project = lead_lg.project_id
    verify_requests.process(data, project, uuid.uuid4(), spool)
    assert "environment failure" in session_notes.deliver("session")

    flag.touch()
    verify_requests.process(data, project, uuid.uuid4(), spool)
    assert session_notes.deliver("session") == ""
    receipt = uuid.uuid4()
    report = {**data, "report_sha256": "b" * 64}
    write_info = hooks._write_info

    with monkeypatch.context() as fault:
        def fail_result(path: Path, *args: Any, **kwargs: Any) -> None:
            if path.parent.name == "verify-results":
                raise OSError("cannot write result")
            write_info(path, *args, **kwargs)

        fault.setattr(hooks, "_write_info", fail_result)
        with pytest.raises(OSError, match="cannot write result"):
            verify_requests.process(report, project, receipt, spool)
    flag.unlink()
    verify_requests.process(report, project, receipt, spool)
    note = session_notes.deliver("session")
    assert "re-run passed" in note and "checked: re-run" in note
    attempts = lead_lg.sql_internal("SELECT id FROM benchbook.experiments WHERE project_id=%s", (project,))
    assert len(attempts) == 2



def flaky_repo(root: Path, marker: Path) -> Path:
    """One check that fails the first time it runs and passes after (MARKER remembers the first run)."""
    m = shlex.quote(str(marker))
    cmd = f"sh -c 'test -e {m} || {{ touch {m}; exit 1; }}'"
    return make_repo(root, f'project = "ignored"\n\n[checks.flaky]\ncmd = {json.dumps(cmd)}\n\n'
                           '[guard]\nbase = "main"\n')


def test_a_failed_automatic_re_run_that_passes_on_a_retry_is_inconclusive_and_no_dead_end(
        tmp_path: Path, vtmp: Path, lead_lg: Ledger) -> None:
    """Third boundary pilot: two re-runs failed once on socket timing tests outside the pieces, and became
    refutations and discarded approaches that the lead's own passing check verify could not undo."""
    from benchbook import merge_gate
    from benchbook.verify_requests import _result_line

    repo = flaky_repo(tmp_path / "repo", tmp_path / "ran-once")
    sha = piece(repo, "p", {"impl.txt": "fixed\n"})
    v, rec = checks.verify(lead_lg, "p", sha, repo=repo, reported_by="impl", retry=True)
    assert v.all_passed and v.retry == checks.RETRY and rec.retried_from is not None
    assert rec.outcome is not None and rec.outcome.decision == "inconclusive", rec.outcome
    assert [link["relation"] for link in links_to(lead_lg, rec.claim_id)] == ["refutes", "supports"]
    assert row(lead_lg, "experiments", rec.retried_from)["metrics"]["retry"] == checks.RETRIED_PASSED
    assert rec.discarded_id is None and not lead_lg.sql_internal(
        "SELECT 1 FROM benchbook.claims WHERE project_id = %s AND 'discarded' = ANY(tags)", (lead_lg.project_id,))
    line = _result_line(v, rec, None, lead_lg.role)
    assert "then passed on a retry" in line and "attempts disagree" in line and "failed run e:" in line
    assert not merge_gate.clean(line)
    # The lead finds the failed run broken, invalidates it, and its own re-run decides.
    lead_lg.decide(rec.retried_from, "invalidated", "human", "flaky check outside the piece")
    _, mine = checks.verify(lead_lg.as_agent("lead"), "p", sha, repo=repo, reported_by="impl")
    assert mine.outcome is not None and mine.outcome.decision == "verified", mine.outcome


def test_a_check_that_fails_again_on_the_retry_refutes_once(tmp_path: Path, vtmp: Path, lead_lg: Ledger) -> None:
    repo = service_repo(tmp_path / "repo", tmp_path / "never")
    sha = piece(repo, "p", {"impl.txt": "fixed\n"})
    v, rec = checks.verify(lead_lg, "p", sha, repo=repo, reported_by="impl", retry=True)
    assert not v.all_passed and v.retry == checks.RETRIED_FAILED and rec.retried_from is None
    assert rec.relation == "refutes" and rec.discarded_id is not None
    assert [link["relation"] for link in links_to(lead_lg, rec.claim_id)] == ["refutes"]
    assert row(lead_lg, "experiments", rec.experiment_id)["metrics"]["retry"] == checks.RETRIED_FAILED


def test_check_verify_without_retry_runs_once(tmp_path: Path, vtmp: Path, lead_lg: Ledger) -> None:
    marker = tmp_path / "ran-once"
    repo = flaky_repo(tmp_path / "repo", marker)
    v, rec = checks.verify(lead_lg, "p", piece(repo, "p", {"impl.txt": "fixed\n"}), repo=repo, reported_by="impl")
    assert not v.all_passed and v.retry is None and rec.relation == "refutes" and rec.discarded_id is not None
