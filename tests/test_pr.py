"""PR 2-P1a: CI evidence and the PR evidence summary. The pieces of a commit range by trailer, the summary's three
states (and "not reported"), the one comment kept by marker (with a stand-in gh), check verify's --trace-ref and
--no-decide, and the GitHub Action's script run end to end, in local mode and under a token."""
from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from benchbook import Ledger, checks, pr
from tests.conftest import make_project

from .writer import as_writer, issue

ROOT = Path(__file__).resolve().parent.parent
ACTION = ROOT / "integrations" / "github-action"
SCRIPT = ACTION / "run.sh"
BIN = Path(sys.executable).parent  # where the benchbook console script of this environment is
GH_TOKEN = "gh-token-for-tests-3c1d"


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


def commit(repo: Path, message: str, files: dict[str, str]) -> str:
    for path, text in files.items():
        (repo / path).parent.mkdir(parents=True, exist_ok=True)
        (repo / path).write_text(text)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)
    return git(repo, "rev-parse", "HEAD")


def new_repo(root: Path, config: str = "") -> Path:
    root.mkdir(parents=True)
    git(root, "init", "-q", "-b", "main")
    for k, v in (("user.email", "t@example.com"), ("user.name", "t"), ("commit.gpgsign", "false")):
        git(root, "config", k, v)
    files = {"value.txt": "ok\n", "tests/test_a.py": "def test_a():\n    pass\n", "a.txt": "a\n"}
    if config:
        files[".benchbook/config.toml"] = config
    commit(root, "base", files)
    return root


# ------------------------------------------------------------------------------------------- pieces from trailers

def test_pieces_come_from_the_trailer_and_the_last_commit_carrying_it_wins(tmp_path: Path) -> None:
    repo = new_repo(tmp_path / "repo")
    commit(repo, "on main before the pull request\n\nBenchbook-Piece: old", {"a.txt": "0\n"})
    base = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "-q", "-b", "feature")
    c1 = commit(repo, "first\n\nBenchbook-Piece: alpha", {"a.txt": "1\n"})
    c2 = commit(repo, "second\n\nBenchbook-Piece: beta\nCo-Authored-By: someone <s@example.com>", {"a.txt": "2\n"})
    c3 = commit(repo, "alpha again, and gamma\n\nBenchbook-Piece: alpha\nbenchbook-piece: gamma", {"a.txt": "3\n"})
    commit(repo, "no trailer\n\nBenchbook-Piece is mentioned in the body only", {"a.txt": "4\n"})
    commit(repo, "a bad value\n\nBenchbook-Piece: has a space", {"a.txt": "5\n"})
    found, skipped = pr.pieces(repo, base, "HEAD")
    assert [(p.piece, p.commit) for p in found] == [("beta", c2), ("alpha", c3), ("gamma", c3)]
    assert c1 not in {p.commit for p in found}
    assert skipped == ["has a space"]
    assert pr.pieces(repo, "HEAD", "HEAD") == ([], [])


def test_pieces_cli_prints_tab_separated_lines(tmp_path: Path) -> None:
    repo = new_repo(tmp_path / "repo")
    base = git(repo, "rev-parse", "HEAD")
    c1 = commit(repo, "x\n\nBenchbook-Piece: media-upload", {"a.txt": "1\n"})
    env = {k: v for k, v in os.environ.items() if not k.startswith(("BENCHBOOK_", "ASSAY_"))}
    r = subprocess.run([str(BIN / "benchbook"), "pr", "pieces", "--base", base], cwd=repo, env=env,
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    assert r.stdout == f"media-upload\t{c1}\n"  # no ledger needed without --claims


# ------------------------------------------------------------------------------------------- the summary

CHECKS = """[checks.value]
cmd = "{env_dump}grep -qx ok value.txt"

[guard]
paths = ["tests/**", ".benchbook/config.toml"]
base = "main"
"""


@pytest.fixture
def code_project(db_url: str) -> str:
    return make_project(db_url, metric="checks_passed", direction="max", pack="code",
                        settings={"partition_key": "piece"})


def pr_repo(root: Path, project: str, env_dump: Path | None = None) -> dict[str, str]:
    """main with the declarations, then a pull request branch with four pieces: alpha passes, beta fails, gamma
    passes but changes a test, delta passes and nobody reported it."""
    dump = f"env >> '{env_dump}'; " if env_dump else ""
    repo = new_repo(root, f'project = "{project}"\n\n' + CHECKS.replace("{env_dump}", dump))
    shas = {"base": git(repo, "rev-parse", "HEAD")}
    git(repo, "checkout", "-q", "-b", "feature")
    shas["alpha"] = commit(repo, "alpha\n\nBenchbook-Piece: alpha", {"a.txt": "alpha\n"})
    shas["beta"] = commit(repo, "beta\n\nBenchbook-Piece: beta", {"value.txt": "broken\n"})
    shas["gamma"] = commit(repo, "gamma\n\nBenchbook-Piece: gamma",
                           {"value.txt": "ok\n", "tests/test_a.py": "def test_a():\n    assert True\n"})
    shas["delta"] = commit(repo, "delta\n\nBenchbook-Piece: delta", {"d.txt": "delta\n"})
    shas["head"] = shas["delta"]
    return shas


def report(lg: Ledger, piece: str, sha: str) -> Any:
    """The implementer's completion claim, as check verify --reported-by records it."""
    return lg.propose_claim("completion", f"{piece} is complete at {sha[:12]}", payload={"piece": piece, "commit": sha},
                            tags=[f"piece:{piece}", f"commit:{sha}"]).id


def test_summary_distinguishes_the_states(tmp_path: Path, db_url: str, code_project: str) -> None:
    repo = tmp_path / "repo"
    shas = pr_repo(repo, code_project)
    impl = Ledger.open(project=code_project, agent="impl", database_url=db_url)
    claims = {p: report(impl, p, shas[p]) for p in ("alpha", "beta", "gamma")}
    found, _ = pr.pieces(repo, shas["base"], shas["head"])
    assert [p.piece for p in found] == ["alpha", "beta", "gamma", "delta"]

    # before any re-run: three claims and nothing confirmed
    before = {s.piece: s for s in pr.describe(impl, found)}
    assert [before[p].state for p in ("alpha", "beta", "gamma", "delta")] == [
        pr.CLAIMED, pr.CLAIMED, pr.CLAIMED, pr.UNREPORTED]
    assert before["alpha"].guard_touched is None and pr._tests(before["alpha"]) == "unknown (no run)"
    assert all(s.discarded == 0 and s.open_findings == [] for s in before.values())

    ci = Ledger.open(project=code_project, agent="ci:41", database_url=db_url)
    trace = {"ci": "github-actions", "run_url": "https://github.com/o/r/actions/runs/41"}
    for p in ("alpha", "beta", "gamma"):
        assert pr.reporter(ci, pr.Piece(p, shas[p])) == "impl"
        v, rec = checks.verify(ci, p, shas[p], repo=repo, base=shas["base"], reported_by="impl", trace_ref=trace,
                               decide=False)
        assert rec.claim_id == claims[p] and rec.claim_existed  # the re-run went to the implementer's claim
        exp = ci.sql_internal("SELECT author, trace_ref FROM benchbook.experiments WHERE id = %s",
                              (rec.experiment_id,))[0]
        assert exp == {"author": "ci:41", "trace_ref": trace}
    ci.record_experiment({"piece": "delta", "checks": ["value"]},
                         {"checks_passed": 1, "checks_total": 1, "guard_touched": False, "checks_changed": False},
                         code_ref=shas["delta"])

    states = {s.piece: s for s in pr.describe(ci, found)}
    assert states["alpha"].state == pr.VERIFIED and states["alpha"].independent == ["ci:41"]
    assert states["alpha"].claim_status == "proposed"  # recorded, not decided
    assert states["beta"].state == pr.DISAGREES and states["beta"].failed == 1
    assert states["gamma"].state == pr.CLAIMED and states["gamma"].guard_files == ["tests/test_a.py"]
    assert states["delta"].state == pr.UNREPORTED and states["delta"].passed == 1
    assert pr._tests(states["alpha"]) == "no" and pr._tests(states["gamma"]).startswith("weakened: 1 file")

    # the failed re-run of beta left one discarded approach by itself; alpha has none yet
    assert (states["alpha"].discarded, states["beta"].discarded) == (0, 1)

    # discarded approaches and open out-of-scope findings of a piece
    impl.propose_claim("observation", "tried caching the upload; slower", tags=["discarded", "piece:alpha"])
    impl.propose_claim("observation", "tried a second queue; races", tags=["discarded", "piece:alpha"])
    impl.propose_claim("observation", "discarded elsewhere", tags=["discarded", "piece:beta"])
    open_f = impl.propose_claim("observation", "retries ignore the timeout", tags=["out-of-scope", "piece:alpha"])
    shut = impl.propose_claim("observation", "flaky test in auth", tags=["out-of-scope", "piece:alpha"])
    lead = Ledger.open(project=code_project, agent="lead", role="orchestrator", database_url=db_url)
    lead.decide(shut.id, "refuted", "human", "by design")
    states = {s.piece: s for s in pr.describe(ci, found)}
    assert states["alpha"].discarded == 2 and states["beta"].discarded == 2
    assert states["alpha"].open_findings == [open_f.id]

    text = pr.render(list(states.values()), project=code_project, agent="ci:41", token=False,
                     run_url="https://github.com/o/r/actions/runs/41")
    assert text.startswith(pr.MARKER + "\n")
    rows = {line.split(" | ")[0].strip("| `"): line for line in text.splitlines() if line.startswith("| `")}
    assert "**verified**" in rows["alpha"] and "**re-run disagrees**" in rows["beta"]
    assert "**claimed only**" in rows["gamma"] and "**not reported**" in rows["delta"]
    assert "| 2 | 1 |" in rows["alpha"]  # discarded approaches, open findings
    assert "self-declared" in text and "[this workflow run](https://github.com/o/r/actions/runs/41)" in text

    # a lead's decision is what the state follows from then on
    lead.decide(claims["beta"], "refuted", "human", "the re-run failed")
    assert pr.describe(ci, found)[1].state == pr.DISAGREES


def test_markdown_keeps_names_inside_their_cells() -> None:
    s = pr.PieceState("a|b`<!--@x", "0" * 40, pr.CLAIMED, "by @someone <b>", independent=["ci|1"])
    text = pr.render([s], project="p", agent="ci:1", token=True, run_url="javascript:alert(1)")
    row = next(line for line in text.splitlines() if line.startswith("| `"))
    assert row.startswith("| `a\\|b'<!--@x` |") and row.count(" | ") == 6
    assert "@\N{ZERO WIDTH SPACE}someone" in text and "\\<b\\>" in text
    assert "javascript" not in text and "per-agent token" in text


# ------------------------------------------------------------------------------------------- the comment

FAKE_GH = '''#!{python}
"""A stand-in for gh: the issue comments API of one repository, kept in a JSON file."""
import json, os, sys

path = os.environ["FAKE_GH_STATE"]
st = json.load(open(path)) if os.path.exists(path) else {{"next": 100, "comments": [], "calls": []}}
args = sys.argv[1:]
st["calls"].append({{"args": args, "token": os.environ.get("GH_TOKEN"),
                    "db": "BENCHBOOK_DATABASE_URL" in os.environ}})
method, rest = "GET", [a for a in args[1:] if a != "--paginate"]
if "-X" in rest:
    i = rest.index("-X"); method = rest[i + 1]; del rest[i:i + 2]
data = None
if "--input" in rest:
    i = rest.index("--input"); del rest[i:i + 2]; data = json.load(sys.stdin)
parts = rest[0].split("?")[0].split("/")
login = os.environ.get("FAKE_GH_LOGIN", "github-actions[bot]")
out = None
if args[0] != "api" or os.environ.get("FAKE_GH_FAIL"):
    sys.stderr.write("HTTP 403: Resource not accessible by integration\\n"); sys.exit(1)
if method == "GET":
    mine = [c for c in st["comments"] if c["issue"] == int(parts[4])]
    print("".join(json.dumps(mine[i:i + 2]) for i in range(0, len(mine), 2)) or "[]")
elif method == "POST":
    out = {{"id": st["next"], "issue": int(parts[4]), "body": data["body"], "user": {{"login": login}}}}
    st["next"] += 1; st["comments"].append(out)
elif method == "PATCH":
    out = next(c for c in st["comments"] if c["id"] == int(parts[5])); out["body"] = data["body"]
elif method == "DELETE":
    st["comments"] = [c for c in st["comments"] if c["id"] != int(parts[5])]
if out is not None:
    print(json.dumps(out))
json.dump(st, open(path, "w"))
'''


@pytest.fixture
def gh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A stand-in gh first on PATH; returns its state file."""
    d = tmp_path / "fakebin"
    d.mkdir()
    exe = d / "gh"
    exe.write_text(FAKE_GH.format(python=sys.executable))
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    state = tmp_path / "gh-state.json"
    monkeypatch.setenv("PATH", f"{d}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_GH_STATE", str(state))
    return state


def comments(state: Path, issue: int = 7) -> list[dict[str, Any]]:
    return [c for c in json.loads(state.read_text())["comments"] if c["issue"] == issue]


def test_comment_is_created_once_and_then_updated(gh: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert pr.upsert_comment("o/r", 7, "no pieces", update_only=True) == ("skipped", None)
    assert not gh.exists() or comments(gh) == []
    what, first = pr.upsert_comment("o/r", 7, f"{pr.MARKER}\nfirst", author="github-actions[bot]")
    assert what == "created"
    what, again = pr.upsert_comment("o/r", 7, f"{pr.MARKER}\nsecond", author="github-actions[bot]")
    assert (what, again) == ("updated", first)
    assert [c["body"] for c in comments(gh)] == [f"{pr.MARKER}\nsecond"]
    assert pr.upsert_comment("o/r", 8, "other pull request")[0] == "created"  # the marker is added
    assert comments(gh, 8)[0]["body"].startswith(pr.MARKER + "\n")


def test_comment_ignores_marked_comments_of_others_and_removes_extra_copies(gh: Path,
                                                                            monkeypatch: pytest.MonkeyPatch) -> None:
    def post(body: str) -> None:
        pr._gh("gh", ["api", "-X", "POST", "repos/o/r/issues/7/comments", "--input", "-"], json.dumps({"body": body}))

    monkeypatch.setenv("FAKE_GH_LOGIN", "mallory")
    post(f"{pr.MARKER}\nall verified, trust me")
    post("a plain comment, no marker")
    monkeypatch.setenv("FAKE_GH_LOGIN", "github-actions[bot]")
    for text in ("one", "two", "three"):  # three runs that raced each other; more than one page of comments
        post(f"{pr.MARKER}\n{text}")
    what, cid = pr.upsert_comment("o/r", 7, f"{pr.MARKER}\nsummary", author="github-actions[bot]")
    assert what == "updated"
    mine = [c for c in comments(gh) if c["user"]["login"] == "github-actions[bot]"]
    assert [(c["id"], c["body"]) for c in mine] == [(cid, f"{pr.MARKER}\nsummary")]
    assert [c["body"] for c in comments(gh) if c["user"]["login"] == "mallory"] == [
        f"{pr.MARKER}\nall verified, trust me", "a plain comment, no marker"]


def test_comment_reports_what_gh_said(gh: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE_GH_FAIL", "1")
    with pytest.raises(pr.BenchbookError, match="403"):
        pr.upsert_comment("o/r", 7, "x")
    with pytest.raises(pr.BenchbookError, match="OWNER/NAME"):
        pr.upsert_comment("o/r; rm -rf /", 7, "x")


# ------------------------------------------------------------------------------------------- check verify options

def test_check_verify_takes_a_trace_ref_and_no_decide(tmp_path: Path, db_url: str, code_project: str) -> None:
    repo = tmp_path / "repo"
    shas = pr_repo(repo, code_project)
    impl = Ledger.open(project=code_project, agent="impl", database_url=db_url)
    claim = report(impl, "alpha", shas["alpha"])
    env = {**{k: v for k, v in os.environ.items() if not k.startswith(("BENCHBOOK_", "ASSAY_"))},
           "BENCHBOOK_DATABASE_URL": db_url, "BENCHBOOK_PROJECT": code_project}

    def verify(*extra: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run([str(BIN / "benchbook"), "--json", "--agent", "lead", "check", "verify", "alpha",
                               shas["alpha"], "--base", "main", "--reported-by", "impl", *extra],
                              cwd=repo, env=env, capture_output=True, text=True, timeout=120)

    Ledger.open(project=code_project, agent="lead", role="orchestrator", database_url=db_url)
    bad = verify("--trace-ref", "[1, 2]")
    assert bad.returncode == 1 and "--trace-ref: must be a JSON object" in bad.stdout
    r = verify("--trace-ref", '{"run_url": "https://ci.example/runs/9"}', "--no-decide")
    out = json.loads(r.stdout)
    assert r.returncode == 0 and out["decision"] is None and "--no-decide" in out["decision_reason"]
    assert impl.get(claim)["status"]["status"] == "proposed"  # a lead, and still nothing decided
    exp = impl.sql_internal("SELECT trace_ref FROM benchbook.experiments WHERE project_id = %s AND author = 'lead'",
                            (code_project,))
    assert exp == [{"trace_ref": {"run_url": "https://ci.example/runs/9"}}]
    assert json.loads(verify().stdout)["decision"] == "verified"  # without the flag the lead decides, as before


# ------------------------------------------------------------------------------------------- the action, end to end

def action_env(tmp_path: Path, shas: dict[str, str], **extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("BENCHBOOK_", "ASSAY_", "GITHUB_", "BB_"))}
    runner_temp = tmp_path / "runner-temp"
    runner_temp.mkdir(exist_ok=True)
    env.update({
        "PATH": f"{BIN}{os.pathsep}{env['PATH']}", "GITHUB_EVENT_NAME": "pull_request", "GITHUB_REPOSITORY": "o/r",
        "GITHUB_RUN_ID": "41", "GITHUB_RUN_ATTEMPT": "1", "GITHUB_SERVER_URL": "https://github.com",
        "GITHUB_ACTOR": "someone", "GITHUB_STEP_SUMMARY": str(tmp_path / "step-summary.md"),
        "RUNNER_TEMP": str(runner_temp), "BB_PR": "7", "BB_BASE_SHA": shas["base"], "BB_HEAD_SHA": shas["head"],
        "BB_HEAD_REPO": "o/r", "BB_INSTALL": "skip", "BB_GITHUB_TOKEN": GH_TOKEN,
        "BB_COMMENT_AUTHOR": "github-actions[bot]"})
    env.update(extra)
    return env


def run_action(repo: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["bash", str(SCRIPT)], cwd=repo, env=env, capture_output=True, text=True, timeout=300)


def attempts(lg: Ledger) -> list[dict[str, Any]]:
    return lg.sql_internal("SELECT config ->> 'piece' AS piece, author, trace_ref, duplicate_of FROM "
                           "benchbook.experiments WHERE project_id = %s ORDER BY created_at", (lg.project_id,))


def test_action_twice_on_one_pull_request_leaves_one_comment(tmp_path: Path, db_url: str, code_project: str,
                                                             gh: Path) -> None:
    repo, dump = tmp_path / "repo", tmp_path / "check-env.txt"
    shas = pr_repo(repo, code_project, env_dump=dump)
    impl = Ledger.open(project=code_project, agent="impl", database_url=db_url)
    for p in ("alpha", "beta", "gamma"):
        report(impl, p, shas[p])
    env = action_env(tmp_path, shas, BB_DATABASE_URL=db_url, BB_PROJECT=code_project)

    first = run_action(repo, env)
    assert first.returncode == 1, first.stdout + first.stderr  # beta's re-run failed: the job fails
    [c] = comments(gh)
    assert c["body"].startswith(pr.MARKER + "\n") and "actions/runs/41" in c["body"]
    for piece, state in (("alpha", "verified"), ("beta", "re-run disagrees"), ("gamma", "claimed only"),
                         ("delta", "not reported")):
        assert re.search(rf"^\| `{piece}` \| `{shas[piece][:12]}` \| \*\*{state}\*\*", c["body"], re.M), piece

    second = run_action(repo, {**env, "GITHUB_RUN_ID": "42"})
    assert second.returncode == 1
    [again] = comments(gh)
    assert again["id"] == c["id"] and "actions/runs/42" in again["body"]  # updated in place, still one

    rows = attempts(impl)  # compared as sets: created_at follows the clock, which WSL sometimes moves back
    assert sorted((r["piece"], r["author"]) for r in rows) == sorted(
        (p, f"ci:{run}") for run in (41, 42) for p in ("alpha", "beta", "gamma", "delta"))
    alpha = next(r for r in rows if r["author"] == "ci:41" and r["piece"] == "alpha")
    assert alpha["trace_ref"] == {"ci": "github-actions", "repository": "o/r", "run_id": "41", "run_attempt": "1",
                                  "run_url": "https://github.com/o/r/actions/runs/41", "pull_request": 7}
    assert all(r["duplicate_of"] for r in rows if r["author"] == "ci:42")  # the second run's name the first's
    statuses = impl.sql_internal("SELECT status FROM benchbook.v_claim_status WHERE project_id = %s AND kind = "
                                 "'completion'", (code_project,))
    assert {s["status"] for s in statuses} == {"proposed"}  # CI recorded and decided nothing
    assert pr.MARKER in (tmp_path / "step-summary.md").read_text()

    # the checks (the pull request's code) never saw the GitHub token or the ledger's URL; gh never saw the URL
    seen = dump.read_text()
    assert "BENCHBOOK_AGENT=ci:41" in seen and GH_TOKEN not in seen and "BB_GITHUB_TOKEN" not in seen
    assert "BENCHBOOK_DATABASE_URL=" not in seen and db_url not in seen
    calls = json.loads(gh.read_text())["calls"]
    assert calls and all(call["token"] == GH_TOKEN and not call["db"] for call in calls)


def test_action_under_a_token_writes_as_the_tokens_agent_and_decides_only_when_asked(
        tmp_path: Path, db_url: str, code_project: str, gh: Path) -> None:
    repo = tmp_path / "repo"
    shas = pr_repo(repo, code_project)
    impl = Ledger.open(project=code_project, agent="impl", database_url=db_url)
    claim = report(impl, "alpha", shas["alpha"])
    token = issue(db_url, code_project, "ci-lead", "orchestrator")
    # BENCHBOOK_AGENT left over in the job's environment must not fight the token
    env = action_env(tmp_path, shas, BB_DATABASE_URL=as_writer(db_url), BB_TOKEN=token, BB_PROJECT=code_project,
                     BENCHBOOK_AGENT="someone-else")
    first = run_action(repo, env)
    assert first.returncode == 1, first.stdout + first.stderr
    assert {(r["author"], r["trace_ref"]["run_id"]) for r in attempts(impl)} == {("ci-lead", "41")}  # run id kept
    assert impl.get(claim)["status"]["status"] == "proposed"  # a lead's token, and still no decision by default
    [c] = comments(gh)
    assert "per-agent token" in c["body"] and "`ci-lead`" in c["body"]

    second = run_action(repo, {**env, "BB_DECIDE": "true", "GITHUB_RUN_ID": "42"})
    assert second.returncode == 1
    assert impl.get(claim)["status"]["status"] == "verified"
    [c] = comments(gh)
    assert "decided verified (oracle rerun)" in c["body"]


def test_action_decide_without_a_token_records_only(tmp_path: Path, db_url: str, code_project: str, gh: Path) -> None:
    repo = tmp_path / "repo"
    shas = pr_repo(repo, code_project)
    impl = Ledger.open(project=code_project, agent="impl", database_url=db_url)
    claim = report(impl, "alpha", shas["alpha"])
    # even a self-declared lead name in local mode: the action names itself ci:<run_id>
    Ledger.open(project=code_project, agent="ci:41", role="orchestrator", database_url=db_url)
    r = run_action(repo, action_env(tmp_path, shas, BB_DATABASE_URL=db_url, BB_PROJECT=code_project,
                                    BB_DECIDE="true"))
    assert "decide: true needs a per-agent token" in r.stdout
    assert impl.get(claim)["status"]["status"] == "proposed"


@pytest.mark.parametrize("case", ["fork", "dependabot", "missing secret", "pull_request_target"])
def test_action_says_why_it_does_nothing(tmp_path: Path, gh: Path, case: str) -> None:
    repo = new_repo(tmp_path / "repo")
    shas = {"base": git(repo, "rev-parse", "HEAD"), "head": git(repo, "rev-parse", "HEAD")}
    env = action_env(tmp_path, shas, BB_DATABASE_URL="postgresql://u:secret-password@db.invalid/x")
    if case == "fork":
        env["BB_HEAD_REPO"] = "someone/fork"
    elif case == "dependabot":
        env.update(BB_DATABASE_URL="", GITHUB_ACTOR="dependabot[bot]")
    elif case == "missing secret":
        env["BB_DATABASE_URL"] = ""
    else:
        env["GITHUB_EVENT_NAME"] = "pull_request_target"
    r = run_action(repo, env)
    if case in ("fork", "dependabot"):
        assert r.returncode == 0 and "::notice title=benchbook::" in r.stdout and "no secrets" in r.stdout
    else:
        assert r.returncode == 1 and "::error title=benchbook::" in r.stdout
    assert not gh.exists()  # nothing was commented
    assert "secret-password" not in r.stdout + r.stderr


def test_action_files_keep_inputs_out_of_the_script_text() -> None:
    action = (ACTION / "action.yml").read_text()
    assert "using: composite" in action and 'run: bash "$GITHUB_ACTION_PATH/run.sh"' in action
    run_lines = [line for line in action.splitlines() if line.strip().startswith("run:")]
    assert len(run_lines) == 1 and "${{" not in run_lines[0]
    script = SCRIPT.read_text()
    assert os.access(SCRIPT, os.X_OK) and "set -x" not in script.replace("never `set -x`", "")
    assert "--no-decide" in script and "check verify" in script
    readme = (ACTION / "README.md").read_text()
    sample = readme.split("```yaml\n", 1)[1].split("```", 1)[0]
    for needle in ("pull_request:", "pull-requests: write", "contents: read", "fetch-depth: 0",
                   "persist-credentials: false", "concurrency:", "secrets.BENCHBOOK_DATABASE_URL",
                   "secrets.BENCHBOOK_TOKEN"):
        assert needle in sample, needle
    assert "pull_request_target" not in "\n".join(line.split("#", 1)[0] for line in sample.splitlines())
    assert "branch protection" in readme.lower() and "required" in readme.lower()
