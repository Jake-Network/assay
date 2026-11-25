"""pack.toml dedup_mode: "refuse" stores nothing for an exact duplicate, "notify" stores it and names the first run."""
from __future__ import annotations

import json
import shutil
import threading
import tomllib
import uuid
from importlib import resources
from pathlib import Path
from typing import Any

import psycopg
import pytest

from benchbook import Ledger, cli, hooks, packs
from benchbook.core import ledger as ledger_module
from benchbook.db import conn as dbconn
from benchbook.db import migrate as dbmigrate
from benchbook.errors import BenchbookError
from benchbook.mcp_server import build
from tests.conftest import make_project
from tests.test_mcp import call

NOTIFY_PACK = "notify-test"


def _pack_dir(tmp_path: Path, mode: str) -> Path:
    """A copy of the core pack whose pack.toml sets dedup_mode (top-level keys go before the first table)."""
    root = tmp_path / f"pack-{mode}"
    shutil.copytree(str(resources.files("benchbook.packs").joinpath("core")), root)
    toml = root / "pack.toml"
    toml.write_text(f'dedup_mode = "{mode}"\n' + toml.read_text(encoding="utf-8"), encoding="utf-8")
    return root


@pytest.fixture
def notify_project(db_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """A project whose pack sets dedup_mode = "notify". No shipped pack does yet, so one is read from a
    temporary directory and served under its name."""
    pack = packs.read_pack(NOTIFY_PACK, _pack_dir(tmp_path, "notify"))
    assert pack.dedup_mode == "notify"
    real = packs.load_pack
    monkeypatch.setattr(ledger_module, "load_pack", lambda name: pack if name == NOTIFY_PACK else real(name))
    return make_project(db_url, pack=NOTIFY_PACK, settings={"partition_key": "part"})


def _open(db_url: str, project: str, agent: str) -> Ledger:
    return Ledger.open(project=project, agent=agent, database_url=db_url)


def _rows(lg: Ledger, query: str) -> list[dict[str, Any]]:
    return lg.sql_internal(query, (lg.project_id,))


# pack.toml ----------------------------------------------------------------------------------------
def test_pack_toml_sets_the_mode(tmp_path: Path) -> None:
    assert packs.load_pack("core").dedup_mode == "refuse"  # the default: core's pack.toml does not name it
    shipped = resources.files("benchbook.packs").joinpath("ml-research", "pack.toml").read_text(encoding="utf-8")
    assert tomllib.loads(shipped)["dedup_mode"] == "refuse"  # ml-research states it
    assert packs.load_pack("ml-research").dedup_mode == "refuse"
    assert packs.read_pack("p", _pack_dir(tmp_path, "notify")).dedup_mode == "notify"
    with pytest.raises(BenchbookError, match="dedup_mode must be one of refuse, notify"):
        packs.read_pack("p", _pack_dir(tmp_path, "warn"))


# refuse (today's behaviour) -----------------------------------------------------------------------
def test_refuse_mode_is_unchanged(db_url: str, lead: Ledger, worker: Ledger) -> None:
    first = worker.record_experiment({"a": 1}, {"score": 1.0}, seed=0, code_ref="c1")
    again = lead.as_agent("worker-2").record_experiment({"a": 1}, {"score": 1.5}, seed=0, code_ref="c1")
    assert first.id is not None and first.duplicate_of is None and first.notice is None
    assert again.model_dump() == {"id": None, "outcome": None, "is_new_best": False, "duplicate_of": first.id,
                                  "dedup_key": first.dedup_key, "primary_value": None, "notice": None}
    rows = _rows(lead, "SELECT id, duplicate_of FROM benchbook.experiments WHERE project_id=%s")
    assert rows == [{"id": first.id, "duplicate_of": None}]
    assert [h["kind"] for h in _rows(lead, "SELECT kind FROM benchbook.dedup_hits WHERE project_id=%s")] == ["rejected"]
    assert lead.status()["dedup"] == {"rejected": 1, "avoided": 0}
    # the database still refuses an undeclared duplicate, whoever writes it
    with psycopg.connect(db_url) as conn, pytest.raises(psycopg.errors.UniqueViolation):
        conn.execute(
            "INSERT INTO benchbook.experiments(project_id,author,config,code_ref,dedup_key,seed,run_status,outcome)"
            " VALUES (%s,'worker-1','{}','c1',%s,0,'ok','unscored')", (lead.project_id, first.dedup_key))


def test_refuse_mode_tool_and_cli_shapes_are_unchanged(project: str) -> None:
    mcp = build(project, "all")
    args: dict[str, Any] = {"agent": "worker-1", "config": {"a": 1}, "metrics": {"score": 1.0}, "seed": 0}
    first = call(mcp, "benchbook_record_experiment", **args)
    assert first == {"id": first["id"], "outcome": "improved", "is_new_best": True, "duplicate_of": None}
    assert call(mcp, "benchbook_record_experiment", **args) == {
        "id": None, "outcome": None, "is_new_best": False, "duplicate_of": first["id"]}


# notify -------------------------------------------------------------------------------------------
def test_notify_mode_records_both_and_reports_the_first(db_url: str, notify_project: str) -> None:
    lead, a, b = (_open(db_url, notify_project, name) for name in ("lead", "implementer", "verifier"))
    cfg = {"part": "p1", "approach": "hash-before-put"}
    first = a.record_experiment(cfg, {"score": 2.0}, seed=0, code_ref="a1b2c3d")
    second = b.record_experiment(cfg, {"score": 2.0}, seed=0, code_ref="a1b2c3d")  # the verifier's re-run
    third = a.record_experiment({**cfg, "notes": "ignored key"}, {"score": 1.0}, seed=0, code_ref="a1b2c3d")
    assert first.id and second.id and third.id and len({first.id, second.id, third.id}) == 3
    assert first.duplicate_of is None and first.notice is None
    # every repeat names the first run, not the one before it
    assert second.duplicate_of == first.id and third.duplicate_of == first.id
    assert second.outcome == "no_change" and third.outcome == "worse" and not second.is_new_best
    short_first = f"e:{str(first.id)[:8]}"
    assert second.notice is not None and short_first in second.notice and "notify" in second.notice
    rows = _rows(lead, "SELECT id, author, duplicate_of FROM benchbook.experiments WHERE project_id=%s"
                       " ORDER BY created_at")
    assert rows == [{"id": first.id, "author": "implementer", "duplicate_of": None},
                    {"id": second.id, "author": "verifier", "duplicate_of": first.id},
                    {"id": third.id, "author": "implementer", "duplicate_of": first.id}]
    # logged as notified: nothing was refused, so the "duplicates blocked" counter does not move
    hits = _rows(lead, "SELECT kind, existing_id, agent_id FROM benchbook.dedup_hits WHERE project_id=%s ORDER BY id")
    assert hits == [{"kind": "notified", "existing_id": first.id, "agent_id": "verifier"},
                    {"kind": "notified", "existing_id": first.id, "agent_id": "implementer"}]
    assert lead.status()["dedup"] == {"rejected": 0, "avoided": 0}
    assert lead.get(second.id)["record"]["duplicate_of"] == str(first.id)


def test_notify_mode_only_reports_exact_ok_repeats(db_url: str, notify_project: str) -> None:
    w = _open(db_url, notify_project, "w")
    cfg = {"part": "p1", "x": 1}
    failed = w.record_experiment(cfg, {}, run_status="error", seed=0, notes="boom")
    first = w.record_experiment(cfg, {"score": 1.0}, seed=0)  # an earlier failure is not a duplicate
    assert failed.duplicate_of is None and first.duplicate_of is None and first.notice is None
    assert w.record_experiment(cfg, {"score": 1.0}, seed=1).duplicate_of is None  # another seed: a replicate
    assert w.record_experiment(cfg, {"score": 1.0}).duplicate_of is None  # no seed is its own slot
    assert w.record_experiment(cfg, {"score": 1.0}, code_ref="other").duplicate_of is None
    assert w.record_experiment(cfg, {}, run_status="error", seed=0).duplicate_of is None  # a failed repeat
    assert w.record_experiment(cfg, {"score": 1.0}).duplicate_of is not None
    assert w.record_experiment(cfg, {"score": 1.0}, seed=0).duplicate_of == first.id


def test_views_stay_correct_when_rows_share_a_key(db_url: str, notify_project: str) -> None:
    lead, a, b = (_open(db_url, notify_project, name) for name in ("lead", "implementer", "verifier"))
    cfg = {"part": "p1", "x": 1}
    first = a.record_experiment(cfg, {"score": 2.0}, seed=0)
    same = b.record_experiment(cfg, {"score": 2.0}, seed=0)
    other = a.record_experiment({"part": "p1", "x": 2}, {"score": 1.0}, seed=0)
    assert same.duplicate_of == first.id and other.duplicate_of is None
    # v_tried: still one row per key; the repeat is an attempt and an ok run, not a new seed
    tried = _rows(lead, "SELECT dedup_key, attempts, seeds, ok_runs, authors, mean_value, best_value, summary_outcome"
                        " FROM benchbook.v_tried WHERE project_id=%s ORDER BY first_at")
    assert tried == [
        {"dedup_key": first.dedup_key, "attempts": 2, "seeds": 1, "ok_runs": 2, "authors": 2, "mean_value": 2.0,
         "best_value": 2.0, "summary_outcome": "improved"},
        {"dedup_key": other.dedup_key, "attempts": 1, "seeds": 1, "ok_runs": 1, "authors": 1, "mean_value": 1.0,
         "best_value": 1.0, "summary_outcome": "worse"}]
    # best per partition: one row, and a tie goes to the first run
    best = _rows(lead, "SELECT id, partition, primary_value FROM benchbook.v_best WHERE project_id=%s")
    assert best == [{"id": first.id, "partition": "p1", "primary_value": 2.0}]
    assert lead.status()["best"]["id"] == f"e:{str(first.id)[:8]}"
    board = _rows(lead, "SELECT id, rank FROM benchbook.v_leaderboard WHERE project_id=%s ORDER BY rank")
    assert board == [{"id": first.id, "rank": 1}, {"id": same.id, "rank": 2}, {"id": other.id, "rank": 3}]
    # a repeat is not a second candidate for the partition's best
    cand = _rows(lead, "SELECT ref_id, status, rank FROM benchbook.v_partition_best WHERE project_id=%s")
    assert cand == [{"ref_id": first.id, "status": "candidate", "rank": 1}]
    t = a.check_tried(cfg)
    assert [e.id for e in t.exact] == [same.id, first.id] and t.replicates == 1 and t.best_value == 2.0
    # a better repeat becomes the best without splitting the key or adding a candidate
    better = b.record_experiment(cfg, {"score": 3.0}, seed=0)
    assert better.duplicate_of == first.id and better.outcome == "improved" and better.is_new_best
    assert _rows(lead, "SELECT id FROM benchbook.v_best WHERE project_id=%s") == [{"id": better.id}]
    assert len(_rows(lead, "SELECT 1 AS x FROM benchbook.v_tried WHERE project_id=%s")) == 2
    assert _rows(lead, "SELECT ref_id FROM benchbook.v_partition_best WHERE project_id=%s") == [{"ref_id": first.id}]
    assert "## " in a.brief()  # the brief compiles over repeated keys


def test_notify_mode_repeats_an_invalidated_run(db_url: str, notify_project: str) -> None:
    lead, w = _open(db_url, notify_project, "lead"), _open(db_url, notify_project, "w")
    broken = w.record_experiment({"x": 1}, {"score": 9.0}, seed=0)
    assert broken.id is not None
    lead.decide(broken.id, "invalidated", "human", "evaluator bug")
    note = w.check_tried({"x": 1}).note
    assert note is not None and "stored and reported as a repeat" in note and "rejected" not in note
    again = w.record_experiment({"x": 1}, {"score": 1.0}, seed=0)
    assert again.id is not None and again.duplicate_of == broken.id
    assert again.outcome == "improved"  # judged without the invalidated 9.0
    assert _rows(lead, "SELECT id FROM benchbook.v_best WHERE project_id=%s") == [{"id": again.id}]


@pytest.mark.parametrize("mode", ["refuse", "notify"])
def test_a_writer_outside_the_lock_is_handled_per_mode(db_url: str, project: str, notify_project: str, mode: str,
                                                       monkeypatch: pytest.MonkeyPatch) -> None:
    """The original appears between the check and the insert (a direct INSERT does not take the advisory lock)."""
    w = _open(db_url, notify_project if mode == "notify" else project, "w")
    first = w.record_experiment({"x": 1}, {"score": 1.0}, seed=0)
    real, calls = Ledger._original, [0]

    def blind_once(self: Ledger, conn: Any, key: str, seed: int | None) -> Any:
        calls[0] += 1
        return None if calls[0] == 1 else real(self, conn, key, seed)

    monkeypatch.setattr(Ledger, "_original", blind_once)
    again = w.record_experiment({"x": 1}, {"score": 1.0}, seed=0)
    assert calls[0] == 2 and again.duplicate_of == first.id
    assert (again.id is not None) == (mode == "notify")
    kinds = [h["kind"] for h in _rows(w, "SELECT kind FROM benchbook.dedup_hits WHERE project_id=%s")]
    assert kinds == ["notified" if mode == "notify" else "rejected"]


def test_notify_mode_concurrent_repeats_share_one_original(db_url: str, notify_project: str) -> None:
    results = []

    def go(i: int) -> None:
        results.append(_open(db_url, notify_project, f"w{i}").record_experiment({"same": True}, {"score": 1.0}, seed=7))

    threads = [threading.Thread(target=go, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len({r.id for r in results}) == 8 and all(r.id is not None for r in results)
    originals = [r for r in results if r.duplicate_of is None]
    assert len(originals) == 1
    assert {r.duplicate_of for r in results if r.duplicate_of is not None} == {originals[0].id}


def test_duplicate_of_cannot_bypass_the_unique_index(db_url: str, notify_project: str) -> None:
    w = _open(db_url, notify_project, "w")
    first = w.record_experiment({"x": 1}, {"score": 1.0}, seed=0)
    repeat = w.record_experiment({"x": 1}, {"score": 1.0}, seed=0)
    other = w.record_experiment({"x": 2}, {"score": 1.0}, seed=0)
    insert = ("INSERT INTO benchbook.experiments(project_id,author,config,dedup_key,seed,run_status,outcome,"
              "duplicate_of) VALUES (%s,'w','{}',%s,%s,%s,%s,%s)")
    cases: list[tuple[type[Exception], tuple[Any, ...]]] = [
        # an undeclared duplicate is refused in a notify project too
        (psycopg.errors.UniqueViolation, (first.dedup_key, 0, "ok", "unscored", None)),
        # duplicate_of must name the original of the same key and seed, and only an ok run can be a repeat
        (psycopg.errors.CheckViolation, (first.dedup_key, 0, "ok", "unscored", other.id)),
        (psycopg.errors.CheckViolation, (first.dedup_key, 1, "ok", "unscored", first.id)),
        (psycopg.errors.CheckViolation, (first.dedup_key, 0, "ok", "unscored", repeat.id)),
        (psycopg.errors.CheckViolation, (first.dedup_key, 0, "error", "failed", first.id)),
        (psycopg.errors.CheckViolation, (first.dedup_key, 0, "ok", "unscored", uuid.uuid4())),  # no such run
    ]
    for error, values in cases:
        with psycopg.connect(db_url) as conn, pytest.raises(error):
            conn.execute(insert, (notify_project, *values))
    with psycopg.connect(db_url) as conn:
        conn.execute(insert, (notify_project, first.dedup_key, 0, "ok", "unscored", first.id))


# adapters -----------------------------------------------------------------------------------------
def test_notice_reaches_the_mcp_tool_result(notify_project: str) -> None:
    mcp = build(notify_project, "all")
    args: dict[str, Any] = {"agent": "implementer", "config": {"part": "p1", "x": 1}, "metrics": {"score": 2.0},
                            "seed": 0, "code_ref": "a1b2c3d"}
    first = call(mcp, "benchbook_record_experiment", **args)
    assert first["id"].startswith("e:") and first["duplicate_of"] is None and "notice" not in first
    again = call(mcp, "benchbook_record_experiment", **{**args, "agent": "verifier"})
    assert again["id"].startswith("e:") and again["id"] != first["id"] and again["outcome"] == "no_change"
    assert again["duplicate_of"] == first["id"] and first["id"] in again["notice"]
    assert call(mcp, "benchbook_status", agent="lead")["dedup"]["rejected"] == 0
    assert call(mcp, "benchbook_get", agent="lead", id=again["id"])["record"]["duplicate_of"] is not None


def _cli(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    with pytest.raises(SystemExit) as exit_info:
        cli.main(list(argv))
    out = capsys.readouterr()
    code = exit_info.value.code
    return (code if isinstance(code, int) else 0), out.out, out.err


def test_notice_reaches_cli_json_output(db_url: str, notify_project: str, tmp_path: Path,
                                        monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    # In process: the temporary pack exists only here. The refuse-mode exit code 2 is in tests/test_cli.py.
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BENCHBOOK_DATABASE_URL", db_url)
    for key in ("json", "project", "agent"):
        monkeypatch.setitem(cli.STATE, key, False if key == "json" else None)
    record = ("--project", notify_project, "exp", "record", "--config", '{"part": "p1", "x": 1}',
              "--metrics", '{"score": 2.0}', "--seed", "0", "--code-ref", "a1b2c3d")
    code, out, err = _cli(capsys, "--agent", "implementer", *record, "--json")
    first = json.loads(out)
    assert code == 0 and first["ok"] and first["duplicate_of"] is None and "notice" not in first

    code, out, err = _cli(capsys, "--agent", "verifier", *record, "--json")
    again = json.loads(out)
    assert code == 0, err  # recorded, not exit 2
    assert again["ok"] is True and again["id"].startswith("e:") and again["id"] != first["id"]
    assert again["duplicate_of"] == first["id"] and first["id"] in again["notice"]
    # the capture hook reads this response as a new record written by this agent
    assert hooks._receipt(out.strip(), "e", False) == again["id"]

    monkeypatch.setitem(cli.STATE, "json", False)
    code, out, err = _cli(capsys, "--agent", "verifier", *record)
    assert code == 0 and first["id"] in err and err.startswith("notice: ")
    third = hooks._receipt(out.strip(), "e", False)  # stdout keeps the one-line receipt shape
    assert third is not None and third not in (first["id"], again["id"])


def test_hook_receipt_tells_a_recorded_repeat_from_a_refusal() -> None:
    new, old = "e:aaaaaaaa", "e:bbbbbbbb"
    assert hooks._receipt(json.dumps({"id": new, "duplicate_of": old, "notice": "x"}), "e", True) == new
    assert hooks._receipt(json.dumps({"id": None, "duplicate_of": old}), "e", True) is None  # refused
    assert hooks._receipt(json.dumps({"ok": False, "duplicate_of": old}), "e", False) is None  # refused (CLI)
    assert hooks._receipt(json.dumps({"id": "c:aaaaaaaa", "duplicate_of": "c:aaaaaaaa"}), "c", True) is None


# migration ----------------------------------------------------------------------------------------
def test_migration_keeps_recorded_rows_and_the_refusal(db_url: str) -> None:
    from alembic import command

    name = f"benchbook_dedup_{uuid.uuid4().hex[:8]}"
    url = dbconn.ensure_database(db_url, name)
    insert = ("INSERT INTO benchbook.experiments(project_id,author,config,dedup_key,seed,run_status,outcome)"
              " VALUES ('old','w','{}','k',%s,'ok','unscored')")
    try:
        command.upgrade(dbmigrate.alembic_config(url), "0007")
        with psycopg.connect(url, autocommit=True) as c:
            c.execute("INSERT INTO benchbook.projects(id,title,goal,pack) VALUES ('old','old','g','core')")
            c.execute(insert, (0,))
            c.execute(insert, (None,))
            c.execute("INSERT INTO benchbook.dedup_hits(project_id,kind,dedup_key) VALUES ('old','rejected','k')")
        before, after = dbmigrate.migrate(url)
        assert before == "0007" and after == dbmigrate.head() and after >= "0009"
        with psycopg.connect(url, autocommit=True) as c:
            assert c.execute("SELECT count(*), count(duplicate_of) FROM benchbook.experiments").fetchone() == (2, 0)
            for seed in (0, None):
                with pytest.raises(psycopg.errors.UniqueViolation):
                    c.execute(insert, (seed,))
            c.execute(insert, (1,))
            c.execute("INSERT INTO benchbook.dedup_hits(project_id,kind,dedup_key) VALUES ('old','notified','k')")
            with pytest.raises(psycopg.errors.CheckViolation):
                c.execute("INSERT INTO benchbook.dedup_hits(project_id,kind,dedup_key) VALUES ('old','other','k')")
    finally:
        with psycopg.connect(db_url, autocommit=True) as c:
            c.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
