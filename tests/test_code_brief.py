"""The code pack's brief, discarded-approach claims and the "reads that changed a plan" count (PR 2-P0c,
docs/code.md "Discarded approaches", "The brief", "Reads that changed a plan")."""
from __future__ import annotations

import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from benchbook import Ledger, checks, pieces
from benchbook.attempts import build
from benchbook.brief import compile_brief
from benchbook.errors import ValidationFailed
from benchbook.failures import DISCARDED_DOMAIN, discarded_hash
from benchbook.ids import short
from benchbook.packs import load_pack
from benchbook.tokens import estimate_tokens
from tests.conftest import make_project
from tests.writer import token_ledger

DOC = Path(__file__).resolve().parent.parent / "docs" / "code.md"
SCRIPT = str(Path(sys.executable).parent / "benchbook")
FILES = ["src/app/modules/media/upload.py", "src/app/modules/media/store.py", "src/app/modules/auth/login.py",
         "tests/test_media.py"]


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    for k, v in (("user.email", "t@example.com"), ("user.name", "t"), ("commit.gpgsign", "false")):
        git(root, "config", k, v)
    for f in FILES:
        (root / f).parent.mkdir(parents=True, exist_ok=True)
        (root / f).write_text(f"# {f}\n")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "files")
    return root


@pytest.fixture
def code(db_url: str) -> str:
    d = load_pack("code").project_defaults
    return make_project(db_url, metric=d["metric"], direction=d["direction"], pack="code",
                        settings=dict(d["settings"]))


def agent(db_url: str, project: str, name: str, role: str = "worker") -> Ledger:
    return Ledger.open(project=project, agent=name, role=role, database_url=db_url)


def attempt(lg: Ledger, piece: str, *, status: str = "fail", approach: str | None = None, line: str = "a" * 16,
            failed: list[list[str]] | None = None, dirty: bool = False, sha: str = "c" * 40) -> checks.Recorded:
    """One check-run attempt recorded the way the spool processor records it (attempts.build, checks._record)."""
    data: dict[str, Any] = {"piece": piece, "check": "unit", "status": status, "exit_code": 0 if status == "ok" else 1,
                            "duration_ms": 5, "sha": sha, "repo": "/nonexistent/checkout", "dirty": dirty,
                            "changed": 1 if dirty else 0, "failed_tests": failed or [],
                            "last_line_hash": None if status == "ok" else line}
    if approach:
        data["approach"] = approach
    return checks._record(lg, build("check-run", data), None, None)


def claims(lg: Ledger, tag: str = "discarded") -> list[dict[str, Any]]:
    return lg.sql_internal("SELECT c.*, s.status, s.n_support FROM benchbook.claims c JOIN benchbook.v_claim_status s"
                           " ON s.id = c.id WHERE c.project_id = %s AND %s = ANY(c.tags) ORDER BY c.created_at",
                           (lg.project_id, tag))


# ------------------------------------------------------------------------------------------- discarded approaches

def test_one_discarded_claim_per_piece_signature_and_approach(db_url: str, code: str) -> None:
    impl = agent(db_url, code, "impl-a")
    first = attempt(impl, "media-upload", approach="hash-before-put")
    again = attempt(agent(db_url, code, "impl-b"), "media-upload", approach="hash-before-put")  # anyone's
    assert first.discarded_new and not again.discarded_new and again.discarded_id == first.discarded_id
    attempt(impl, "media-upload", approach="retry-loop")  # another approach
    attempt(impl, "media-upload", approach="hash-before-put", line="b" * 16)  # another failure
    attempt(impl, "media-upload")  # no approach named
    attempt(impl, "auth-refresh", approach="hash-before-put")  # another piece
    rows = claims(impl)
    assert len(rows) == 5
    c = rows[0]
    sig = "unit:exit 1:" + "a" * 16
    assert c["kind"] == "observation" and c["tags"] == ["discarded", "piece:media-upload", "approach:hash-before-put"]
    assert c["statement"] == f"Approach hash-before-put on piece media-upload failed: {sig}"
    assert c["n_support"] == 2 and c["status"] == "proposed" and c["author"] == "impl-a"
    assert c["payload"] == {"piece": "media-upload", "signature": sig, "approach": "hash-before-put",
                            "first_attempt": str(first.experiment_id), "commit": "c" * 40}
    assert c["statement_hash"] == discarded_hash("media-upload", sig, "hash-before-put")
    assert DISCARDED_DOMAIN == b"benchbook:discarded\0"  # fixed: changing it splits claims across releases
    links = impl.sql_internal("SELECT src_id, relation FROM benchbook.links WHERE dst_id = %s ORDER BY created_at",
                              (c["id"],))
    assert links == [{"src_id": first.experiment_id, "relation": "supports"},
                     {"src_id": again.experiment_id, "relation": "supports"}]
    unnamed = next(r for r in rows if "approach" not in r["payload"])
    assert unnamed["tags"] == ["discarded", "piece:media-upload"]
    assert unnamed["statement"].startswith("An attempt without a named approach on piece media-upload failed: ")


def test_passing_dirty_and_interrupted_runs_leave_no_claim(db_url: str, code: str) -> None:
    impl = agent(db_url, code, "impl-a")
    assert attempt(impl, "media-upload", status="ok").discarded_id is None
    assert attempt(impl, "media-upload", dirty=True).discarded_id is None
    assert attempt(impl, "media-upload", status="interrupted").discarded_id is None
    assert claims(impl) == []


def test_the_pieces_module_tags_are_copied(db_url: str, code: str, repo: Path) -> None:
    impl = agent(db_url, code, "impl-a")
    pieces.start(impl, "media-upload", ["src/app/modules/media/*"], repo, tags=["mod:media"])
    attempt(impl, "media-upload", approach="x")
    [c] = claims(impl)
    assert c["tags"] == ["discarded", "piece:media-upload", "approach:x", "mod:media"]


def test_under_a_token_the_claim_is_the_tokens_agent(db_url: str, code: str) -> None:
    tok = token_ledger(db_url, code, "impl-tok")
    r = attempt(tok, "media-upload", approach="x")
    assert r.discarded_id is not None and r.discarded_error is None
    [c] = claims(agent(db_url, code, "reader"))
    assert c["author"] == "impl-tok"
    [link] = tok.sql_internal("SELECT author FROM benchbook.links WHERE dst_id = %s", (c["id"],))
    assert link["author"] == "impl-tok"


def test_a_claim_that_cannot_be_written_never_loses_the_attempt(db_url: str, code: str,
                                                                monkeypatch: pytest.MonkeyPatch) -> None:
    from benchbook import failures

    def broken(*a: Any, **k: Any) -> Any:
        raise RuntimeError("database went away")

    monkeypatch.setattr(failures, "record_discarded", broken)
    impl = agent(db_url, code, "impl-a")
    r = attempt(impl, "media-upload")
    assert r.discarded_id is None and r.discarded_error == "RuntimeError: database went away"
    assert impl.sql_internal("SELECT id FROM benchbook.experiments WHERE id = %s", (r.experiment_id,))


# ------------------------------------------------------------------------------------------- the brief

def _verify(db_url: str, code: str, piece: str, sha: str) -> Any:
    """A verified completion of piece at sha: the implementer's claim, a passing re-run by another agent, and a
    person's decision (oracle human, which needs no rerun rule)."""
    impl = agent(db_url, code, "impl-a")
    c = impl.propose_claim("completion", f"{piece} is complete at {sha[:12]}", payload={"piece": piece, "commit": sha},
                           tags=[f"piece:{piece}", f"commit:{sha}"])
    rerun = attempt(agent(db_url, code, "verifier", "harness"), piece, status="ok", sha=sha)
    human = agent(db_url, code, "human", "human")
    human.decide(c.id, "supported", "human", "re-run passed", [rerun.experiment_id])
    human.decide(c.id, "verified", "human", "re-run passed", [rerun.experiment_id])
    return c.id


def _finding(lg: Ledger, text: str, tags: list[str]) -> Any:
    return lg.propose_claim("observation", text, tags=["out-of-scope", *tags]).id


@pytest.fixture
def ledger(db_url: str, code: str, repo: Path) -> dict[str, Any]:
    """Two modules: media (piece media-upload, impl-a) and auth (piece auth-refresh, impl-b)."""
    a, b = agent(db_url, code, "impl-a"), agent(db_url, code, "impl-b")
    ids: dict[str, Any] = {}
    ids["piece_media"] = pieces.start(a, "media-upload", ["src/app/modules/media/upload.py"], repo,
                                      tags=["mod:media"]).task.id
    ids["piece_auth"] = pieces.start(b, "auth-refresh", ["src/app/modules/auth/*"], repo, tags=["mod:auth"]).task.id
    r1 = attempt(a, "media-upload", approach="hash-before-put",
                 failed=[["tests.test_media::test_reupload", "AssertionError"]])
    attempt(b, "media-upload", approach="hash-before-put",
            failed=[["tests.test_media::test_reupload", "AssertionError"]])
    r2 = attempt(a, "media-upload", approach="retry-loop", line="d" * 16)
    r3 = attempt(b, "auth-refresh", approach="lock-free")
    ids.update(d_hash=r1.discarded_id, e_hash=r1.experiment_id, d_retry=r2.discarded_id, e_retry=r2.experiment_id,
               d_auth=r3.discarded_id)
    ids["f_media"] = _finding(a, "The retry loop ignores the timeout. mod:media",
                              ["mod:media", "file:src/app/modules/media/upload.py"])
    ids["f_file"] = _finding(b, "store.py leaks a file handle", ["file:src/app/modules/media/store.py"])
    ids["f_auth"] = _finding(b, "Logout races with token refresh. mod:auth", ["mod:auth"])
    done = _finding(a, "Uploads lack a size limit. mod:media", ["mod:media"])
    agent(db_url, code, "human", "human").decide(done, "refuted", "human", "by design")
    ids["c_media"] = _verify(db_url, code, "media-upload", "e" * 40)
    ids["c_auth"] = _verify(db_url, code, "auth-refresh", "f" * 40)
    return ids


def _now(lg: Ledger) -> datetime:
    t = lg.sql_internal("SELECT now() AS t")[0]["t"]
    assert isinstance(t, datetime)
    return t


def test_the_brief_for_one_module(db_url: str, code: str, ledger: dict[str, Any]) -> None:
    """Pinned on the fixture ledger: the four sections for mod:media and nothing of mod:auth."""
    lg = agent(db_url, code, "impl-c")
    now = _now(lg)
    out = compile_brief(lg, None, 2000, now=now, log=False, tags=["mod:media"])
    s = {k: short("claim" if k[0] in "dfc" else "experiment" if k[0] == "e" else "task", v) for k, v in ledger.items()}
    expected = f"""# benchbook brief · {code} · {now.astimezone(UTC):%Y-%m-%d %H:%MZ}
Goal: Maximise score.
Scope: mod:media

## Discarded approaches, do not repeat (2 of 2)
- {s['d_retry']} retry-loop on media-upload: unit:exit 1:{'d' * 16} · first {s['e_retry']} · ×1, last just now
- {s['d_hash']} hash-before-put on media-upload: unit:tests.test_media::test_reupload:AssertionError · first \
{s['e_hash']} · ×2, last just now

## Open out-of-scope findings (1 of 1)
- {s['f_media']} "The retry loop ignores the timeout. mod:media" (src/app/modules/media/upload.py)

## Occupied paths (1 of 1 live pieces)
- {s['piece_media']} media-upload by impl-a, Nm left [mod:media]: src/app/modules/media/upload.py

## Last verified commit per piece (1 of 1)
- media-upload at eeeeeeeeeeee ({s['c_media']}, verified just now)

Details: benchbook_get(<id>). Name your approach when you run a check (benchbook check run <check> --approach NAME, \
or BENCHBOOK_APPROACH): a failure seen before is named in the run's output.
"""
    assert re.sub(r"\d+m left", "Nm left", out) == expected
    assert estimate_tokens(out) <= 2000
    # a file: prefix adds what lies under it: the finding that names only a file, and the piece holding paths there
    wider = compile_brief(lg, None, 2000, now=now, log=False, tags=["mod:media", "file:src/app/modules/media/"])
    assert "Scope: mod:media, file:src/app/modules/media/" in wider and s["f_file"] in wider
    assert "## Open out-of-scope findings (2 of 2)" in wider
    auth = compile_brief(lg, None, 2000, now=now, log=False, tags=["mod:auth"])
    assert s["d_auth"] in auth and s["f_auth"] in auth and "auth-refresh at ffffffffffff" in auth
    assert s["d_hash"] not in auth and "media-upload" not in auth


def test_without_tags_the_whole_project_and_an_unknown_tag_shows_none(db_url: str, code: str,
                                                                      ledger: dict[str, Any]) -> None:
    lg = agent(db_url, code, "impl-c")
    out = compile_brief(lg, None, 2000, log=False)
    assert "Scope: the whole project (no --tags)" in out
    assert "## Discarded approaches, do not repeat (3 of 3)" in out and "## Open out-of-scope findings (3 of 3)" in out
    assert "## Occupied paths (2 of 2 live pieces)" in out and "## Last verified commit per piece (2 of 2)" in out
    none = compile_brief(lg, None, 2000, log=False, tags=["mod:billing"])
    assert none.count("- none recorded") == 4


def test_a_discarded_approach_stays_when_the_piece_is_verified(db_url: str, code: str, ledger: dict[str, Any]) -> None:
    """docs/code.md: the claim records that the approach failed; a verified commit does not undo that."""
    lg = agent(db_url, code, "reader")
    [row] = lg.sql_internal("SELECT status FROM benchbook.v_claim_status WHERE id = %s", (ledger["d_hash"],))
    assert row["status"] == "proposed"
    out = compile_brief(lg, None, 2000, log=False, tags=["mod:media"])
    assert short("claim", ledger["d_hash"]) in out and "media-upload at eeeeeeeeeeee" in out


def test_the_budget_trims_bottom_up_and_says_what_it_left_out(db_url: str, code: str, repo: Path) -> None:
    impl = agent(db_url, code, "impl-a")
    pieces.start(impl, "media-upload", ["src/app/modules/media/*"], repo, tags=["mod:media"])
    for i in range(30):
        attempt(impl, "media-upload", approach=f"approach-{i}", line=f"{i:016x}")
        _finding(impl, f"finding number {i} about something fairly specific in the media module mod:media",
                 ["mod:media"])
    full = compile_brief(impl, None, 20000, log=False, tags=["mod:media"])
    assert "## Discarded approaches, do not repeat (8 of 30)" in full and "(+22 more: benchbook search" in full
    small = compile_brief(impl, None, 400, log=False, tags=["mod:media"])
    assert estimate_tokens(small) <= 400 * 1.05
    assert small.startswith("# benchbook brief") and "Scope: mod:media" in small and "Details: benchbook_get" in small
    assert "## Discarded approaches" in small and "more: benchbook search --tags discarded" in small
    assert "## Last verified commit" not in small  # the last section goes first
    default = compile_brief(impl, None, 2000, log=False, tags=["mod:media"])
    assert estimate_tokens(default) <= 2000 * 1.05
    with pytest.raises(ValidationFailed, match="too small"):
        compile_brief(impl, None, 40, log=False, tags=["mod:media"])


def test_the_brief_is_logged_and_reachable_from_the_cli_and_mcp(db_url: str, code: str, ledger: dict[str, Any],
                                                                tmp_path: Path) -> None:
    from benchbook.mcp_server import build
    from tests.test_mcp import call

    lg = agent(db_url, code, "impl-c")
    out = lg.brief(tags=["mod:media"])
    assert "Scope: mod:media" in out
    [log] = lg.sql_internal("SELECT agent_id, tokens_est FROM benchbook.brief_log WHERE project_id = %s", (code,))
    assert log == {"agent_id": "impl-c", "tokens_est": estimate_tokens(out)}
    env = {"BENCHBOOK_DATABASE_URL": db_url, "XDG_STATE_HOME": str(tmp_path / "state"), "PATH": "/usr/bin:/bin"}
    p = subprocess.run([SCRIPT, "--project", code, "--agent", "impl-c", "brief", "--tags", "mod:auth"],
                       capture_output=True, text=True, env=env, cwd=tmp_path, timeout=120)
    assert p.returncode == 0, p.stderr
    assert "Scope: mod:auth" in p.stdout and "auth-refresh" in p.stdout and "media-upload" not in p.stdout
    b = call(build(code, "worker"), "benchbook_brief", agent="impl-c", tags=["mod:media"])
    assert "Scope: mod:media" in b and "auth-refresh" not in b


def test_other_packs_refuse_tags_and_keep_their_brief(db_url: str, lead: Ledger) -> None:
    before = lead.brief()
    assert before.startswith(f"# benchbook brief · {lead.project_id} · ") and "benchbook_check_tried" in before
    with pytest.raises(ValidationFailed, match="does not read tags"):
        lead.brief(tags=["mod:media"])
    ml = Ledger.open(project=make_project(db_url, metric="val_bpb", direction="min", pack="ml-research"),
                     agent="gpu-1", database_url=db_url)
    with pytest.raises(ValidationFailed, match="ml-research pack's brief does not read tags"):
        ml.brief(tags=["mod:media"])


# ------------------------------------------------------------------------------------------- reads that changed a plan

def _plan_query() -> str:
    section = DOC.read_text(encoding="utf-8").split("## Reads that changed a plan", 1)[1].split("\n## ", 1)[0]
    [query] = re.findall(r"```sql\n(.*?)```", section, re.S)
    return query


def test_reads_that_changed_a_plan(db_url: str, code: str, ledger: dict[str, Any]) -> None:
    lead = agent(db_url, code, "lead", "orchestrator")
    written = _now(lead)
    late = _finding(lead, "Found after the plan was written. mod:media", ["mod:media"])
    note = lead.propose_claim("note", "an unrelated note").id
    plan = f"""# Plan for media
- Do not repeat {short('claim', ledger['d_hash'])} (hash-before-put failed); try streaming instead.
- Fix {short('claim', ledger['f_media'])} first. Builds on {short('claim', ledger['c_media'])}.
- See also {short('claim', late)}, {short('claim', note)} and c:00000000, and {short('claim', ledger['d_hash'])} again.
"""
    ids = sorted(set(re.findall(r"\bc:[0-9a-f]{8}\b", plan)))  # the doc's grep
    values = ", ".join(f"('{i}')" for i in ids)
    query = re.sub(r"VALUES \(.*?\)\)", f"VALUES {values})", _plan_query(), count=1)
    query = query.replace("<plan written>", written.isoformat())
    [row] = lead.sql(query)["rows"]
    assert row == {"cited": 6, "in_ledger": 5, "discarded_approaches": 1, "findings": 2, "verified_commits": 1,
                   "reads_that_changed_the_plan": 3}


# ------------------------------------------------------------------------------------- scope by changed paths (T06)

@pytest.mark.parametrize(("paths", "limit", "want"), [
    (["media/a.py", "media/sub/b.py", "x.py"], 20, ["media/", "x.py"]),
    (["a/b/c.py", "a/d/e.py"], 1, ["a/"]),
    ([f"d{i}/f.py" for i in range(25)], 20, [""]),
    ([], 20, []),
])
def test_path_prefixes(paths: list[str], limit: int, want: list[str]) -> None:
    assert checks.path_prefixes(paths, limit) == want


def test_here_scope_finds_a_discarded_approach_without_piece_start(tmp_path: Path, db_url: str, code: str) -> None:
    from benchbook.packs.code.brief import changed_scope
    from tests.conftest import make_repo
    from tests.conftest import piece as commit_piece

    r = make_repo(tmp_path / "r", 'project = "x"\n[checks.value]\ncmd = "test ! -e media/broken && test ! -e '
                  'other/broken"\n[guard]\nbase = "main"\n')
    lead = agent(db_url, code, "lead", "orchestrator")
    media = commit_piece(r, "feature", {"media/broken": "x\n"})
    other = commit_piece(r, "elsewhere", {"other/broken": "x\n"})
    checks.verify(lead, "pm-1", media, repo=r, reported_by="impl")  # no piece start: no task, no mod: tag
    checks.verify(lead, "pm-2", other, repo=r, reported_by="impl")
    assert changed_scope(r) == []  # on main
    git(r, "checkout", "-q", "feature")
    scope = changed_scope(r)
    assert scope == ["file:media/", "piece:feature"]
    out = compile_brief(lead, None, 2000, tags=scope)
    assert "pm-1" in out and "pm-2" not in out
    [claim] = lead.sql_internal("SELECT tags, payload FROM benchbook.claims WHERE project_id = %s"
                                " AND 'discarded' = ANY(tags) AND payload ->> 'piece' = 'pm-1'", (code,))
    assert "file:media/" in claim["tags"] and claim["payload"]["paths"] == ["media/"]
    (r / "other").mkdir()
    (r / "other" / "new.py").write_text("x\n")  # an untracked file joins the scope
    assert changed_scope(r)[:2] == ["file:media/", "file:other/"]
