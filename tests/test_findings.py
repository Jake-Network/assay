"""Out-of-scope findings captured from an implementer's final report (spec §8 P1, docs/code.md): the parser on
real-shaped SubagentStop and SubagentHandback inputs, the claims the hook writes, dedup across reports, the token
path, and the accept/reject commands with the open-findings query of docs/code.md."""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from benchbook import Ledger, config, findings, hooks
from benchbook.config import write_config
from benchbook.core.brief import compile_brief
from benchbook.errors import BenchbookError
from benchbook.findings import FINDING_DOMAIN, finding_hash, parse
from benchbook.hooks import extract_text, process

from .conftest import make_project
from .writer import as_writer, issue

FX = Path(__file__).parent / "fixtures" / "hooks" / "findings"
DOC = Path(__file__).resolve().parent.parent / "docs" / "code.md"
THUMB = "src/app/modules/media/thumbnail.py"


def _event(name: str, cwd: Path) -> dict[str, Any]:
    return json.loads((FX / name).read_text(encoding="utf-8").replace("{CWD}", str(cwd)))  # type: ignore[no-any-return]


def _repo(path: Path, project: str, heading: str | None) -> Path:
    write_config(path, project)
    if heading is not None:
        with (path / ".benchbook" / "config.toml").open("a", encoding="utf-8") as f:
            f.write(f'\n[report]\nout_of_scope_heading = "{heading}"\n')
    return path


def _findings(lg: Ledger) -> list[dict[str, Any]]:
    return lg.sql_internal("SELECT c.*, s.status, s.n_support FROM benchbook.claims c"
                           " JOIN benchbook.v_claim_status s ON s.id = c.id"
                           " WHERE c.project_id = %s AND 'out-of-scope' = ANY(c.tags) ORDER BY c.created_at",
                           (lg.project_id,))


def _notes(lg: Ledger) -> list[dict[str, Any]]:
    return lg.sql_internal("SELECT * FROM benchbook.claims WHERE project_id = %s AND 'auto-captured' = ANY(tags)",
                           (lg.project_id,))


def _by_file(rows: list[dict[str, Any]], path: str) -> dict[str, Any]:
    [row] = [r for r in rows if r["payload"].get("file") == path]
    return row


@pytest.fixture
def code_project(db_url: str) -> str:
    return make_project(db_url, metric="checks_passed", direction="max", pack="code",
                        settings={"partition_key": "piece"})


@pytest.fixture
def lead(db_url: str, code_project: str) -> Ledger:
    return Ledger.open(project=code_project, agent="lead", database_url=db_url)


@pytest.fixture
def ko_repo(tmp_path: Path, code_project: str) -> Path:
    return _repo(tmp_path, code_project, "범위 밖")


# ---------------------------------------------------------------------- the parser


def test_korean_report_with_nested_items_and_a_file(tmp_path: Path) -> None:
    found = parse(extract_text(_event("subagent_stop_ko.json", tmp_path)), "범위 밖")
    assert [f.text for f in found] == [
        f"`{THUMB}:88`의 리사이즈가 EXIF 회전을 무시합니다.",
        "인증 토큰 갱신이 로그아웃과 경쟁합니다. 재현은 못 했고 로그에서 두 번 봤습니다.",  # a wrapped line
        "mod:billing 청구서 합계가 할인 적용 전 금액으로 반올림됩니다",
    ]
    # nested items are details of their parent, not findings; the item's own path wins over the details' one
    assert found[0].details.splitlines() == ["- 세로 사진이 눕혀진 채 저장됩니다.",
                                             "- tests/media/test_thumbnail.py에는 이 경우가 없습니다."]
    assert found[0].paths == (THUMB,) and found[0].file == THUMB
    assert found[1].paths == () and found[1].details == ""
    assert found[2].modules == ("mod:billing",) and found[0].modules == ()
    # "## 변경 내용" and "## 다음 단계" are other sections
    assert not any("리뷰" in f.text or "해시 비교" in f.text for f in found)


def test_english_bold_heading_numbered_items(tmp_path: Path) -> None:
    found = parse(extract_text(_event("subagent_stop_en.json", tmp_path)), "Out of scope")
    assert [f.file for f in found] == [THUMB, "scripts/seed.sh", None]
    assert "ERROR: relation" in found[1].details and found[1].text.endswith("fails on a clean database:")
    # prose is not a path: "Read/write", "os.path", "hooks.process"
    assert found[2].paths == ()
    assert not any("Nothing else" in f.text for f in found)  # the next bold line ends the section


def test_no_section_and_empty_sections(tmp_path: Path) -> None:
    assert parse(extract_text(_event("subagent_stop_no_section.json", tmp_path)), "범위 밖") == []
    # a heading followed by the next heading, and a bold section that only says "없음"
    assert parse(extract_text(_event("subagent_stop_empty_section.json", tmp_path)), "범위 밖") == []
    assert parse("## 범위 밖\n- \n- N/A\n- None.\n", "범위 밖") == []


def test_section_rules() -> None:
    assert [f.text for f in parse("### 범위 밖:\n- a\n", "범위 밖")] == ["a"]
    assert parse("## 범위 밖 발견\n- a\n", "범위 밖") == []  # the heading must be the configured one
    assert parse("## Out of scope\n- a\n", "범위 밖") == []
    text = "## Out of scope\n- a\n### media\n- b\n---\n- c\n"  # a deeper heading groups; a break ends
    assert [f.text for f in parse(text, "out of scope")] == ["a", "b"]
    text = "## Out of scope\n```\n## Next\n- in a fence\n```\n- d\n\nA closing paragraph.\n- e\n"
    assert [f.text for f in parse(text, "Out of scope")] == ["d", "e"]
    assert [f.text for f in parse("**Out of scope**:\n- f\n## Next\n- g\n", "Out of Scope")] == ["f"]
    assert [f.text for f in parse("## Out of scope\n- [ ] h\n- h\n", "Out of scope")] == ["h"]  # one per key


def test_paths(tmp_path: Path) -> None:
    root = str(tmp_path)
    [f] = parse(f"## X\n- {root}/src/a.py:12 and ./lib/b.ts#L3 and /etc/passwd and ../up.py\n", "X", root)
    assert f.paths == ("src/a.py", "lib/b.ts")
    [f] = parse("## X\n- see hooks.py, config.toml, `src/media` and src/app/modules/media/\n", "X")
    assert f.paths == ("src/media", "hooks.py", "config.toml", "src/app/modules/media/")
    [f] = parse("## X\n- 설정은 config.py에서 읽고 https://example.com/a/b.py 는 링크입니다\n", "X")
    assert f.paths == ("config.py",)


def test_dedup_key() -> None:
    a = finding_hash(THUMB, f"`{THUMB}:88`의 리사이즈가 EXIF 회전을 무시합니다.")
    assert a == finding_hash(THUMB, f"{THUMB}:88의  리사이즈가 exif 회전을 무시합니다")
    assert a != finding_hash(None, f"{THUMB}:88의 리사이즈가 EXIF 회전을 무시합니다")
    assert a != finding_hash(THUMB, f"{THUMB}:89의 리사이즈가 EXIF 회전을 무시합니다")
    # the stored domains never change: the auto-capture one keeps its pre-rename name, the finding one is pinned
    assert FINDING_DOMAIN == b"benchbook:out-of-scope\0" and hooks.CAPTURE_DOMAIN == b"assay:auto-captured\0"
    assert finding_hash("a.py", "**X** y.") == hashlib.sha256(FINDING_DOMAIN + b"a.py\0x y").hexdigest()


# ---------------------------------------------------------------------- config


def test_report_config(tmp_path: Path) -> None:
    assert config.load(_repo(tmp_path / "a", "p", "범위 밖")).out_of_scope_heading == "범위 밖"
    assert config.load(_repo(tmp_path / "b", "p", None)).out_of_scope_heading is None
    assert config.parse_report(None) is None and config.parse_report({}) is None
    for table, words in (({"out_of_scope": "x"}, "unknown key out_of_scope"), ({"out_of_scope_heading": 1}, "one line"),
                         ({"out_of_scope_heading": " "}, "one line"), ({"out_of_scope_heading": "a\nb"}, "one line"),
                         ("x", "must be a table")):
        with pytest.raises(BenchbookError, match=words):
            config.parse_report(table)


def test_report_config_imports_nothing_heavy(tmp_path: Path) -> None:
    """The hook entry loads config.toml at every worker stop: [report] must not pull the database layer in."""
    _repo(tmp_path, "p", "범위 밖")
    code = ("import sys; from pathlib import Path; from benchbook import config; c = config.load(Path(sys.argv[1]));"
            "assert c.out_of_scope_heading == '범위 밖';"
            "print(sorted(m for m in sys.modules if m.split('.')[0] in ('psycopg', 'typer', 'jsonschema', 'pydantic')"
            " or m.startswith(('benchbook.core.ledger', 'benchbook.db', 'benchbook.findings'))))")
    p = subprocess.run([sys.executable, "-c", code, str(tmp_path)], capture_output=True, text=True, check=True)
    assert p.stdout.strip() == "[]"


# ---------------------------------------------------------------------- what the hook records


def test_each_item_becomes_one_claim(ko_repo: Path, lead: Ledger) -> None:
    data = _event("subagent_stop_ko.json", ko_repo)
    text = extract_text(data)
    assert process("subagent-stop", data, accepted=True).startswith("captured:c:")  # the note, as before
    rows = _findings(lead)
    assert len(rows) == len(parse(text, "범위 밖")) == 3
    assert len(_notes(lead)) == 1
    for r in rows:
        assert (r["kind"], r["author"], r["status"], r["n_support"]) == ("observation", "hook", "proposed", 1)
        assert r["payload"]["source"] == {"session_id": "9b1f3c2e-sess", "event": "SubagentStop",
                                          "agent_id": "a71c0d", "agent_type": "implementer"}
        assert r["payload"]["heading"] == "범위 밖"
    thumb = _by_file(rows, THUMB)
    assert thumb["tags"] == ["out-of-scope", f"file:{THUMB}"]
    assert thumb["statement"] == f"`{THUMB}:88`의 리사이즈가 EXIF 회전을 무시합니다."
    assert thumb["payload"]["details"].startswith("- 세로 사진이")
    assert [r["tags"] for r in rows if "billing" in r["statement"]] == [["out-of-scope", "mod:billing"]]
    # the report is one source, supporting every finding it names
    [src] = lead.sql_internal("SELECT * FROM benchbook.sources WHERE project_id = %s", (lead.project_id,))
    digest = hashlib.sha256(text.encode()).hexdigest()
    assert (src["uri"], src["sha256"]) == (f"subagent-report:sha256:{digest}", digest)
    assert "implementer" in src["title"] and "a71c0d" in src["title"]
    links = lead.sql_internal("SELECT dst_id FROM benchbook.links WHERE project_id = %s AND src_id = %s"
                              " AND relation = 'supports'", (lead.project_id, src["id"]))
    assert {x["dst_id"] for x in links} == {r["id"] for r in rows}


def test_a_repeated_item_adds_evidence_not_a_claim(ko_repo: Path, lead: Ledger) -> None:
    first, second = _event("subagent_stop_ko.json", ko_repo), _event("subagent_stop_repeat.json", ko_repo)
    process("subagent-stop", first, accepted=True)
    process("subagent-stop", second, accepted=True)
    distinct = {f.key for d in (first, second) for f in parse(extract_text(d), "범위 밖")}
    rows = _findings(lead)
    assert len(rows) == len(distinct) == 4  # 3 + the cdn finding; the thumbnail one twice, once within a report
    thumb = _by_file(rows, THUMB)
    assert thumb["n_support"] == 2 and thumb["payload"]["source"]["agent_id"] == "a71c0d"  # first reporter kept
    assert _by_file(rows, "src/app/modules/media/cdn.py")["n_support"] == 1
    # a retried report (a replay after a transient failure) writes nothing new
    process("subagent-stop", second, accepted=True)
    assert [(r["id"], r["n_support"]) for r in _findings(lead)] == [(r["id"], r["n_support"]) for r in rows]
    assert len(lead.sql_internal("SELECT id FROM benchbook.sources WHERE project_id = %s", (lead.project_id,))) == 2


def test_no_section_or_an_empty_one_records_no_finding(ko_repo: Path, lead: Ledger) -> None:
    for name in ("subagent_stop_no_section.json", "subagent_stop_empty_section.json"):
        assert process("subagent-stop", _event(name, ko_repo), accepted=True).startswith("captured:c:")
    assert _findings(lead) == [] and len(_notes(lead)) == 2
    assert lead.sql_internal("SELECT id FROM benchbook.sources WHERE project_id = %s", (lead.project_id,)) == []


def test_other_packs_and_an_unset_heading_change_nothing(tmp_path: Path, db_url: str, code_project: str,
                                                         lead: Ledger) -> None:
    core = make_project(db_url)
    core_lg = Ledger.open(project=core, agent="lead", database_url=db_url)
    for repo, lg in ((_repo(tmp_path / "core", core, "범위 밖"), core_lg),
                     (_repo(tmp_path / "unset", code_project, None), lead)):
        assert process("subagent-stop", _event("subagent_stop_ko.json", repo), accepted=True).startswith("captured:")
        assert _findings(lg) == [] and len(_notes(lg)) == 1


def test_piece_comes_from_the_runs_own_attempts(ko_repo: Path, lead: Ledger, code_project: str,
                                               db_url: str) -> None:
    """A report that cites the attempt it recorded is not captured as a note, but its findings are, tagged with the
    piece of that attempt."""
    impl = Ledger.open(project=code_project, agent="impl-1", database_url=db_url)
    e = impl.record_experiment({"piece": "media-upload-dedupe", "checks": ["unit"]},
                               {"checks_passed": 1, "checks_total": 1}, code_ref="3f9a2c1")
    ref = f"e:{str(e.id)[:8]}"
    start = datetime.now(UTC) - timedelta(minutes=1)
    rows = [{"timestamp": start.isoformat(), "message": {"content": [
                {"type": "tool_use", "id": "b1", "name": "Bash",
                 "input": {"command": "benchbook --agent impl-1 exp run --config '{}' -- uv run pytest"}}]}},
            {"timestamp": (start + timedelta(seconds=5)).isoformat(), "message": {"content": [
                {"type": "tool_result", "tool_use_id": "b1", "content": f"benchbook: recorded {ref} improved"}]}}]
    transcript = ko_repo / "agent-a71c0d.jsonl"
    transcript.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    data = _event("subagent_stop_ko.json", ko_repo)
    data["agent_transcript_path"] = str(transcript)
    data["last_assistant_message"] = f"Recorded {ref}.\n\n" + data["last_assistant_message"]
    assert process("subagent-stop", data, accepted=True) == "cited"
    found = _findings(lead)
    assert len(found) == 3 and _notes(lead) == []
    assert all("piece:media-upload-dedupe" in r["tags"] and r["payload"]["piece"] == "media-upload-dedupe"
               for r in found)


def test_piece_comes_from_the_reports_commit(tmp_path: Path, lead: Ledger, code_project: str) -> None:
    """PR #4 issue 3: an implementer that records no attempt and reports `커밋: <sha>` gets its findings tagged with
    the piece the automatic re-run takes from that commit (verify_requests.reported_target), so the piece's
    `--here` brief shows them."""
    from benchbook.core.brief import compile_brief
    from benchbook.packs.code.brief import changed_scope

    def git(*args: str) -> str:
        return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                              text=True).stdout.strip()

    repo = tmp_path / "repo"
    repo.mkdir()
    git("init", "-q", "-b", "main")
    for k, v in (("user.email", "t@example.com"), ("user.name", "t"), ("commit.gpgsign", "false")):
        git("config", k, v)
    _repo(repo, code_project, "범위 밖")
    with (repo / ".benchbook" / "config.toml").open("a", encoding="utf-8") as f:
        f.write('\n[guard]\nbase = "main"\n')
    git("add", "-A")
    git("commit", "-qm", "base")
    git("checkout", "-qb", "hook-piece")
    (repo / "src" / "app" / "utils").mkdir(parents=True)
    (repo / "src" / "app" / "utils" / "sku.py").write_text("def split(s):\n    return s.split('-')\n")
    git("add", "-A")
    git("commit", "-qm", "sku")
    sha = git("rev-parse", "HEAD")
    data = _event("subagent_stop_one_line.json", repo)
    data["last_assistant_message"] = data["last_assistant_message"] + f"커밋: {sha}\n"
    assert process("subagent-stop", data, accepted=True, findings_only=True) == "findings-only"
    [row] = _findings(lead)
    assert "piece:hook-piece" in row["tags"] and row["payload"]["piece"] == "hook-piece"
    brief = compile_brief(lead, None, 2000, log=False, tags=changed_scope(repo), repo=repo)
    assert "Scope: piece:hook-piece" in brief
    findings = brief.split("## Open out-of-scope findings", 1)[1].split("\n## ", 1)[0]
    assert findings.startswith(" (1 of 1)") and "publish/client.py" in findings


def test_the_handback_carries_the_findings_once(tmp_path: Path, code_project: str, lead: Ledger,
                                               monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _repo(tmp_path, code_project, "Out of scope")
    monkeypatch.setenv("XDG_STATE_HOME", str(repo / "state"))
    spool = hooks._spool_dir()
    spool.mkdir(parents=True)
    back = _event("handback.json", repo)
    stop = {**_event("subagent_stop_en.json", repo), "session_id": back["session_id"], "agent_id": back["agent_id"]}
    for data in (stop, back):  # the closing SubagentStop is queued first and must not add its own section
        hooks._write_spool(spool, {"event": "subagent-stop", "project": code_project, "data": data,
                                   "accepted": True, "out_of_scope_heading": "Out of scope"})
        time.sleep(0.05)
    s = hooks.replay_now(spool)
    assert (s.captured, s.skipped, s.ok) == (1, 1, True)
    [row] = _findings(lead)
    assert row["tags"] == ["out-of-scope", "file:src/app/modules/export/csv.py"]
    assert row["payload"]["source"]["tool_name"] == "SubagentHandback"


def test_the_heading_is_kept_with_the_queued_report(ko_repo: Path, code_project: str, lead: Ledger,
                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(ko_repo / "state"))
    monkeypatch.setenv("BENCHBOOK_HOOK_AGENTS", "implementer")
    monkeypatch.setattr(hooks.subprocess, "Popen", lambda *a, **k: None)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(_event("subagent_stop_ko.json", ko_repo))))
    hooks.main("subagent-stop")
    spool = hooks._spool_dir()
    [entry] = spool.glob("*.json")
    assert json.loads(entry.read_text(encoding="utf-8"))["out_of_scope_heading"] == "범위 밖"
    _repo(ko_repo, code_project, None)  # the heading is removed before the replay: the run's setting still holds
    assert hooks.replay_now(spool).captured == 1
    assert len(_findings(lead)) == 3
    # a report queued without the heading (an earlier build, or the heading unset) records no finding
    hooks._write_spool(spool, {"event": "subagent-stop", "project": code_project, "accepted": True,
                               "data": _event("subagent_stop_repeat.json", ko_repo)})
    assert hooks.replay_now(spool).captured == 1 and len(_findings(lead)) == 3
    assert "out-of-scope findings: 3 (3 new)" in (hooks._state_dir() / "hook.log").read_text(encoding="utf-8")


def test_under_a_token_the_hook_writes_as_its_agent(ko_repo: Path, code_project: str, lead: Ledger, db_url: str,
                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    """Dedup does not depend on the author: a finding first recorded in local mode (author hook) gets the report
    written under a token as evidence, and a new finding is authored by the token's agent while its payload still
    names the subagent."""
    process("subagent-stop", _event("subagent_stop_ko.json", ko_repo), accepted=True)
    monkeypatch.delenv("BENCHBOOK_AGENT", raising=False)
    monkeypatch.setenv("BENCHBOOK_DATABASE_URL", as_writer(db_url))
    monkeypatch.setenv("BENCHBOOK_TOKEN", issue(db_url, code_project, "lead-2", "orchestrator"))
    assert process("subagent-stop", _event("subagent_stop_repeat.json", ko_repo), accepted=True).startswith("captured")
    rows = _findings(lead)
    assert len(rows) == 4
    thumb, cdn = _by_file(rows, THUMB), _by_file(rows, "src/app/modules/media/cdn.py")
    assert (thumb["author"], thumb["n_support"]) == ("hook", 2)
    assert cdn["author"] == "lead-2" and cdn["payload"]["source"]["agent_id"] == "c9d8e7"
    authors = lead.sql_internal("SELECT author FROM benchbook.sources WHERE project_id = %s ORDER BY created_at",
                                (lead.project_id,))
    assert [a["author"] for a in authors] == ["hook", "lead-2"]


# ---------------------------------------------------------------------- deciding, and the query of docs/code.md


def _doc_queries() -> list[str]:
    section = DOC.read_text(encoding="utf-8").split("### Reading open findings", 1)[1]
    return re.findall(r"```sql\n(.*?)```", section, re.S)


def test_decisions_from_the_cli_and_the_open_findings_query(ko_repo: Path, lead: Ledger, db_url: str) -> None:
    process("subagent-stop", _event("subagent_stop_ko.json", ko_repo), accepted=True)
    process("subagent-stop", _event("subagent_stop_repeat.json", ko_repo), accepted=True)
    rows = _findings(lead)
    thumb, cdn = _by_file(rows, THUMB), _by_file(rows, "src/app/modules/media/cdn.py")
    tid, cid = f"c:{str(thumb['id'])[:8]}", f"c:{str(cdn['id'])[:8]}"
    module, counts = _doc_queries()

    def open_in_media() -> set[str]:
        return {f"c:{r['id'][:8]}" for r in lead.sql(module)["rows"]}

    assert open_in_media() == {tid, cid}
    env = {**os.environ, "BENCHBOOK_DATABASE_URL": db_url}
    env.pop("BENCHBOOK_PROJECT", None)

    def cli(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, "-m", "benchbook.cli", *args], capture_output=True, text=True,
                              cwd=ko_repo, env=env, timeout=60)

    url = "https://github.com/org/repo/issues/12"
    r = cli("--json", "finding", "accept", tid, "--issue", url, "--oracle", "human", "--as", "human")
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert out["status"] == "supported" and out["claim"] == tid
    shown = lead.get(tid)
    assert shown["status"]["status"] == "supported"
    assert {(x["relation"], x["src"]) for x in shown["links_in"]} >= {("cites", out["source"])}
    [d] = shown["decisions"]
    assert (d["oracle"], d["by"], d["rationale"]) == ("human", "human", f"accepted, follow-up {url}")
    [src] = lead.sql_internal("SELECT uri, title FROM benchbook.sources WHERE id::text LIKE %s",
                              (out["source"][2:] + "%",))
    assert src == {"uri": url, "title": "follow-up issue"}
    assert open_in_media() == {cid}  # decided: no longer listed for its module
    r = cli("decide", cid, "refuted", "--oracle", "human", "--rationale", "signed URLs are cached on purpose",
            "--as", "human")
    assert r.returncode == 0, r.stderr
    assert open_in_media() == set()
    assert lead.sql(counts)["rows"] == [{"open": 2, "accepted": 1, "rejected": 1}]


def test_add_explicit_files_are_preserved_in_a_file_scoped_brief(ko_repo: Path, lead: Ledger, db_url: str) -> None:
    files = ["Dockerfile", "app/users/[id]/page.tsx", "test data/case 1.txt"]
    sentence = "The build container includes debug tools"
    env = {**os.environ, "BENCHBOOK_DATABASE_URL": db_url}
    env.pop("BENCHBOOK_PROJECT", None)
    args = [arg for file in files for arg in ("--file", file)]
    result = subprocess.run([sys.executable, "-m", "benchbook.cli", "--agent", "lead", "finding", "add",
                             sentence, *args], capture_output=True, text=True, cwd=ko_repo, env=env, timeout=60)
    assert result.returncode == 0, result.stderr
    [finding] = _findings(lead)
    assert finding["statement"] == sentence and finding["payload"]["file"] == files[0]
    assert set(finding["tags"]) == {"out-of-scope", *(f"file:{file}" for file in files)}
    for file in files:
        assert sentence in compile_brief(lead, None, 2000, log=False, tags=[f"file:{file}"])


def test_add_explicit_file_already_in_prose_keeps_the_report_identity(lead: Ledger) -> None:
    path = "src/loader.py"
    sentence = f"`{path}` still caches errors"
    captured = findings.capture(lead, f"## Out of scope\n- {sentence}\n", "Out of scope", source={})
    noted = findings.record_noted(lead, sentence, [f"./{path}", "Dockerfile"])
    assert noted.claims == captured.claims
    assert captured.new == 1 and noted.new == 0
    [finding] = _findings(lead)
    assert finding["n_support"] == 2
    assert sentence in compile_brief(lead, None, 2000, log=False, tags=["file:Dockerfile"])
    assert sentence not in compile_brief(lead, None, 2000, log=False, tags=["file:Elsewhere"])


def test_accept_refuses_before_writing(ko_repo: Path, lead: Ledger, db_url: str) -> None:
    process("subagent-stop", _event("subagent_stop_ko.json", ko_repo), accepted=True)
    finding = f"c:{str(_by_file(_findings(lead), THUMB)['id'])[:8]}"
    other = lead.propose_claim("observation", "not a finding")
    env = {**os.environ, "BENCHBOOK_DATABASE_URL": db_url}
    env.pop("BENCHBOOK_PROJECT", None)

    def accept(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, "-m", "benchbook.cli", "finding", "accept", *args],
                              capture_output=True, text=True, cwd=ko_repo, env=env, timeout=60)

    sources = "SELECT count(*) AS n FROM benchbook.sources WHERE project_id = %s"
    before = lead.sql_internal(sources, (lead.project_id,))
    url = "https://example.com/issues/1"
    for args, words in (([finding, "--issue", url, "--oracle", "human", "--agent", "impl-1"], "only orchestrator"),
                        ([f"c:{str(other.id)[:8]}", "--issue", url, "--oracle", "human", "--as", "human"],
                         "not an out-of-scope finding"),
                        ([finding, "--issue", "issue 12", "--oracle", "human", "--as", "human"], "--issue"),
                        ([finding, "--issue", url, "--oracle", "vibes", "--as", "human"], "--oracle")):
        r = accept(*args)
        assert r.returncode == 1 and words in r.stderr, (args, r.stderr)
    assert lead.sql_internal(sources, (lead.project_id,)) == before
    lead.decide(finding, "refuted", "human", "not a bug")
    r = accept(finding, "--issue", url, "--oracle", "human", "--as", "human")
    assert r.returncode == 1 and "already decided (refuted)" in r.stderr
    assert lead.sql_internal(sources, (lead.project_id,)) == before


# ---------------------------------------------------------------------- one-line reports and [report] agents (T05)

@pytest.mark.parametrize("line", [
    "범위 밖: `src/a.py`의 timeout 무시",
    "- 범위 밖: `src/a.py`의 timeout 무시",
    "**범위 밖:** `src/a.py`의 timeout 무시",
    "**범위 밖**: `src/a.py`의 timeout 무시",
    "범위 밖：`src/a.py`의 timeout 무시",
])
def test_a_one_line_finding(line: str) -> None:
    [f] = parse(f"done at a1b2c3d.\n{line}\n", "범위 밖")
    assert f.text == "`src/a.py`의 timeout 무시" and f.file == "src/a.py"


@pytest.mark.parametrize("line", ["범위 밖: 없음", "범위 밖:", "## 범위 밖: x", "범위: x"])
def test_one_line_forms_that_are_not_findings(line: str) -> None:
    assert parse(f"{line}\n", "범위 밖") == []


def test_report_agents_config(tmp_path: Path) -> None:
    assert config.parse_report_agents({"agents": ["*"]}) == ["*"]
    assert config.parse_report_agents({"out_of_scope_heading": "x"}) == [] == config.parse_report_agents(None)
    for bad in ("*", [""], [1]):
        with pytest.raises(BenchbookError, match="agents must be a list"):
            config.parse_report_agents({"agents": bad})
    assert config.parse_report({"out_of_scope_heading": "x", "agents": ["*"]}) == "x"


def test_report_agents_take_findings_without_a_note(ko_repo: Path, lead: Ledger) -> None:
    data = _event("subagent_stop_one_line.json", ko_repo)
    assert process("subagent-stop", data, accepted=True, findings_only=True) == "findings-only"
    [row] = _findings(lead)
    assert row["payload"]["file"] == "src/app/modules/publish/client.py" and _notes(lead) == []


def test_the_hook_spools_a_listed_agents_report_as_findings_only(ko_repo: Path, tmp_path: Path,
                                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    with (ko_repo / ".benchbook" / "config.toml").open("a", encoding="utf-8") as f:
        f.write('agents = ["*"]\n')
    monkeypatch.delenv("BENCHBOOK_HOOK_AGENTS", raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(hooks.subprocess, "Popen", lambda *a, **k: None)
    data = _event("subagent_stop_one_line.json", ko_repo)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(data)))
    hooks._main("subagent-stop", None)
    [entry] = [json.loads(p.read_text()) for p in (tmp_path / "state").rglob("*.json")
               if "findings_only" in p.read_text()]
    assert entry["findings_only"] is True and entry["out_of_scope_heading"] == "범위 밖"
    # set explicitly, BENCHBOOK_HOOK_AGENTS still decides alone
    monkeypatch.setenv("BENCHBOOK_HOOK_AGENTS", "benchbook-worker")
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({**data, "agent_id": "other"})))
    hooks._main("subagent-stop", None)
    assert not [p for p in (tmp_path / "state").rglob("*.json") if "other" in p.read_text()]
