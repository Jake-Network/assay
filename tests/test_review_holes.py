"""The holes an independent review reproduced in the merged code, each closed (docs/code.md, docs/multi-machine.md):

1. A worker removed itself from the implementer set through the CI flow (pr.reporter trusted a worker's
   payload.reported_by, and the implementers of a claim were that claim's alone).
2. A check declared junit = true passed on exit code 0 when no test had run.
3. Any agent could take another agent's completion claim first: claims were found by statement.
4. A lease holder could write an 'expired' task event in any agent's name.
5. A raw writer could create an intent with settings.worker_intents off, and hold it with an infinite lease.
6. check verify ran the commit's code with the verifier's ledger credentials in its environment.
"""
from __future__ import annotations

import os
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from benchbook import Ledger, attempts, check_run, checks, findings, pr, signature
from benchbook.config import CheckSpec
from benchbook.core.dedup import statement_hash
from benchbook.errors import InvalidTransition, PermissionDenied
from tests.conftest import make_project
from tests.writer import as_writer, issue

PY = shlex.quote(sys.executable)
GUARD = '["tests/**", "**/conftest.py", ".benchbook/config.toml", "check.py"]'
CFG_SCRIPT = f'''project = "ignored"

[checks.unit]
cmd = "{PY} check.py"

[guard]
paths = {GUARD}
base = "main"
'''
CFG_PYTEST = f'''project = "ignored"

[checks.unit]
cmd = "{PY} -m pytest -q -p no:cacheprovider tests --junitxml={{junit}}"
junit = true
timeout = 120

[guard]
paths = {GUARD}
base = "main"
'''
CHECK = "import app\nassert app.VALUE == 1, 'broken'\nprint('ok')\n"
TEST_APP = "import app\n\n\ndef test_value():\n    assert app.VALUE == 1\n"
PASSING = {"checks_passed": 1, "checks_total": 1, "checks": {"unit": "ok"}, "checks_declared": ["unit"],
           "failure_signature": None, "guard_touched": False, "guard_files": [], "checks_changed": False}


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


def make_repo(root: Path, cfg: str = CFG_SCRIPT) -> Path:
    root.mkdir(parents=True)
    git(root, "init", "-q", "-b", "main")
    for k, v in (("user.email", "t@example.com"), ("user.name", "t"), ("commit.gpgsign", "false")):
        git(root, "config", k, v)
    files = {".benchbook/config.toml": cfg, "check.py": CHECK, "tests/test_app.py": TEST_APP, "app.py": "VALUE = 0\n"}
    for path, text in files.items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text(text)
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "base")
    return root


def commit(repo: Path, branch: str, files: dict[str, str], message: str | None = None) -> str:
    git(repo, "checkout", "-q", "-b", branch, "main")
    for path, text in files.items():
        (repo / path).parent.mkdir(parents=True, exist_ok=True)
        (repo / path).write_text(text)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message or branch)
    sha = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "-q", "main")
    return sha


def code_project(db_url: str, **settings: Any) -> str:
    return make_project(db_url, metric="checks_passed", direction="max", pack="code",
                        settings={"partition_key": "piece", "worker_intents": True, **settings})


def tok(db_url: str, project: str, name: str, role: str | None = None) -> tuple[Ledger, str]:
    t = issue(db_url, project, name, role)
    return Ledger.open(project=project, database_url=as_writer(db_url), token=t), t


def local(db_url: str, project: str, name: str, role: str | None = None) -> Ledger:
    return Ledger.open(project=project, agent=name, role=role, database_url=db_url)


def raw(db_url: str, project: str, token: str, sql: str, params: Any = ()) -> list[dict[str, Any]]:
    """One committed transaction on a raw writer connection, authenticated with the agent's own token: what an agent
    that ignores the SDK can do."""
    with psycopg.connect(as_writer(db_url), row_factory=dict_row) as conn:
        conn.execute("SELECT * FROM benchbook.authenticate(%s, %s)", (project, token))
        cur = conn.execute(sql, params)
        rows = cur.fetchall() if cur.description else []
        conn.commit()
        return rows


def owner_rows(db_url: str, sql: str, params: Any = ()) -> list[dict[str, Any]]:
    with psycopg.connect(db_url, row_factory=dict_row) as conn:
        return conn.execute(sql, params).fetchall()


def claim_row(db_url: str, claim: Any) -> dict[str, Any]:
    return owner_rows(db_url, "SELECT author, statement, payload FROM benchbook.claims WHERE id = %s", (claim,))[0]


# ------------------------------------------------------------------------------------------- 1. the implementer set

def test_a_workers_claim_names_the_worker_as_reporter_whatever_it_puts_in_the_payload(db_url: str,
                                                                                    tmp_path: Path) -> None:
    """The CI flow (pr pieces --claims, then check verify --reported-by) cannot launder a worker's report: the reporter
    of a worker's claim is the worker, so the worker is an implementer of CI's claim and its own run is no re-run."""
    project = code_project(db_url)
    lead, _ = tok(db_url, project, "lead")
    impl, _ = tok(db_url, project, "impl-a", "worker")
    ci, _ = tok(db_url, project, "ci-bot", "harness")
    repo = make_repo(tmp_path / "repo")
    sha = commit(repo, "impl", {"app.py": "VALUE = 1\n"}, "media\n\nBenchbook-Piece: media")
    impl.propose_claim("completion", "media: done, see the PR",
                       payload={"piece": "media", "commit": sha, "reported_checks": [], "reported_by": "ghost"},
                       tags=["piece:media", f"commit:{sha}"])
    own = impl.record_experiment({"piece": "media", "checks": ["unit"]}, PASSING, code_ref=sha).id

    [p], _ = pr.pieces(repo, "main", sha)
    assert pr.reporter(ci, p) == "impl-a"
    _, rec = checks.verify(ci, "media", sha, repo=repo, base="main", reported_by=pr.reporter(ci, p), decide=False)
    assert "impl-a" in lead.completion_attempts(rec.claim_id)["implementers"]
    with pytest.raises(InvalidTransition, match="other than the implementer"):
        lead.check_rerun(rec.claim_id, [own])
    # a lead's (or a harness agent's) claim is a report it relays: its payload.reported_by is the reporter
    relayed = ci.propose_claim("completion", "relayed", payload={"piece": "x", "commit": sha, "reported_by": "impl-b"},
                               tags=["piece:x", f"commit:{sha}"])
    assert pr.reporter(ci, pr.Piece("x", sha)) == "impl-b" and relayed.duplicate_of is None


def test_in_local_mode_too_a_workers_payload_does_not_name_the_reporter(db_url: str) -> None:
    project = code_project(db_url)
    w = local(db_url, project, "impl-a", "worker")
    sha = "b" * 40
    w.propose_claim("completion", "done", payload={"piece": "m", "commit": sha, "reported_by": "ghost"},
                    tags=["piece:m", f"commit:{sha}"])
    assert pr.reporter(w, pr.Piece("m", sha)) == "impl-a"


def test_every_completion_of_the_piece_and_commit_adds_its_reporter(db_url: str) -> None:
    """Defence in depth: whoever writes a second claim for the same piece and commit, naming anyone, the implementers
    of each claim still include the reporter of every other one (rule all_pass, check verify, pr summary)."""
    project = code_project(db_url)
    lead, _ = tok(db_url, project, "lead")
    impl, _ = tok(db_url, project, "impl-a", "worker")
    ci, _ = tok(db_url, project, "ci-bot", "harness")
    sha = "c" * 40
    impl.propose_claim("completion", "media is done", payload={"piece": "media", "commit": sha},
                       tags=["piece:media", f"commit:{sha}"])
    own = impl.record_experiment({"piece": "media", "checks": ["unit"]}, PASSING, code_ref=sha).id
    # a harness agent records a claim of the same piece and commit naming someone else (no tags even)
    other = ci.propose_claim("completion", "media is complete, says ci",
                             payload={"piece": "media", "commit": sha, "reported_by": "ghost"}).id
    assert lead.completion_attempts(other)["implementers"] == ["ghost", "impl-a"]
    with pytest.raises(InvalidTransition, match="other than the implementer"):
        lead.check_rerun(other, [own])
    lead.decide(other, "supported", "rerun", "x", [own])
    with pytest.raises(InvalidTransition):
        lead.decide(other, "verified", "rerun", "own run only", [own])
    [state] = pr.describe(ci, [pr.Piece("media", sha)])
    assert set(state.implementers) >= {"impl-a", "ghost"} and state.independent == []


def test_a_worker_naming_the_lead_adds_only_itself_to_other_claims(db_url: str) -> None:
    """Naming a reporter adds that name to the worker's own claim; to the other claims of the piece and commit a
    worker adds only itself, so a worker cannot shut the lead (or CI) out of verifying someone else's completion."""
    project = code_project(db_url)
    lead, _ = tok(db_url, project, "lead")
    b, _ = tok(db_url, project, "impl-b", "worker")
    x, _ = tok(db_url, project, "impl-x", "worker")
    sha = "d" * 40
    mine = b.propose_claim("completion", f"media is complete at {sha[:12]}", payload={"piece": "media", "commit": sha},
                           tags=["piece:media", f"commit:{sha}"]).id
    spoiler = x.propose_claim("completion", "media, reported by the lead",
                              payload={"piece": "media", "commit": sha, "reported_by": "lead"},
                              tags=["piece:media", f"commit:{sha}"]).id
    assert lead.completion_attempts(spoiler)["implementers"] == ["impl-b", "impl-x", "lead"]
    assert lead.completion_attempts(mine)["implementers"] == ["impl-b", "impl-x"]
    rerun = lead.record_experiment({"piece": "media", "checks": ["unit"]}, PASSING, code_ref=sha).id
    lead.decide(mine, "supported", "rerun", "x", [rerun])
    lead.decide(mine, "verified", "rerun", "the lead's re-run", [rerun])
    assert lead.claim_status(mine) == "verified"


# ------------------------------------------------------------------------------------------- 2. junit = true

def junit_run(tmp_path: Path, xml: str | None, exit_code: int = 0) -> checks.CheckResult:
    """_run_one of a junit check whose command writes xml (None: nothing) to {junit} and exits with exit_code."""
    wt, out = tmp_path / "wt", tmp_path / "out"
    wt.mkdir(parents=True)
    out.mkdir()
    (wt / "write.py").write_text("import sys\nif len(sys.argv) > 2:\n    open(sys.argv[1], 'w').write(sys.argv[2])\n"
                                 f"sys.exit({exit_code})\n")
    arg = "" if xml is None else " " + shlex.quote(xml)
    return checks._run_one(CheckSpec("unit", f"{PY} write.py {{junit}}{arg}", True, 30), wt, out)


OK_CASE = '<testcase classname="t" name="a"/>'


@pytest.mark.parametrize(("xml", "status", "part"), [
    (None, "fail", signature.NO_REPORT),
    ("not xml", "fail", signature.NO_REPORT),
    ('<testsuite tests="0"></testsuite>', "fail", signature.NO_TESTS),
    ('<testsuite><testcase classname="t" name="a"><skipped/></testcase></testsuite>', "fail", signature.NO_TESTS),
    ('<testsuite><testcase classname="t" name="a"><failure message="x"/></testcase></testsuite>', "fail",
     "t::a:failed"),
    (f"<testsuite>{OK_CASE}</testsuite>", "ok", None),
    (f'<testsuites><testsuite>{OK_CASE}<testcase classname="t" name="b"><skipped/></testcase></testsuite>'
     "</testsuites>", "ok", None),
])
def test_a_junit_check_that_exits_0_passes_only_with_a_report_in_which_a_test_ran(
        tmp_path: Path, xml: str | None, status: str, part: str | None) -> None:
    r = junit_run(tmp_path, xml)
    assert (r.status, r.exit_code, r.signature) == (status, 0, part)


def test_a_failing_junit_check_is_signed_as_before(tmp_path: Path) -> None:
    r = junit_run(tmp_path, '<testsuite><testcase classname="t" name="a"><error type="KeyError"/></testcase>'
                            "</testsuite>", exit_code=1)
    assert (r.status, r.signature, r.failed_tests) == ("fail", "t::a:KeyError", [("t::a", "KeyError")])
    assert junit_run(tmp_path / "x", None, exit_code=2).signature.startswith("exit 2:")  # type: ignore[union-attr]


def test_exit_0_before_any_test_ran_is_not_verified(db_url: str, tmp_path: Path) -> None:
    """Code outside the guarded paths ends pytest with status 0 while collecting: tests/, conftest.py and [checks]
    are unchanged, and the re-run used to pass."""
    project = code_project(db_url)
    lead, _ = tok(db_url, project, "lead")
    impl, _ = tok(db_url, project, "impl-a", "worker")
    repo = make_repo(tmp_path / "repo", CFG_PYTEST)
    sha = commit(repo, "impl", {"app.py": "import os\nos._exit(0)\nVALUE = 0\n"})
    impl.propose_claim("completion", f"media is complete at {sha[:12]}",
                       payload={"piece": "media", "commit": sha, "reported_checks": []},
                       tags=["piece:media", f"commit:{sha}"])
    v, rec = checks.verify(lead, "media", sha, repo=repo, base="main", reported_by="impl-a")
    assert not v.all_passed and v.results[0].status == "fail" and v.results[0].exit_code == 0
    assert v.metrics()["failure_signature"] == f"unit:{signature.NO_REPORT}"
    assert rec.relation == "refutes" and rec.outcome is not None and rec.outcome.decision is None
    good = commit(repo, "good", {"app.py": "VALUE = 1\n"})
    v2, _ = checks.verify(lead, "media", good, repo=repo, base="main")
    assert v2.all_passed  # a report with a passing test still passes


def test_check_run_records_a_junit_check_that_ran_nothing_as_failed(tmp_path: Path, db_url: str,
                                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    project = code_project(db_url)
    repo = make_repo(tmp_path / "repo", CFG_PYTEST.replace('project = "ignored"', f'project = "{project}"'))
    git(repo, "checkout", "-q", "-b", "media")
    (repo / "app.py").write_text("import os\nos._exit(0)\n")
    git(repo, "commit", "-qam", "exit early")
    queued: list[dict[str, Any]] = []
    monkeypatch.setattr(check_run, "spool_attempt", lambda event, proj, data: queued.append(data) or tmp_path)
    assert check_run.run("unit", cwd=repo) == 0  # the exit code stays the command's
    [data] = queued
    assert (data["status"], data["exit_code"], data["signature"]) == ("fail", 0, signature.NO_REPORT)
    run = attempts.build(check_run.EVENT, data)
    assert run.metrics()["checks"] == {"unit": "fail"} and run.metrics()["checks_passed"] == 0


# ------------------------------------------------------------------------------------------- 3. claim identity

def test_another_agent_cannot_take_a_completion_claim_first(db_url: str, tmp_path: Path) -> None:
    """A second worker writes the usual statement before the implementer is verified: with the right payload (to take
    the credit) and with a wrong commit in it (to block the piece). Neither works: the report gets its own claim,
    the pre-empter only makes itself an implementer too, and the lead's re-run verifies it."""
    project = code_project(db_url)
    lead, _ = tok(db_url, project, "lead")
    a, _ = tok(db_url, project, "impl-a", "worker")
    repo = make_repo(tmp_path / "repo")
    sha = commit(repo, "b-work", {"app.py": "VALUE = 1\n"})
    sha2 = commit(repo, "b-work2", {"app.py": "VALUE = 1  # v2\n"})
    first = a.propose_claim("completion", f"media is complete at {sha[:12]}",
                            payload={"piece": "media", "commit": sha, "reported_checks": []},
                            tags=["piece:media", f"commit:{sha}"]).id
    a.propose_claim("completion", f"media is complete at {sha2[:12]}",
                    payload={"piece": "media", "commit": "0" * 40, "reported_checks": []}, tags=[])

    _, rec = checks.verify(lead, "media", sha, repo=repo, base="main", reported_by="impl-b")
    assert rec.claim_id != first and not rec.claim_existed and rec.outcome.decision == "verified"  # type: ignore[union-attr]
    row = claim_row(db_url, rec.claim_id)
    assert row["author"] == "lead" and row["payload"]["reported_by"] == "impl-b"
    assert row["statement"] == f"media is complete at {sha[:12]}, reported by impl-b"
    assert lead.completion_attempts(rec.claim_id)["implementers"] == ["impl-a", "impl-b"]
    # the pull request summary shows the verified claim, not the older one the pre-empter wrote
    [state] = pr.describe(lead, [pr.Piece("media", sha)])
    assert (state.claim_id, state.state, state.reporter) == (rec.claim_id, pr.VERIFIED, "impl-b")
    assert pr.reporter(lead, pr.Piece("media", sha)) == "impl-b"

    _, rec2 = checks.verify(lead, "media", sha2, repo=repo, base="main", reported_by="impl-b")
    assert rec2.relation == "supports" and rec2.outcome.decision == "verified"  # type: ignore[union-attr]
    assert claim_row(db_url, rec2.claim_id)["payload"]["commit"] == sha2

    # one claim per (piece, commit, reporter): the next verify of the same report finds it again
    _, again = checks.verify(lead, "media", sha, repo=repo, base="main", reported_by="impl-b")
    assert again.claim_id == rec.claim_id and again.claim_existed


def test_both_statements_taken_first_still_leave_the_report_its_own_claim(db_url: str, tmp_path: Path) -> None:
    """statement_hash is the client's: a writer can store the usual statement's hash under any text, and the
    statement naming the reporter too. The claim then gets a hash nobody could know, and is still found again."""
    project = code_project(db_url)
    lead, _ = tok(db_url, project, "lead")
    a, _ = tok(db_url, project, "impl-a", "worker")
    repo = make_repo(tmp_path / "repo")
    sha = commit(repo, "b-work", {"app.py": "VALUE = 1\n"})
    usual, named = checks.statements("media", sha, "impl-b")
    p = {"piece": "media", "commit": sha}
    a._propose_claim("completion", "something else entirely", p, ["piece:media", f"commit:{sha}"],
                     _dedup_hash=statement_hash(usual))
    a.propose_claim("completion", named, {**p, "reported_by": "impl-b"}, ["piece:media", f"commit:{sha}"])
    _, rec = checks.verify(lead, "media", sha, repo=repo, base="main", reported_by="impl-b")
    row = claim_row(db_url, rec.claim_id)
    assert (row["author"], row["statement"], rec.claim_existed) == ("lead", named, False)
    assert rec.outcome is not None and rec.outcome.decision == "verified"
    _, again = checks.verify(lead, "media", sha, repo=repo, base="main", reported_by="impl-b")
    assert again.claim_id == rec.claim_id and again.claim_existed
    assert len(owner_rows(db_url, "SELECT id FROM benchbook.claims WHERE project_id = %s AND kind = 'completion'"
                                  " AND author = 'lead'", (project,))) == 1


def test_in_local_mode_the_report_gets_its_own_claim_in_the_reporters_name(db_url: str, tmp_path: Path) -> None:
    project = code_project(db_url)
    lead = local(db_url, project, "lead", "orchestrator")
    a = local(db_url, project, "impl-a", "worker")
    repo = make_repo(tmp_path / "repo")
    sha = commit(repo, "b-work", {"app.py": "VALUE = 1\n"})
    a.propose_claim("completion", f"media is complete at {sha[:12]}", payload={"piece": "media", "commit": sha},
                    tags=["piece:media", f"commit:{sha}"])
    _, rec = checks.verify(lead, "media", sha, repo=repo, base="main", reported_by="impl-b")
    row = claim_row(db_url, rec.claim_id)
    assert row["author"] == "impl-b" and "reported_by" not in row["payload"] and not rec.claim_existed
    assert rec.outcome is not None and rec.outcome.decision == "verified"
    _, again = checks.verify(lead, "media", sha, repo=repo, base="main", reported_by="impl-b")
    assert again.claim_id == rec.claim_id and again.claim_existed


def test_a_worker_verifier_reuses_its_own_record_of_a_report(db_url: str, tmp_path: Path) -> None:
    """A worker's claim naming another reporter is the worker's (reporter_of), but it is still the claim this
    verifier writes for that report: verifying again does not pile up claims."""
    project = code_project(db_url)
    w, _ = tok(db_url, project, "impl-w", "worker")
    repo = make_repo(tmp_path / "repo")
    sha = commit(repo, "b-work", {"app.py": "VALUE = 1\n"})
    _, one = checks.verify(w, "media", sha, repo=repo, base="main", reported_by="impl-b")
    _, two = checks.verify(w, "media", sha, repo=repo, base="main", reported_by="impl-b")
    assert two.claim_id == one.claim_id and two.claim_existed and not one.claim_existed


# ------------------------------------------------------------------------------------------- 4. task events

def test_an_expired_event_names_only_the_previous_holder(db_url: str) -> None:
    project = make_project(db_url)
    lead, _ = tok(db_url, project, "lead")
    w1, _ = tok(db_url, project, "w1", "worker")
    w2, w2tok = tok(db_url, project, "w2", "worker")
    [tid] = lead.create_tasks([{"title": "t"}])
    assert w1.next_task() is not None
    # the lease runs out (the owner connection moves it back), and w2 takes the task over through the SDK
    owner_rows(db_url, "UPDATE benchbook.tasks SET lease_expires_at = now() - interval '1 second' WHERE id = %s"
                       " RETURNING id", (tid,))
    assert w2.next_task() is not None
    events = owner_rows(db_url, "SELECT event, agent_id FROM benchbook.task_events WHERE task_id = %s ORDER BY id",
                        (tid,))
    assert [(e["event"], e["agent_id"]) for e in events][-2:] == [("expired", "w1"), ("claimed", "w2")]
    insert = ("INSERT INTO benchbook.task_events(project_id,task_id,event,agent_id,detail)"
              " VALUES (%s,%s,'expired',%s,'{}')")
    for name in ("lead", "w3"):
        with pytest.raises(psycopg.errors.InsufficientPrivilege, match="cannot write a task event"):
            raw(db_url, project, w2tok, insert, (project, tid, name))


# ------------------------------------------------------------------------------------------- 5. guard_task

INTENT_SQL = ("INSERT INTO benchbook.tasks(project_id,title,spec,tags,created_by,status,lease_owner,lease_expires_at,"
              "attempts,max_attempts) VALUES (%s,%s,%s,ARRAY['intent','piece','piece:media'],%s,'claimed',%s,"
              "{until},1,1) RETURNING id")


def raw_intent(db_url: str, project: str, token: str, agent: str, until: str = "now() + interval '1 hour'",
               spec: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    return raw(db_url, project, token, INTENT_SQL.format(until=until),
               (project, "piece media", Jsonb(spec or {"piece": "media"}), agent, agent))


def test_a_raw_intent_needs_worker_intents_for_a_worker(db_url: str) -> None:
    core = make_project(db_url)
    w, wtok = tok(db_url, core, "w1", "worker")
    _, ltok = tok(db_url, core, "lead")
    with pytest.raises(PermissionDenied):
        w.create_intent("x", {"paths": ["src/*"]})
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match="worker_intents"):
        raw_intent(db_url, core, wtok, "w1")
    assert raw_intent(db_url, core, ltok, "lead")  # a lead's intent needs no setting, as in the SDK
    on = code_project(db_url)
    _, otok = tok(db_url, on, "w1", "worker")
    assert raw_intent(db_url, on, otok, "w1")


def test_a_lease_is_capped_by_max_lease_seconds(db_url: str) -> None:
    project = code_project(db_url)
    w, wtok = tok(db_url, project, "impl-a", "worker")
    lead, _ = tok(db_url, project, "lead")
    for until in ("'infinity'", "now() + interval '86401 seconds'"):
        with pytest.raises(psycopg.errors.InsufficientPrivilege, match="max_lease_seconds"):
            raw_intent(db_url, project, wtok, "impl-a", until)
    t = w.create_intent("piece media", {"piece": "media", "paths": ["src/media/*"]}, ["piece", "piece:media"], 7200)
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match="max_lease_seconds"):  # a heartbeat, by hand
        raw(db_url, project, wtok, "UPDATE benchbook.tasks SET lease_expires_at = 'infinity' WHERE id = %s", (t.id,))
    w.heartbeat(t.id, 3600)
    [tid] = lead.create_tasks([{"title": "queued"}])
    with pytest.raises(PermissionDenied, match="max_lease_seconds"):  # a claim through the SDK
        w.claim_task(tid, lease_seconds=86400 * 2)
    lead.update_settings({"max_lease_seconds": 600})
    with pytest.raises(PermissionDenied, match="at most 600 s"):
        w.heartbeat(t.id, 3600)
    assert w.claim_task(tid, lease_seconds=600).lease_owner == "impl-a"
    # the owner connection (local mode) is trusted and keeps any lease
    local(db_url, project, "impl-a").heartbeat(t.id, 86400 * 30)


def test_path_overlap_is_checked_by_the_sdk_only(db_url: str) -> None:
    """The documented limit (docs/code.md, docs/multi-machine.md): a raw writer with worker_intents on can still
    hold a piece and paths another agent holds, within the lease cap. The SDK refuses it."""
    project = code_project(db_url)
    b, _ = tok(db_url, project, "impl-b", "worker")
    _, atok = tok(db_url, project, "impl-a", "worker")
    b.create_intent("piece media", {"piece": "media", "paths": ["src/media/*"]}, ["piece", "piece:media"], 7200,
                    exclusive=["piece"], exclusive_paths=["paths"])
    assert raw_intent(db_url, project, atok, "impl-a", spec={"piece": "media", "paths": ["**"]})


# ------------------------------------------------------------------------------------------- 6. the checks' environment

ENV_CFG = '''project = "ignored"

[checks.unit]
cmd = "env > \\"$DUMP_TO\\""
'''
CREDENTIALS = {"BENCHBOOK_TOKEN": "bbt_secret", "ASSAY_TOKEN": "bbt_old", "BENCHBOOK_READER_PASSWORD": "rp",
               "ASSAY_READER_PASSWORD": "rp-old", "ASSAY_DATABASE_URL": "postgresql://x", "PGPASSWORD": "pw",
               "PGPASSFILE": "/tmp/pgpass"}


def test_check_verify_runs_the_checks_without_ledger_credentials(db_url: str, tmp_path: Path,
                                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    project = code_project(db_url)
    lead = local(db_url, project, "lead", "orchestrator")
    repo = make_repo(tmp_path / "repo", ENV_CFG)
    sha = commit(repo, "w", {"app.py": "VALUE = 1\n"})
    dump = tmp_path / "env.txt"
    assert os.environ.get("BENCHBOOK_DATABASE_URL")  # set by conftest, as the action sets it
    for k, v in {**CREDENTIALS, "DUMP_TO": str(dump), "BENCH_SWE_HIDDEN": "kept"}.items():
        monkeypatch.setenv(k, v)
    v, _ = checks.verify(lead, "media", sha, repo=repo, base="main", reported_by="impl")
    assert v.all_passed
    names = {line.split("=", 1)[0] for line in dump.read_text().splitlines() if "=" in line}
    assert not names & (set(CREDENTIALS) | {"BENCHBOOK_DATABASE_URL"})
    assert {"BENCH_SWE_HIDDEN", "PATH", "DUMP_TO"} <= names
    assert checks.CREDENTIALS >= set(CREDENTIALS) | {"BENCHBOOK_DATABASE_URL"}


def test_check_run_keeps_the_implementers_environment(db_url: str, tmp_path: Path,
                                                      monkeypatch: pytest.MonkeyPatch) -> None:
    project = code_project(db_url)
    repo = make_repo(tmp_path / "repo", ENV_CFG.replace('project = "ignored"', f'project = "{project}"'))
    git(repo, "checkout", "-q", "-b", "media")
    dump = tmp_path / "env.txt"
    monkeypatch.setenv("DUMP_TO", str(dump))
    monkeypatch.setenv("BENCHBOOK_READER_PASSWORD", "rp")
    monkeypatch.setattr(check_run, "spool_attempt", lambda event, proj, data: tmp_path)
    assert check_run.run("unit", cwd=repo) == 0
    names = {line.split("=", 1)[0] for line in dump.read_text().splitlines() if "=" in line}
    assert {"BENCHBOOK_DATABASE_URL", "BENCHBOOK_READER_PASSWORD"} <= names


# ------------------------------------------------------------------------------------------- regression

def test_discarded_approaches_and_findings_are_written_under_tokens(db_url: str, tmp_path: Path) -> None:
    """Kept from the review's soundness checks: the post-0008 write paths work on a writer connection, each bound
    to the token's agent."""
    project = code_project(db_url)
    ci, _ = tok(db_url, project, "ci-bot", "harness")
    w, _ = tok(db_url, project, "impl-a", "worker")
    repo = make_repo(tmp_path / "repo")
    sha = commit(repo, "bad", {"app.py": "VALUE = 2\n"})
    _, rec = checks.verify(ci, "media", sha, repo=repo, base="main", reported_by="impl-a")
    assert rec.relation == "refutes" and rec.discarded_id is not None and rec.discarded_error is None
    out = findings.capture(w, "## Out of scope\n- `src/x.py` leaks a handle\n", "Out of scope",
                           source={"agent_type": "t"}, piece="media")
    assert out.findings == 1 and out.new == 1
    authors = owner_rows(db_url, "SELECT DISTINCT author FROM benchbook.claims WHERE project_id = %s"
                                 " AND kind = 'observation' ORDER BY 1", (project,))
    assert [r["author"] for r in authors] == ["ci-bot", "impl-a"]
