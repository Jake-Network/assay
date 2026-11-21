"""An install from before the rename (assay) keeps working: ASSAY_* environment variables, the .assay config folder,
the default data and state directories, the `assay` command, the worker's agent type, and identifiers that existing
ledgers, transcripts and files carry. Everything under the home directory is redirected to tmp_path."""
from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
import tomllib
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg
import pytest

import benchbook
from benchbook import Ledger, config, doctor, hooks
from benchbook import ledger as ledger_mod
from benchbook.blobs import LEGACY_BLOB_PREFIX, artifact_blob, blob_digest, is_blob_uri
from benchbook.db import conn as dbconn
from benchbook.integrations.autoresearch import ops
from benchbook.integrations.autoresearch.checkout import Checkout

ROOT = Path(__file__).resolve().parent.parent
BIN = Path(sys.executable).parent
SIM = ROOT / "examples" / "autoresearch-sim"
OURS = ("DATABASE_URL", "DATA_DIR", "PROJECT", "AGENT", "HOOK_AGENTS", "AR_HW", "SERVE_TOKEN")


@pytest.fixture(autouse=True)
def like_a_new_process(monkeypatch: pytest.MonkeyPatch) -> None:
    """Nothing said yet, notices on stderr, not started as `assay`: other tests in this process may have changed it."""
    monkeypatch.setattr(config, "_told", set())
    monkeypatch.setattr(config, "_held", None)
    monkeypatch.setattr(config, "LEGACY_COMMAND", False)


def only_legacy(monkeypatch: pytest.MonkeyPatch, **values: str) -> None:
    """This process's environment as an install from before the rename has it: ASSAY_* names only."""
    for name in OURS:
        monkeypatch.delenv("BENCHBOOK_" + name, raising=False)
        monkeypatch.delenv("ASSAY_" + name, raising=False)
    for name, value in values.items():
        monkeypatch.setenv("ASSAY_" + name, value)


def legacy_environ(**values: str) -> dict[str, str]:
    """The same for a subprocess."""
    env = {k: v for k, v in os.environ.items() if not k.startswith(("BENCHBOOK_", "ASSAY_"))}
    env.update({"ASSAY_" + name: value for name, value in values.items()})
    return env


def legacy_config(root: Path, project: str = "old-project") -> Path:
    p = root / ".assay" / "config.toml"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f'project = "{project}"\ndefault_budget_tokens = 1234\n', encoding="utf-8")
    return p


def run(cmd: list[Any], **kw: Any) -> subprocess.CompletedProcess[str]:
    return subprocess.run([str(c) for c in cmd], capture_output=True, text=True, timeout=120, **kw)


def notices(stderr: str) -> list[str]:
    return [line for line in stderr.splitlines() if line.startswith("benchbook: ")]


# ---------------------------------------------------------------------- 1. environment variables


def test_env_prefers_the_new_name_and_says_once_that_the_old_one_was_read(
        monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    only_legacy(monkeypatch, PROJECT="old", AGENT="w")
    assert config.env("PROJECT") == "old" and config.env("AGENT") == "w" and config.env("PROJECT") == "old"
    [line] = capsys.readouterr().err.splitlines()  # once per process, not once per read, naming every old variable
    assert line.startswith("benchbook: ASSAY_AGENT, ASSAY_PROJECT: ") and "BENCHBOOK_AGENT, BENCHBOOK_PROJECT" in line
    monkeypatch.setenv("BENCHBOOK_PROJECT", "new")
    assert config.env("PROJECT") == "new" and config.env_name("PROJECT") == "BENCHBOOK_PROJECT"
    monkeypatch.setenv("BENCHBOOK_PROJECT", "")  # empty counts as unset, as it always did
    assert config.env("PROJECT") == "old"
    assert config.env("SERVE_TOKEN") is None and config.env("SERVE_TOKEN", "d") == "d"
    assert config.env("AR_HW", environ={"ASSAY_AR_HW": "h100"}) == "h100"
    assert config.env("AR_HW", environ={"ASSAY_AR_HW": "h100", "BENCHBOOK_AR_HW": "a100"}) == "a100"
    assert capsys.readouterr().err == ""


def test_held_notices_stay_off_stderr(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    only_legacy(monkeypatch, PROJECT="old")
    config.hold_notices()
    assert config.env("PROJECT") == "old"
    assert capsys.readouterr().err == ""
    [note] = config.take_notices()
    assert "ASSAY_PROJECT" in note and config.take_notices() == []


def test_no_direct_reads_of_our_variables_are_left() -> None:
    """Every read goes through config.env, or the fallback to ASSAY_* silently does not apply to it."""
    import re

    direct = re.compile(r"""(environ(\.get\(|\[)|getenv\()\s*["'](BENCHBOOK|ASSAY)_""")
    setting = re.compile(r"""environ\[["']\w+["']\]\s*=(?!=)""")  # a variable set for child processes is not a read
    # `benchbook exp run` sets these for the command it runs, under the new name only, and the example script that
    # reads them must also run without benchbook installed
    own = re.compile(r"BENCHBOOK_EXP_(CONFIG|SEED)")
    hits = [f"{p.relative_to(ROOT)}:{n}" for top in ("src", "bench", "examples")
            for p in sorted((ROOT / top).rglob("*.py"))
            for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1)
            if direct.search(line) and not setting.search(line) and not own.search(line)]
    assert hits == []


# ---------------------------------------------------------------------- 2. config folder


def test_find_config_reads_the_pre_rename_folder_and_marks_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    only_legacy(monkeypatch)
    repo = tmp_path / "repo"
    sub = repo / "a" / "b"
    sub.mkdir(parents=True)
    old = legacy_config(repo)
    assert config.find_config(sub) == old
    cfg = config.load(sub)
    assert (cfg.project, cfg.default_budget_tokens, cfg.path, cfg.legacy) == ("old-project", 1234, old, True)
    new = repo / ".benchbook" / "config.toml"  # at the same level .benchbook wins
    new.parent.mkdir()
    new.write_text('project = "new-project"\n')
    cfg = config.load(sub)
    assert (cfg.project, cfg.path, cfg.legacy) == ("new-project", new, False)
    nearer = legacy_config(repo / "a", "nearer")  # the search order is unchanged: the nearest folder first
    cfg = config.load(sub)
    assert (cfg.project, cfg.path, cfg.legacy) == ("nearer", nearer, True)


def test_linked_worktree_finds_the_main_worktrees_pre_rename_config(tmp_path: Path,
                                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    only_legacy(monkeypatch)
    main = tmp_path / "main"
    main.mkdir()
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid"]
    for args in (["init", "-q"], ["commit", "-q", "--allow-empty", "-m", "root"],
                 ["worktree", "add", "-q", str(tmp_path / "wt")]):
        assert run([*git, *args], cwd=main).returncode == 0
    old = legacy_config(main)  # not committed, so the linked worktree has no copy of its own
    cfg = config.load(tmp_path / "wt")
    assert (cfg.project, cfg.path, cfg.legacy) == ("old-project", old, True)


def test_write_config_stays_in_a_pre_rename_folder(tmp_path: Path) -> None:
    old = tmp_path / "old"
    legacy_config(old)
    assert config.write_config(old, "p") == old / ".assay" / "config.toml"
    assert 'project = "p"' in (old / ".assay" / "config.toml").read_text() and not (old / ".benchbook").exists()
    new = tmp_path / "new"
    new.mkdir()
    assert config.write_config(new, "p") == new / ".benchbook" / "config.toml" and not (new / ".assay").exists()


def test_autoresearch_files_follow_the_folder(tmp_path: Path) -> None:
    co = Checkout(tmp_path)
    assert co.state_path() == tmp_path / ".benchbook" / "ar_state.json"  # a checkout made after the rename
    legacy_config(tmp_path)
    state = tmp_path / ".assay" / "ar_state.json"
    state.write_text(json.dumps({"last_code_hash": "abc"}))
    assert co.load_state() == {"last_code_hash": "abc"}
    co.save_state(last_experiment="e1")
    assert json.loads(state.read_text()) == {"last_code_hash": "abc", "last_experiment": "e1"}
    assert co.pending_path() == tmp_path / ".assay" / "ar_pending.jsonl" and not (tmp_path / ".benchbook").exists()
    # `git mv .assay .benchbook` elsewhere, then a pull here: the tracked config moved, the untracked files did not
    (tmp_path / ".benchbook").mkdir()
    (tmp_path / ".assay" / "config.toml").rename(tmp_path / ".benchbook" / "config.toml")
    assert co.load_state()["last_experiment"] == "e1"
    assert co.pending_path() == tmp_path / ".benchbook" / "ar_pending.jsonl"  # nothing was queued in the old one
    (tmp_path / ".assay" / "ar_pending.jsonl.0af3.flushing").write_text("{}\n")  # a batch a crashed flush left
    assert co.pending_path() == tmp_path / ".assay" / "ar_pending.jsonl"


def sim_checkout(base: Path, name: str) -> Path:
    root = base / name
    root.mkdir()
    for f in ("prepare.py", "train.py"):
        shutil.copy(SIM / f, root / f)
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid"]
    for args in (["init", "-q"], ["add", "-A"], ["commit", "-qm", "upstream"]):
        assert run([*git, *args], cwd=root).returncode == 0
    return root


def test_ar_init_in_a_pre_rename_checkout_stays_in_its_folder(tmp_path: Path, db_url: str,
                                                               monkeypatch: pytest.MonkeyPatch) -> None:
    only_legacy(monkeypatch, DATABASE_URL=db_url, AR_HW="Test CPU")
    pid = f"ar-{uuid.uuid4().hex[:10]}"
    root = sim_checkout(tmp_path, "a")
    legacy_config(root, pid)
    r = ops.init(pid, start=root)
    assert r["config"] == str(root / ".assay" / "config.toml") and r["hw"] == "test-cpu"
    ignored = (root / ".gitignore").read_text().split()
    assert ".assay/ar_state.json" in ignored and ".assay/ar_pending.jsonl" in ignored
    assert not any(x.startswith(".benchbook") for x in ignored) and not (root / ".benchbook").exists()


def test_doctor_shows_how_to_rename_the_folder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    only_legacy(monkeypatch)
    assert doctor._legacy_config(tmp_path) is None
    old = legacy_config(tmp_path)
    check = doctor._legacy_config(tmp_path)
    assert check is not None and check.ok and "git mv .assay .benchbook" in check.detail and str(old) in check.detail


# ---------------------------------------------------------------------- 3. data and state directories


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An empty home directory, with no XDG overrides and no benchbook variables."""
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    only_legacy(monkeypatch)
    for k in ("XDG_DATA_HOME", "XDG_STATE_HOME"):
        monkeypatch.delenv(k, raising=False)
    assert Path.home() == h
    return h


@pytest.mark.parametrize(("system", "base"), [("Linux", ".local/share"), ("Darwin", "Library/Application Support")])
def test_data_dir_keeps_a_cluster_from_before_the_rename(home: Path, monkeypatch: pytest.MonkeyPatch, system: str,
                                                          base: str) -> None:
    monkeypatch.setattr(dbconn.platform, "system", lambda: system)
    new, old = home / base / "benchbook" / "pg", home / base / "assay" / "pg"
    assert dbconn.data_dir() == new  # a fresh machine
    old.mkdir(parents=True)
    assert dbconn.data_dir() == new  # an empty folder holds no cluster
    (old / "PG_VERSION").write_text("16\n")
    assert dbconn.data_dir() == old
    assert not new.exists() and sorted(p.name for p in old.iterdir()) == ["PG_VERSION"]  # nothing moved or created
    note = doctor._legacy_data_dir()
    assert note is not None and note.ok
    assert shlex.split(note.detail.split("no Claude Code session running: ")[1]) == [
        "benchbook", "db", "stop", "&&", "mkdir", "-p", str(new.parent), "&&", "mv", str(old), str(new)]
    new.mkdir(parents=True)
    assert dbconn.data_dir() == old  # still the only cluster
    (new / "PG_VERSION").write_text("16\n")
    assert dbconn.data_dir() == new and doctor._legacy_data_dir() is None
    monkeypatch.setenv("ASSAY_DATA_DIR", str(home / "explicit"))
    assert dbconn.data_dir() == home / "explicit"


def test_data_dir_follows_xdg_data_home_for_both_names(home: Path, tmp_path: Path,
                                                        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dbconn.platform, "system", lambda: "Linux")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    assert dbconn.data_dir() == tmp_path / "xdg" / "benchbook" / "pg"
    (tmp_path / "xdg" / "assay" / "pg").mkdir(parents=True)
    (tmp_path / "xdg" / "assay" / "pg" / "PG_VERSION").write_text("16\n")
    assert dbconn.data_dir() == tmp_path / "xdg" / "assay" / "pg"


def test_state_dir_keeps_the_pre_rename_folder(home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    new, old = home / ".local" / "state" / "benchbook", home / ".local" / "state" / "assay"
    assert config.state_dir() == new == hooks._state_dir() == dbconn.state_dir()
    assert doctor._legacy_state_dir() is None
    (old / "spool").mkdir(parents=True)
    assert config.state_dir() == old == hooks._state_dir() and not new.exists()
    hooks.log("a line after the upgrade")
    assert "a line after the upgrade" in (old / "hook.log").read_text() and not new.exists()
    note = doctor._legacy_state_dir()
    assert note is not None and note.ok and f"mv {old} {new}" in note.detail
    new.mkdir()
    assert config.state_dir() == new
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    (tmp_path / "xdg" / "assay").mkdir(parents=True)
    assert config.state_dir() == tmp_path / "xdg" / "assay"


def test_embedded_database_keeps_its_pre_rename_name(db_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    tag = uuid.uuid4().hex[:8]
    new, old = f"benchbook_{tag}", f"assay_{tag}"
    monkeypatch.setattr(dbconn, "EMBEDDED_DB", new)
    monkeypatch.setattr(dbconn, "LEGACY_EMBEDDED_DB", old)
    monkeypatch.setattr(dbconn, "_embedded_server_uri", lambda pgdata: db_url)
    monkeypatch.setattr(dbconn, "data_dir", lambda: Path("/nonexistent"))
    only_legacy(monkeypatch)

    def names() -> set[str]:
        with psycopg.connect(db_url, autocommit=True) as c:
            return {r[0] for r in c.execute("SELECT datname FROM pg_database WHERE datname = ANY(%s)", ([new, old],))}

    try:
        dbconn.ensure_database(db_url, old)  # a cluster from before the rename holds the ledger in "assay"
        url = dbconn.database_url()
        assert psycopg.conninfo.conninfo_to_dict(url)["dbname"] == old and names() == {old}  # no empty twin
        dbconn.ensure_database(db_url, new)  # once a database with the new name exists, it is the one
        assert psycopg.conninfo.conninfo_to_dict(dbconn.database_url())["dbname"] == new
    finally:
        with psycopg.connect(db_url, autocommit=True) as c:
            for name in (new, old):
                c.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
    assert psycopg.conninfo.conninfo_to_dict(dbconn.database_url())["dbname"] == new  # a new cluster gets the new name
    with psycopg.connect(db_url, autocommit=True) as c:
        c.execute(f'DROP DATABASE IF EXISTS "{new}" WITH (FORCE)')


# ---------------------------------------------------------------------- 4. the `assay` command


def test_pyproject_declares_the_legacy_command() -> None:
    scripts = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["scripts"]
    assert scripts == {"benchbook": "benchbook.entry:main", "assay": "benchbook.entry:legacy_main"}


def test_assay_prints_one_notice_and_then_runs_as_benchbook() -> None:
    env = legacy_environ()
    for args in (["--version"], ["db", "--help"]):
        new, old = run([BIN / "benchbook", *args], env=env), run([BIN / "assay", *args], env=env)
        assert new.returncode == old.returncode == 0 and old.stdout == new.stdout and new.stderr == ""
        [line] = old.stderr.splitlines()
        assert line.startswith("benchbook: the `assay` command is now `benchbook`")
    assert run([BIN / "assay", "--version"], env=env).stdout.strip() == f"benchbook {benchbook.__version__}"


def worker_stop(repo: Path, text: str, agent_type: str = "assay:assay-worker") -> dict[str, Any]:
    return {"cwd": str(repo), "hook_event_name": "SubagentStop", "session_id": "s", "agent_id": uuid.uuid4().hex,
            "agent_type": agent_type, "last_assistant_message": text}


def captured(lg: Ledger) -> list[str]:
    return [r["text"] for r in lg.sql_internal(
        "SELECT payload->>'text' AS text FROM benchbook.claims WHERE project_id=%s AND 'auto-captured' = ANY(tags)"
        " ORDER BY created_at", (lg.project_id,))]


def test_deployed_assay_hook_is_silent_and_captures(tmp_path: Path, db_url: str, project: str, lead: Ledger) -> None:
    """What an install from before the rename runs at every subagent stop: `assay hook subagent-stop`, ASSAY_*
    variables from Claude Code's settings, .assay/config.toml, the worker type `assay:assay-worker`. Nothing may
    reach stderr; the notes about the old names go to hook.log, and only for a worker's event."""
    repo = tmp_path / "repo"
    legacy_config(repo, project)
    state = tmp_path / "state"
    env = legacy_environ(DATABASE_URL=db_url, HOOK_AGENTS="assay-worker")
    env["XDG_STATE_HOME"] = str(state)
    other = run([BIN / "assay", "hook", "subagent-stop"], env=env, cwd=repo,
                input=json.dumps(worker_stop(repo, "Explore results.", agent_type="Explore")))
    assert (other.returncode, other.stdout, other.stderr) == (0, "", "")
    assert not (state / "benchbook" / "hook.log").exists()  # another subagent's stop leaves no trace
    for n, cmd in enumerate((["assay", "hook", "subagent-stop"], ["benchbook", "hook", "subagent-stop"],
                             ["assay", "--project", project, "hook", "subagent-stop"])):  # the last one: via the CLI
        p = run([BIN / cmd[0], *cmd[1:]], env=env, cwd=repo,
                input=json.dumps(worker_stop(repo, f"Finding {n} of a pre-rename install.")))
        assert (p.returncode, p.stdout, p.stderr) == (0, "", ""), cmd
    for _ in range(300):
        if len(captured(lead)) == 3:
            break
        time.sleep(0.1)
    assert sorted(captured(lead)) == [f"Finding {n} of a pre-rename install." for n in range(3)]
    log = (state / "benchbook" / "hook.log").read_text()
    assert log.count("note: the `assay` command is now `benchbook`") == 2  # not for the run through `benchbook`
    assert log.count("note: ASSAY_DATABASE_URL, ASSAY_HOOK_AGENTS: these are the names from before the rename") == 3
    assert db_url not in log  # the note names the variable, never its value
    assert not any(marker in line for line in log.splitlines() if " note: " in line
                   for marker in doctor.FAILURE_MARKERS)  # doctor must not read the notes as lost reports


def test_hook_logs_a_note_about_a_variable_read_late(tmp_path: Path, db_url: str, monkeypatch: pytest.MonkeyPatch,
                                                      capsys: pytest.CaptureFixture[str]) -> None:
    """ASSAY_PROJECT is read only when the project is resolved, after the worker check: its note still reaches
    hook.log, and nothing reaches stderr."""
    only_legacy(monkeypatch, PROJECT="p-from-the-old-variable")
    monkeypatch.setenv("BENCHBOOK_DATABASE_URL", db_url)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(hooks.subprocess, "Popen", lambda *a, **k: None)  # no background processor
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(worker_stop(tmp_path, "A finding."))))
    assert hooks.main("subagent-stop") is None
    assert capsys.readouterr().err == ""
    log = (tmp_path / "state" / "benchbook" / "hook.log").read_text()
    assert "note: ASSAY_PROJECT: this is the name from before the rename" in log
    [entry] = (tmp_path / "state" / "benchbook" / "spool").glob("*/*.json")
    assert json.loads(entry.read_text())["project"] == "p-from-the-old-variable"


def test_assay_hook_starts_without_the_cli_framework(tmp_path: Path) -> None:
    """Start-up of the hook is paid at every subagent stop: the legacy entry must stay on the fast path too."""
    code = ("import io, json, sys\n"
            "sys.argv = ['assay', 'hook', 'subagent-stop']\n"
            "sys.stdin = io.StringIO(json.dumps({'agent_type': 'Explore'}))\n"
            "from benchbook.entry import legacy_main\n"
            "try:\n    legacy_main()\nexcept SystemExit as e:\n    assert e.code == 0, e.code\n"
            "else:\n    raise AssertionError('the hook entry did not exit')\n"
            "heavy = [m for m in ('typer', 'click', 'benchbook.cli', 'benchbook.ledger', 'psycopg', 'tomllib')"
            " if m in sys.modules]\n"
            "assert not heavy, heavy\n")
    env = legacy_environ(HOOK_AGENTS="assay-worker")
    env["XDG_STATE_HOME"] = str(tmp_path / "state")
    p = run([sys.executable, "-c", code], env=env, cwd=tmp_path)
    assert (p.returncode, p.stdout, p.stderr) == (0, "", "")


# ---------------------------------------------------------------------- 5. the worker's agent type


def test_worker_types_cover_both_names(monkeypatch: pytest.MonkeyPatch) -> None:
    only_legacy(monkeypatch)
    for t in ("benchbook-worker", "benchbook:benchbook-worker", "assay-worker", "assay:assay-worker"):
        assert hooks.is_worker(t), t
    assert not hooks.is_worker("Explore") and not hooks.is_worker("assay:assay-lead") and not hooks.is_worker(None)
    # a setting written before the rename names the old worker; the updated plugin's worker is still captured
    monkeypatch.setenv("ASSAY_HOOK_AGENTS", "assay-worker,implementer")
    assert hooks.is_worker("benchbook:benchbook-worker") and hooks.is_worker("implementer")
    monkeypatch.setenv("BENCHBOOK_HOOK_AGENTS", "implementer")  # a list without the worker excludes both names
    assert not hooks.is_worker("assay:assay-worker") and not hooks.is_worker("benchbook-worker")


# ---------------------------------------------------------------------- 6. persisted identifiers


def test_blob_uris_of_both_schemes_are_read_and_new_ones_use_benchbook(worker: Ledger) -> None:
    uri = worker.put_blob(b"print(1)\n")
    digest = hashlib.sha256(b"print(1)\n").hexdigest()
    assert uri == "benchbook://blob/" + digest
    for stored in (uri, "assay://blob/" + digest, digest):
        assert worker.get_blob(stored) == b"print(1)\n" and blob_digest(stored) == digest
    assert is_blob_uri("assay://blob/" + digest) and not is_blob_uri("s3://bucket/x") and not is_blob_uri(None)
    assert artifact_blob([{"uri": "s3://x"}, "junk", {"uri": "assay://blob/" + digest}]) == "assay://blob/" + digest
    assert artifact_blob(None) is None


def test_runs_recorded_with_assay_blob_uris_are_still_replicated(tmp_path: Path, db_url: str,
                                                                  monkeypatch: pytest.MonkeyPatch) -> None:
    """A ledger from before the rename: the baseline's train.py is stored as assay://blob/<sha>. After the upgrade
    the referee must still queue its replication, another agent must still fetch the code, and the same bytes must
    still be recognised as already run."""
    only_legacy(monkeypatch)
    for k, v in {"BENCHBOOK_DATABASE_URL": db_url, "BENCHBOOK_AR_HW": "test-cpu", "AR_SIM_SECONDS": "0.05",
                 "AR_SIM_LUCKY": "0", "AR_SIM_NOISE": "0.0005", "GIT_AUTHOR_NAME": "t", "GIT_COMMITTER_NAME": "t",
                 "GIT_AUTHOR_EMAIL": "t@example.invalid", "GIT_COMMITTER_EMAIL": "t@example.invalid"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("CUDA_VISIBLE_DEVICES", raising=False)
    cmd = f"{sys.executable} train.py"
    pid = f"ar-{uuid.uuid4().hex[:10]}"
    ra, rb = sim_checkout(tmp_path, "a"), sim_checkout(tmp_path, "b")
    ops.init(pid, start=ra)
    a = ops.open_ctx(project=pid, agent="a", start=ra)
    with monkeypatch.context() as before_rename:
        before_rename.setattr(ledger_mod, "BLOB_PREFIX", LEGACY_BLOB_PREFIX)
        first = ops.run(a, "baseline", cmd)
    src = (ra / "train.py").read_bytes()
    stored = ops.blob_of(a.lg.get(first["id"])["record"])
    assert stored == "assay://blob/" + hashlib.sha256(src).hexdigest()
    assert [r["id"] for r in ops._finished_runs(a, src, ("not-this-code-ref",))] == [a.lg.resolve(first["id"])[1]]
    done = ops.replicate(ops.open_ctx(project=pid, agent="b", start=rb), cmd=cmd)
    assert len(done) == 1 and done[0]["run_status"] == "ok"


def transcript(path: Path, block: dict[str, Any], result: str) -> Path:
    start = datetime.now(UTC) - timedelta(seconds=10)
    rows = [{"timestamp": start.isoformat(), "message": {"role": "user", "content": "take a task"}},
            {"timestamp": (start + timedelta(seconds=5)).isoformat(),
             "message": {"content": [{"type": "tool_use", "id": "x", **block}]}},
            {"timestamp": (start + timedelta(seconds=6)).isoformat(),
             "message": {"content": [{"type": "tool_result", "tool_use_id": "x",
                                      "content": [{"type": "text", "text": result}]}]}}]
    path.write_text("\n".join(json.dumps(r) for r in rows))
    return path


def test_citations_in_pre_rename_transcripts_still_count(tmp_path: Path, worker: Ledger) -> None:
    """A worker that recorded its result through the old tool or command names cited its own record: no auto-captured
    note on top of it."""
    assert hooks._write_tool({"name": "mcp__plugin_assay_assay__assay_propose_claim",
                              "input": {"agent": "w"}}) == ("c", "w", True)
    assert hooks._write_tool({"name": "Bash", "input": {
        "command": "ASSAY_AGENT=w uv run python -m assay claim add note 'x'"}}) == ("c", "w", False)
    config.write_config(tmp_path, worker.project_id)
    for n, block in enumerate((
            {"name": "mcp__plugin_assay_assay__assay_record_experiment", "input": {"agent": worker.agent}},
            {"name": "Bash", "input": {"command": f"ASSAY_AGENT={worker.agent} assay --json exp record --config x"}})):
        e = worker.record_experiment({"a": n}, {"score": 1})
        ref = f"e:{str(e.id)[:8]}"
        data = worker_stop(tmp_path, f"Recorded {ref}.")
        data["agent_transcript_path"] = str(transcript(tmp_path / f"t{n}.jsonl", block, json.dumps({"id": ref})))
        assert hooks.process("subagent-stop", data) == "cited", block
    assert captured(worker) == []


def test_reports_queued_before_the_upgrade_are_replayed(tmp_path: Path, db_url: str, worker: Ledger,
                                                         monkeypatch: pytest.MonkeyPatch) -> None:
    """The spool of an install from before the rename: under <state>/assay, keyed by the database URL it read from
    ASSAY_DATABASE_URL, holding an entry of the worker type that build knew."""
    only_legacy(monkeypatch, DATABASE_URL=db_url)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    spool = tmp_path / "state" / "assay" / "spool" / hashlib.sha256(json.dumps([db_url]).encode()).hexdigest()
    spool.mkdir(parents=True)
    (spool / "queued.json").write_text(json.dumps({
        "event": "subagent-stop", "project": worker.project_id, "spooled_at": time.time(),
        "data": worker_stop(tmp_path, "Queued before the upgrade.", agent_type="assay:assay-worker")}))
    assert hooks._spool_dir() == spool
    summary = hooks.replay_now(hooks._spool_dir())
    assert (summary.captured, summary.dead, summary.queued) == (1, 0, 0) and not list(spool.glob("*.json"))
    assert captured(worker) == ["Queued before the upgrade."]
    assert not (tmp_path / "state" / "benchbook").exists()


def test_capture_hash_is_the_one_stored_before_the_rename(tmp_path: Path, worker: Ledger) -> None:
    """A report captured by the old build and replayed by this one must land on the same claim, not a second one."""
    config.write_config(tmp_path, worker.project_id)
    text = "The same report, before and after the upgrade."
    result = hooks.process("subagent-stop", worker_stop(tmp_path, text))
    assert result.startswith("captured:")
    [row] = worker.sql_internal("SELECT statement_hash FROM benchbook.claims WHERE project_id=%s AND"
                                " 'auto-captured' = ANY(tags)", (worker.project_id,))
    assert row["statement_hash"] == hashlib.sha256(b"assay:auto-captured\0" + text.encode()).hexdigest()


def test_doctor_reads_hook_log_lines_of_the_old_build(tmp_path: Path) -> None:
    line = "subagent-stop: not captured: no assay project for {} (no .assay/config.toml there or above)"
    assert doctor._counts(line.format(tmp_path / "sub"), tmp_path) is True
    assert doctor._counts(line.format("/somewhere/else"), tmp_path) is False


def test_mcp_server_started_as_assay_serves_the_old_tool_names(project: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """The plugin installed before the rename starts `assay mcp`; its worker definition allows only
    mcp__plugin_assay_assay__assay_* tools and its skills call them by those names."""
    from benchbook.mcp_server import TOOLS, build, tool_names
    from tests.test_mcp import call

    new = tool_names(build(project, "all"))
    assert new == sorted("benchbook_" + t for t in TOOLS)  # what the legacy server renames in its texts
    monkeypatch.setattr(config, "LEGACY_COMMAND", True)  # what entry.legacy_main sets
    mcp = build(project, "all")
    assert tool_names(mcp) == sorted("assay_" + n.removeprefix("benchbook_") for n in new)
    tools = {t.name: t for t in asyncio.run(mcp.list_tools())}
    assert "assay://schema" in (tools["assay_sql"].description or "")
    assert "benchbook_" not in json.dumps([t.description for t in tools.values()])
    doc = asyncio.run(mcp.read_resource("assay://schema"))
    assert "assay_sql" in str(doc) and "v_claim_status" in str(doc)
    brief = call(mcp, "assay_brief", agent="w")  # the brief names the tools to call next: the ones this server has
    assert "assay_check_tried" in brief and "assay_get" in brief and "benchbook_" not in brief
    assert "benchbook_get" in call(build(project, "all", legacy_names=False), "benchbook_brief", agent="w")
    assert tool_names(build(project, "all", legacy_names=False)) == new


def test_viewer_accepts_the_token_cookie_set_before_the_rename(lead: Ledger) -> None:
    from starlette.testclient import TestClient

    from benchbook.web.app import create_app

    app = create_app(lead.project_id, host="10.0.0.5", token="s3cret", with_mcp=False)
    with TestClient(app, base_url="http://10.0.0.5:7466") as client:
        assert client.get("/tasks").status_code == 401
        assert client.get("/tasks", headers={"Cookie": "assay_token=wrong"}).status_code == 401
        assert client.get("/tasks", headers={"Cookie": "assay_token=s3cret"}).status_code == 200


def test_bench_readers_accept_pre_rename_keys(db_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    from bench import real
    from bench.autoresearch import arms
    from tests.conftest import make_project

    runs = [{"assay_run": "run", "assay_run_id": "r1", "agent": "gpu0", "code_hash": "x", "seed": 0, "start": 0.0},
            {"assay_run": "replicate", "assay_run_id": "r2", "agent": "gpu1", "code_hash": "x", "seed": 1,
             "start": 1.0},
            {"assay_run": None, "agent": "gpu0", "code_hash": "y", "seed": None, "start": 2.0}]
    ledger = [{"ar_run_id": "r1", "author": "gpu0", "code_ref": "x", "seed": 0},
              {"ar_run_id": "r2", "author": "gpu1", "code_ref": "x", "seed": 1}]
    assert arms.unrecorded(runs, ledger) == {"not_recorded": 0, "ledger_only": 0, "outside_benchbook": 1}
    assert [arms.replication_number(r) for r in runs] == [0, 1, 0]
    monkeypatch.setenv("BENCHBOOK_DATABASE_URL", db_url)
    pid = make_project(db_url, metric="sum_radii")
    w = Ledger.open(project=pid, agent="worker")
    for n, system in enumerate(("assay-bench", "benchbook-bench", "something-else")):
        w.record_experiment({"n": n}, {"sum_radii": 1.0}, trace_ref={"system": system, "trace_id": f"eval-{n}"})
    assert sorted(r["eval_id"] for r in real._ledger_receipts(pid)) == ["eval-0", "eval-1"]


# ---------------------------------------------------------------------- 7. a pre-rename install, end to end


def test_legacy_only_environment_inits_records_and_passes_doctor(tmp_path: Path, db_url: str) -> None:
    """ASSAY_PROJECT, ASSAY_DATABASE_URL and ASSAY_AGENT set, no BENCHBOOK_* variable, .assay/config.toml present."""
    repo = tmp_path / "repo"
    pid = f"legacy-{uuid.uuid4().hex[:8]}"
    legacy_config(repo, "a-project-the-variable-overrides")
    env = legacy_environ(DATABASE_URL=db_url, PROJECT=pid, AGENT="w0")
    env.update(HOME=str(tmp_path / "home"), XDG_STATE_HOME=str(tmp_path / "state"),
               CLAUDE_CONFIG_DIR=str(tmp_path / "no-claude"))
    assert not [k for k in env if k.startswith("BENCHBOOK_")]

    r = run([BIN / "benchbook", "--json", "init", "--project", pid, "--goal", "Legacy install", "--metric", "score",
             "--direction", "max"], cwd=repo, env=env)
    assert r.returncode == 0, r.stderr
    assert Path(json.loads(r.stdout)["config"]) == (repo / ".assay" / "config.toml").resolve()
    assert f'project = "{pid}"' in (repo / ".assay" / "config.toml").read_text() and not (repo / ".benchbook").exists()
    [note] = notices(r.stderr)  # one line, whatever number of old variables was read
    assert "ASSAY_" in note

    r = run([BIN / "benchbook", "--json", "exp", "record", "--config", '{"x": 1}', "--metrics", '{"score": 1.5}'],
            cwd=repo, env=env)
    assert r.returncode == 0, r.stderr
    rec = json.loads(r.stdout)
    assert rec["ok"] is True and rec["outcome"] == "improved" and len(notices(r.stderr)) == 1

    r = run([BIN / "assay", "--json", "status"], cwd=repo, env=env)  # the old command, the old variables
    assert r.returncode == 0, r.stderr
    status = json.loads(r.stdout)
    assert status["project"] == pid and status["experiments"]["total"] == 1 and status["best"]["author"] == "w0"
    assert len(notices(r.stderr)) == 2  # the command and the variables, one line each

    r = run([BIN / "benchbook", "--json", "doctor"], cwd=repo, env=env)
    checks = {c["check"]: c for c in json.loads(r.stdout)["checks"]}
    assert r.returncode == 0, checks  # an install that works under the old names passes
    assert checks["connect"]["ok"] and checks["project config"]["ok"] and pid in checks["project config"]["detail"]
    assert "git mv .assay .benchbook" in checks["config folder"]["detail"]
    for name in ("ASSAY_DATABASE_URL", "ASSAY_PROJECT", "ASSAY_AGENT"):
        assert name in checks["legacy variables"]["detail"]
    assert db_url not in r.stdout  # the URL can carry a password
    assert "data directory" not in checks  # ASSAY_DATABASE_URL is the target, not the embedded default


def test_doctor_accepts_the_plugin_and_settings_from_before_the_rename(tmp_path: Path, db_url: str,
                                                                       project: str) -> None:
    home, repo = tmp_path / "home", tmp_path / "repo"
    claude = home / ".claude"
    (claude / "plugins").mkdir(parents=True)
    legacy_config(repo, project)
    (claude / "plugins" / "installed_plugins.json").write_text(json.dumps(
        {"version": 2, "plugins": {"assay@assay": [{"scope": "user", "installPath": "/x"}]}}))
    settings = {"enabledPlugins": {"assay@assay": True},
                "env": {"ASSAY_PROJECT": project, "ASSAY_DATABASE_URL": db_url}}
    (claude / "settings.json").write_text(json.dumps(settings))
    env = legacy_environ(DATABASE_URL=db_url)
    env.update(HOME=str(home), CLAUDE_CONFIG_DIR=str(claude), XDG_STATE_HOME=str(tmp_path / "state"),
               BENCHBOOK_PROJECT=project, PATH=str(BIN) + os.pathsep + "/usr/bin:/bin")

    def checks() -> tuple[int, dict[str, Any]]:
        r = run([sys.executable, "-m", "benchbook.cli", "--json", "doctor"], cwd=repo, env=env)
        assert db_url not in r.stdout
        return r.returncode, {c["check"]: c for c in json.loads(r.stdout)["checks"]}

    code, c = checks()
    assert code == 0, c
    assert c["claude code plugin"]["ok"] and "assay@assay" in c["claude code plugin"]["detail"]
    assert "claude plugin install benchbook@benchbook" in c["claude code plugin"]["detail"]
    # the same target under different names on the two sides is still the same target
    assert c["BENCHBOOK_DATABASE_URL"]["ok"] and "ASSAY_DATABASE_URL (shell)" in c["BENCHBOOK_DATABASE_URL"]["detail"]
    assert c["BENCHBOOK_PROJECT"]["ok"] and "ASSAY_PROJECT (Claude Code)" in c["BENCHBOOK_PROJECT"]["detail"]
    settings["env"]["ASSAY_PROJECT"] = "some-other-project"  # ... and a different value is still a difference
    (claude / "settings.json").write_text(json.dumps(settings))
    code, c = checks()
    assert code != 0 and c["BENCHBOOK_PROJECT"]["ok"] is False
    assert "some-other-project" in c["BENCHBOOK_PROJECT"]["detail"]
    settings["env"]["ASSAY_PROJECT"] = project
    settings["enabledPlugins"] = {}
    (claude / "settings.json").write_text(json.dumps(settings))
    (repo / ".mcp.json").write_text(json.dumps({"mcpServers": {"assay": {"command": "assay", "args": ["mcp"]}}}))
    code, c = checks()
    assert code == 0 and "manual install" in c["claude code plugin"]["detail"], c
