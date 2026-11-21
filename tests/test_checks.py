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
from tests.conftest import make_project

PY = shlex.quote(sys.executable)
CONFIG = f"""project = "ignored"

[checks.unit]
cmd = "{PY} -m pytest -q -p no:cacheprovider tests --junitxml={{junit}}"
junit = true
timeout = 120

[checks.value]
cmd = "grep -qx ok value.txt"

[guard]
paths = ["tests/**", "**/conftest.py", ".benchbook/config.toml"]
base = "main"
"""
TEST_IMPL = """def test_fixed():
    print("OUTPUT-MARKER-7f3a")
    assert open("impl.txt").read().strip() == "fixed"
"""
TEST_EXTRA = "def test_extra():\n    assert True\n"
METRIC_KEYS = {"checks_passed", "checks_total", "checks", "failure_signature", "guard_touched", "guard_files",
               "checks_changed"}
DOC = Path(__file__).resolve().parent.parent / "docs" / "code.md"


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


def make_repo(root: Path, cfg: str = CONFIG) -> Path:
    root.mkdir(parents=True)
    git(root, "init", "-q", "-b", "main")
    for k, v in (("user.email", "t@example.com"), ("user.name", "t"), ("commit.gpgsign", "false")):
        git(root, "config", k, v)
    files = {".benchbook/config.toml": cfg, "tests/test_impl.py": TEST_IMPL, "tests/test_extra.py": TEST_EXTRA,
             "impl.txt": "todo\n", "value.txt": "ok\n"}
    for path, text in files.items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text(text)
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "base")
    return root


def piece(repo: Path, branch: str, files: dict[str, str | None]) -> str:
    """A commit on a new branch from main (None deletes the file); main stays checked out."""
    git(repo, "checkout", "-q", "-b", branch, "main")
    for path, text in files.items():
        if text is None:
            git(repo, "rm", "-q", path)
        else:
            (repo / path).parent.mkdir(parents=True, exist_ok=True)
            (repo / path).write_text(text)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", branch)
    sha = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "-q", "main")
    return sha


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
        "checks_passed": 2, "checks_total": 2, "checks": {"unit": "ok", "value": "ok"}, "failure_signature": None,
        "guard_touched": False, "guard_files": [], "checks_changed": False}
    claim = row(verifier, "claims", rec.claim_id)
    assert claim["author"] == "impl" and claim["kind"] == "completion"  # two different agents in the ledger
    assert claim["payload"] == {"piece": "media", "commit": sha, "reported_checks": []}
    assert set(claim["tags"]) == {"piece:media", f"commit:{sha}"}
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
    assert payload == {"piece": "media", "commit": sha, "reported_checks": ["unit"],
                       "report": {"sha256": hashlib.sha256(report.read_bytes()).hexdigest(),
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
    [query] = re.findall(r"```sql\n(.*?)```", DOC.read_text(), re.S)
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
