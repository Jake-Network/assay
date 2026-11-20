"""assay ar operations on real git checkouts of the CPU simulator (§8, §16.1). Runs take ~0.05 s each."""
from __future__ import annotations

import hashlib
import shutil
import subprocess
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from pathlib import Path
from threading import Event

import pytest

from assay import Ledger
from assay.integrations.autoresearch import ops
from assay.integrations.autoresearch.checkout import Checkout
from assay.integrations.autoresearch.code import code_hash, normalize, short_hash
from assay.integrations.autoresearch.referee import tick
from assay.tokens import estimate_tokens

SIM = Path(__file__).resolve().parent.parent / "examples" / "autoresearch-sim"
CMD = f"{sys.executable} train.py"


@pytest.fixture(autouse=True)
def sim_env(db_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ASSAY_DATABASE_URL", db_url)
    monkeypatch.setenv("ASSAY_AR_HW", "test-cpu")
    monkeypatch.setenv("AR_SIM_SECONDS", "0.05")
    monkeypatch.setenv("AR_SIM_LUCKY", "0")
    monkeypatch.setenv("AR_SIM_NOISE", "0.0005")
    for k, v in {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid", "GIT_COMMITTER_NAME": "t",
                 "GIT_COMMITTER_EMAIL": "t@example.invalid"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("CUDA_VISIBLE_DEVICES", raising=False)


def git(root: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=True).stdout


def checkout(base: Path, name: str) -> Path:
    root = base / name
    root.mkdir()
    for f in ("prepare.py", "train.py"):
        shutil.copy(SIM / f, root / f)
    git(root, "init", "-q")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "upstream")
    return root


def set_knob(root: Path, name: str, value: str, commit: bool = True) -> None:
    import re

    p = root / "train.py"
    p.write_text(re.sub(rf"^{name} = [^#\n]*?(\s*#.*)?$", lambda m: f"{name} = {value}{m.group(1) or ''}",
                        p.read_text(), count=1, flags=re.M))
    if commit:
        git(root, "commit", "-qam", f"{name}={value}")


@pytest.fixture
def lab(tmp_path: Path) -> tuple[str, list[Path]]:
    pid = f"ar-{uuid.uuid4().hex[:10]}"
    roots = [checkout(tmp_path, n) for n in ("a", "b", "c")]
    ops.init(pid, start=roots[0])
    return pid, roots


def ctx(pid: str, root: Path, agent: str) -> ops.Ctx:
    return ops.open_ctx(project=pid, agent=agent, start=root)


def calibrate(pid: str, roots: list[Path]) -> ops.Ctx:
    a = ctx(pid, roots[0], "a")
    assert ops.run(a, "baseline", CMD)["verdict"].startswith("KEEP (baseline")
    assert len(ops.replicate(ctx(pid, roots[1], "b"), cmd=CMD)) == 1
    assert len(ops.replicate(ctx(pid, roots[2], "c"), cmd=CMD)) == 1
    return a


def test_init(lab: tuple[str, list[Path]]) -> None:
    pid, (root, *_) = lab
    assert "LOOP FOREVER" in (root / "program.assay.md").read_text()
    assert ".assay/ar_state.json" in (root / ".gitignore").read_text()
    assert f'project = "{pid}"' in (root / ".assay" / "config.toml").read_text()
    lg = Ledger.open(project=pid, agent="human")
    assert lg.project.pack == "ml-research" and lg.project.settings["partition_key"] == "partition"
    assert lg.as_agent("referee").role == "orchestrator"
    again = ops.init(pid, start=root)
    assert not again["created"] and not again["wrote_program"] and not again["program_differs"]


def test_run_refuses_duplicates_and_records_crashes(lab: tuple[str, list[Path]]) -> None:
    pid, (root, *_) = lab
    a = ctx(pid, root, "a")
    first = ops.run(a, "baseline", CMD)
    assert first["outcome"] == "improved" and first["value"] is not None
    with pytest.raises(ops.Refused, match="DUPLICATE"):
        ops.run(a, "baseline again", CMD)
    (root / "train.py").write_text((root / "train.py").read_text() + "\n# a comment does not make it new\n")
    with pytest.raises(ops.Refused):
        ops.run(a, "baseline with a comment", CMD)
    git(root, "checkout", "train.py")
    set_knob(root, "DEPTH", "14")  # simulated OOM
    crash = ops.run(a, "deeper", CMD)
    assert crash["verdict"].startswith("CRASH") and crash["run_status"] == "error"
    rec = a.lg.get(crash["id"])["record"]
    assert rec["outcome"] == "failed" and "OutOfMemoryError" in rec["notes"]
    with pytest.raises(ops.Refused, match="retry-failed"):
        ops.run(a, "deeper again", CMD)
    assert ops.run(a, "deeper again", CMD, retry_failed=True)["run_status"] == "error"
    rows = (root / "results.tsv").read_text().splitlines()
    assert rows[0] == "commit\tval_bpb\tmemory_gb\tstatus\tdescription"
    assert rows[1].split("\t")[3:] == ["keep", "baseline"] and rows[1].split("\t")[0] == git(
        root, "rev-parse", "--short=7", "HEAD~1").strip()
    assert rows[2].split("\t")[1:4] == ["0.000000", "0.0", "crash"]
    assert len(rows) == 4


def test_running_duplicate_is_refused(lab: tuple[str, list[Path]]) -> None:
    pid, (ra, rb, _) = lab
    a, b = ctx(pid, ra, "a"), ctx(pid, rb, "b")
    code = code_hash((ra / "train.py").read_bytes())
    b.lg.create_intent("baseline", {"partition": b.partition, "code_key": f"{b.partition}|{code}"})
    with pytest.raises(ops.Refused, match="running right now"):
        ops.run(a, "baseline", CMD)
    assert not a.lg.sql_internal("SELECT 1 FROM assay.experiments WHERE project_id=%s", (pid,))


def test_parent_changed_knobs_and_lineage(lab: tuple[str, list[Path]]) -> None:
    pid, (root, *_) = lab
    a = ctx(pid, root, "a")
    base = ops.run(a, "baseline", CMD)
    set_knob(root, "MATRIX_LR", "0.06")
    r = ops.run(a, "MATRIX_LR to 0.06", CMD)
    assert r["changed"] == {"MATRIX_LR": [0.04, 0.06]} and r["parent_from"] == "HEAD~1"
    assert r["parent"] == base["code"]
    set_knob(root, "WARMDOWN_RATIO", "0.65", commit=False)  # not committed: the parent is HEAD
    r2 = ops.run(a, "warmdown", CMD)
    assert r2["parent_from"] == "HEAD" and r2["changed"] == {"WARMDOWN_RATIO": [0.5, 0.65]}
    lin = a.lg.sql_internal("SELECT id, depth FROM assay.v_lineage WHERE project_id=%s ORDER BY depth", (pid,))
    assert [x["depth"] for x in lin] == [0, 1, 2]
    kh = a.lg.sql_internal("SELECT knob FROM assay.v_knob_history WHERE project_id=%s ORDER BY knob", (pid,))
    assert [x["knob"] for x in kh] == ["MATRIX_LR", "WARMDOWN_RATIO"]


def test_claims(lab: tuple[str, list[Path]]) -> None:
    pid, (ra, rb, rc) = lab
    a, b, c = ctx(pid, ra, "a"), ctx(pid, rb, "b"), ctx(pid, rc, "c")
    first = ops.claim(a, "raise matrix lr to 0.06", {"MATRIX_LR": 0.06})
    with pytest.raises(ops.Refused, match="already working"):
        ops.claim(b, "a different wording", {"MATRIX_LR": 0.060})
    with pytest.raises(ops.Refused, match="very similar"):
        ops.claim(b, "Raise matrix LR to 0.06!", {"MATRIX_LR": 0.06, "DEPTH": 10})
    forced = ops.claim(b, "Raise matrix LR to 0.06!", {"MATRIX_LR": 0.06, "DEPTH": 10}, force=True)
    assert forced["warnings"]
    second = ops.claim(a, "something else", {"DEPTH": 9})
    assert [t.title for t in a.lg.my_intents()] == ["something else"]  # the older intent was released
    ops.claim(c, "now free", {"MATRIX_LR": 0.06})
    assert first["intent"] != second["intent"]


def test_replicate_is_isolated_and_partitioned(lab: tuple[str, list[Path]]) -> None:
    pid, (ra, rb, rc) = lab
    a, b = ctx(pid, ra, "a"), ctx(pid, rb, "b")
    ops.run(a, "baseline", CMD)
    set_knob(rb, "DEPTH", "10", commit=False)  # b is mid-edit
    before = ((rb / "train.py").read_bytes(), git(rb, "status", "--porcelain"), git(rb, "rev-parse", "HEAD"))
    done = ops.replicate(b, cmd=CMD)
    assert len(done) == 1 and done[0]["run_status"] == "ok" and done[0]["kind"] == "calibration"
    assert ((rb / "train.py").read_bytes(), git(rb, "status", "--porcelain"), git(rb, "rev-parse", "HEAD")) == before
    assert not (rb / "results.tsv").exists()
    # a checkout whose prepare.py differs is another partition and gets no task
    (rc / "prepare.py").write_text((rc / "prepare.py").read_text().replace("TIME_BUDGET = 3", "TIME_BUDGET = 4"))
    c = ctx(pid, rc, "c")
    assert c.partition != a.partition and ops.replicate(c, cmd=CMD) == []
    assert ops.replicate(a, cmd=CMD) == []  # never your own run


def test_sync_adopts_verified_best_and_leaves_refuted_lines(lab: tuple[str, list[Path]],
                                                             monkeypatch: pytest.MonkeyPatch) -> None:
    pid, roots = lab
    # Low noise: with sigma 0.0005 and epsilon estimated from 3 runs, the lucky candidate's replications alone
    # beat the baseline by more than epsilon in roughly one run in seven (the noise-estimate weakness, A9).
    monkeypatch.setenv("AR_SIM_NOISE", "0.0001")
    a = calibrate(pid, roots)
    b, c = ctx(pid, roots[1], "b"), ctx(pid, roots[2], "c")
    assert ops.sync(a)["adopted"] is False  # a already has the verified baseline
    # a lucky candidate: UNEMBEDDING_LR does not affect the simulated loss
    set_knob(roots[0], "UNEMBEDDING_LR", "0.005")
    monkeypatch.setenv("AR_SIM_LUCKY", "1")
    lucky = ops.run(a, "unembedding lr", CMD)
    monkeypatch.setenv("AR_SIM_LUCKY", "0")
    assert lucky["verdict"].startswith("KEEP (candidate")
    assert ops.sync(a)["adopted"] is False  # its own candidate looks better than the verified mean
    ops.replicate(b, cmd=CMD)
    ops.replicate(c, cmd=CMD)
    assert a.lg.get(lucky["id"])["links_out"]
    s = ops.sync(a)
    assert s["adopted"] and "refuted" in s["why"] and s["committed"], s
    assert "UNEMBEDDING_LR = 0.004" in (roots[0] / "train.py").read_text()
    assert git(roots[0], "log", "-1", "--format=%s").startswith("adopt verified best")
    # a real improvement replicates and becomes the verified best that others adopt
    set_knob(roots[1], "MATRIX_LR", "0.06")
    assert ops.run(b, "matrix lr 0.06", CMD)["verdict"].startswith("KEEP (candidate")
    ops.replicate(a, cmd=CMD)
    ops.replicate(c, cmd=CMD)
    s = ops.sync(c)
    assert s["adopted"] and s["status"] == "verified"
    assert "MATRIX_LR = 0.06" in (roots[2] / "train.py").read_text()
    st = ops.status(c.lg)["partitions"][0]
    assert st["verified_best"]["author"] == "b" and st["results"].get("refuted") == 1
    assert st["calibration"]["status"] == "ok" and st["gpu_minutes"]["replication_share"] > 0
    brief = c.lg.brief(budget_tokens=1500, partition=c.partition)
    assert "## Verified best" in brief and "MATRIX_LR: 0.06 base" in brief and "assay ar claim" in brief
    assert estimate_tokens(brief) <= 1575
    tiny = c.lg.brief(budget_tokens=200, partition=c.partition)
    assert estimate_tokens(tiny) <= 210 and "Next:" in tiny


def test_unrecorded_runs_are_queued_and_flushed(lab: tuple[str, list[Path]],
                                                monkeypatch: pytest.MonkeyPatch) -> None:
    pid, (root, *_) = lab
    a = ctx(pid, root, "a")

    def down(*args: object, **kw: object) -> None:
        raise ConnectionError("database unreachable")

    monkeypatch.setattr(a.lg, "record_experiment", down)
    r = ops.run(a, "baseline", CMD)
    assert r["outcome"] == "queued" and (root / ".assay" / "ar_pending.jsonl").exists()
    assert len(a.lg.my_intents()) == 1  # A5: the code stays reserved until the run is in the ledger
    a2 = ctx(pid, root, "a")  # every ar command flushes the queue first
    assert not (root / ".assay" / "ar_pending.jsonl").exists()
    assert not list((root / ".assay").glob("*.flushing"))
    assert len(a2.lg.sql_internal("SELECT 1 FROM assay.experiments WHERE project_id=%s", (pid,))) == 1
    assert a2.lg.my_intents() == []  # completed once recorded


def test_unrecorded_replication_keeps_its_task(lab: tuple[str, list[Path]], monkeypatch: pytest.MonkeyPatch) -> None:
    """A5: a replication that could not be recorded leaves its task claimed, so the referee cannot judge the
    candidate without it; the flush records the run and completes the task."""
    pid, roots = lab
    a = ctx(pid, roots[0], "a")
    ops.run(a, "baseline", CMD)
    b = ctx(pid, roots[1], "b")

    def down(*args: object, **kw: object) -> None:
        raise ConnectionError("database unreachable")

    monkeypatch.setattr(b.lg, "record_experiment", down)
    done = ops.replicate(b, cmd=CMD)
    assert done[0]["queued"] and done[0]["id"] is None
    q = "SELECT status FROM assay.tasks WHERE project_id=%s AND 'calibration'=ANY(tags) ORDER BY created_at"
    assert [t["status"] for t in a.lg.sql_internal(q, (pid,))] == ["claimed", "open"]
    ctx(pid, roots[1], "b")  # a fresh ledger connection: the flush records the run
    assert [t["status"] for t in a.lg.sql_internal(q, (pid,))] == ["done", "open"]


def test_changed_run_is_retained_when_database_drops_after_training(
    lab: tuple[str, list[Path]], monkeypatch: pytest.MonkeyPatch,
) -> None:
    from assay.integrations.autoresearch.runner import RunResult

    pid, (root, *_) = lab
    a = ctx(pid, root, "a")
    parent = ops.run(a, "baseline", CMD)
    set_knob(root, "MATRIX_LR", "0.06")

    def down(*args: object, **kwargs: object) -> None:
        raise ConnectionError("database unreachable")

    def finish(*args: object, **kwargs: object) -> tuple[RunResult, None]:
        monkeypatch.setattr(a.lg, "sql_internal", down)
        monkeypatch.setattr(a.lg, "record_experiment", down)
        return RunResult("ok", 0, {"val_bpb": parent["value"] - 0.01}), None

    monkeypatch.setattr(ops, "_run_isolated", finish)
    child = ops.run(a, "matrix lr 0.06", CMD)
    assert child["outcome"] == "queued" and child["keep"]
    assert a.co.pending_path().is_file() and "\tkeep\tmatrix lr 0.06" in (root / "results.tsv").read_text()
    a2 = ctx(pid, root, "a")
    saved = a2.lg.search("matrix lr 0.06", kind="experiment")[0]
    assert a2.lg.get(saved["id"])["links_out"][0]["dst"] == parent["id"]
    assert not a2.co.pending_path().exists() and not a2.lg.my_intents()


def test_failed_run_replay_does_not_record_again_after_completion_error(
    lab: tuple[str, list[Path]], monkeypatch: pytest.MonkeyPatch,
) -> None:
    pid, (root, *_) = lab
    a = ctx(pid, root, "a")

    def down(*args: object, **kwargs: object) -> None:
        raise ConnectionError("connection lost before marking the task done")

    monkeypatch.setattr(a.lg, "complete_task", down)
    assert ops.run(a, "failed once", "exit 9")["run_status"] == "error"
    assert a.co.pending_path().exists() and a.lg.my_intents()
    # Simulate an abandoned batch containing a damaged record and an otherwise replayable run.
    batch = ops._take_pending(a.co.pending_path())[0]
    batch.write_bytes(b"\xff\xfe\n" + batch.read_bytes())
    a2 = ctx(pid, root, "a")
    rows = a2.lg.sql_internal("SELECT run_status FROM assay.experiments WHERE project_id=%s", (pid,))
    assert rows == [{"run_status": "error"}]
    assert not a2.lg.my_intents() and not a2.co.pending_path().exists()
    assert batch.with_suffix(".invalid").exists()


def test_pending_queue_take_is_atomic(tmp_path: Path) -> None:
    """A5: lines appended while a flush is in progress go to a fresh queue file and are never lost."""
    p = tmp_path / ".assay" / "ar_pending.jsonl"
    ops._append_line(p, '{"n": 1}')
    ops._append_line(p, '{"n": 2}')
    taken = ops._take_pending(p)
    assert len(taken) == 1 and not p.exists()
    ops._append_line(p, '{"n": 3}')  # a run finishing during the flush
    assert taken[0].read_text().splitlines() == ['{"n": 1}', '{"n": 2}'] and p.read_text() == '{"n": 3}\n'
    assert ops._take_pending(p)[0].read_text() == '{"n": 3}\n'
    assert ops._take_pending(p) == []  # the active queue is empty


def test_pending_rotation_waits_until_open_writer_finishes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    p = tmp_path / "ar_pending.jsonl"
    p.write_text('{"n": 1}\n')
    open_file = Path.open
    writer_open = Event()
    finish_write = Event()

    def open_with_writer_pause(path: Path, *args: object, **kwargs: object) -> object:
        f = open_file(path, *args, **kwargs)  # type: ignore[arg-type]
        if path == p and args == ("ab",):
            writer_open.set()
            assert finish_write.wait(5)
        return f

    monkeypatch.setattr(Path, "open", open_with_writer_pause)
    with ThreadPoolExecutor(max_workers=2) as pool:
        writer = pool.submit(ops._append_line, p, '{"n": 2}')
        try:
            assert writer_open.wait(5)
            rotation = pool.submit(ops._take_pending, p)
            with pytest.raises(TimeoutError):
                rotation.result(timeout=0.1)
        finally:
            finish_write.set()
        writer.result(timeout=5)
        batch = rotation.result(timeout=5)[0]
    assert batch.read_text().splitlines() == ['{"n": 1}', '{"n": 2}']


def test_notes_and_ideas(lab: tuple[str, list[Path]]) -> None:
    pid, (root, *_) = lab
    a = ctx(pid, root, "a")
    r = ops.run(a, "baseline", CMD)
    n = ops.note(a, "the baseline is stable")
    got = a.lg.get(n["id"])
    assert got["links_in"][0]["src"] == r["id"]
    i = ops.idea(a, "try a deeper model", {"DEPTH": 10})
    assert a.lg.get(i["id"])["record"]["payload"] == {"partition": a.partition, "knobs": {"DEPTH": 10}}
    tick(a.lg)


def test_viewer_pages(lab: tuple[str, list[Path]], monkeypatch: pytest.MonkeyPatch) -> None:
    from starlette.testclient import TestClient

    from assay.web.app import create_app

    pid, roots = lab
    monkeypatch.setenv("AR_SIM_NOISE", "0.0001")  # epsilon from 3 noisy runs can swallow the 0.004 gain
    a = calibrate(pid, roots)
    set_knob(roots[0], "MATRIX_LR", "0.06")
    r = ops.run(a, "matrix lr 0.06", CMD)
    assert r["outcome"] == "improved", r
    ops.claim(ctx(pid, roots[1], "b"), "try depth 10", {"DEPTH": 10})
    app = create_app(pid, with_mcp=False)
    with TestClient(app, base_url="http://127.0.0.1:7466") as client:
        page = client.get("/")
        assert page.status_code == 200 and "verified best" in page.text and "try depth 10" in page.text
        assert a.partition in page.text and "/lineage" in page.text
        prog = client.get(f"/api/partition_progress?p={a.partition}").json()
        assert prog["kind"] == "partition" and any(v is not None for v in prog["verified"])
        assert any(v is not None for v in prog["candidate"])
        lin = client.get("/lineage")
        assert lin.status_code == 200 and r["id"] in lin.text
        assert "MATRIX_LR" in client.get("/knobs").text
        rec = client.get(f"/r/{r['id']}")
        assert rec.status_code == 200 and "train.py vs parent" in rec.text and "+MATRIX_LR = 0.06" in rec.text


def test_import_results_tsv(lab: tuple[str, list[Path]]) -> None:
    pid, (root, *_) = lab
    rows = ["commit\tval_bpb\tmemory_gb\tstatus\tdescription"]
    base = git(root, "rev-parse", "--short=7", "HEAD").strip()
    rows.append(f"{base}\t1.002000\t44.0\tkeep\tbaseline")
    set_knob(root, "MATRIX_LR", "0.06")
    rows.append(f"{git(root, 'rev-parse', '--short=7', 'HEAD').strip()}\t0.998000\t44.0\tkeep\tlr 0.06")
    set_knob(root, "DEPTH", "14")
    rows.append(f"{git(root, 'rev-parse', '--short=7', 'HEAD').strip()}\t0.000000\t0.0\tcrash\tdepth 14 OOM")
    rows.append("deadbee\t0.990000\t44.0\tkeep\tlost commit")
    (root / "old.tsv").write_text("\n".join(rows) + "\n")
    a = ctx(pid, root, "a")
    r = ops.import_results(a, root / "old.tsv", hw="test-cpu")
    assert r == {"imported": 3, "duplicates": 0, "missing_commit": 1, "rows": 4, "referee": r["referee"]}
    exps = a.lg.sql_internal("SELECT outcome, config->'changed' AS ch FROM assay.experiments WHERE project_id=%s"
                             " ORDER BY created_at", (pid,))
    assert [e["outcome"] for e in exps] == ["improved", "improved", "failed"]
    assert exps[1]["ch"] == {"MATRIX_LR": [0.04, 0.06]}
    again = ops.import_results(a, root / "old.tsv", hw="test-cpu")
    assert again["imported"] == 0 and again["duplicates"] == 3  # nothing is imported twice



def test_keep_compares_with_the_parent(lab: tuple[str, list[Path]], monkeypatch: pytest.MonkeyPatch) -> None:
    """A1: a child that beats the verified best but not its own parent is DISCARD, as in upstream."""
    pid, roots = lab
    monkeypatch.setenv("AR_SIM_NOISE", "0.00001")
    a = calibrate(pid, roots)
    set_knob(roots[0], "MATRIX_LR", "0.06")  # about 0.004 better than the baseline
    parent = ops.run(a, "matrix lr 0.06", CMD)
    assert parent["keep"] and parent["verdict"].startswith("KEEP (candidate")
    set_knob(roots[0], "MATRIX_LR", "0.05")  # better than the baseline, worse than its parent
    child = ops.run(a, "matrix lr 0.05", CMD)
    assert child["outcome"] == "improved" and not child["keep"]
    assert child["verdict"].startswith("DISCARD (not better than its parent")
    assert child["parent_value"] == pytest.approx(parent["value"])
    assert (roots[0] / "results.tsv").read_text().splitlines()[-1].split("\t")[3] == "discard"
    assert a.co.load_state()["last_code_hash"] == code_hash(git(roots[0], "show", "HEAD~1:train.py"))
    row = a.lg.get(child["id"])["record"]
    tr = row["trace_ref"]
    assert tr["beats_parent"] is False and tr["kept"] is False and tr["cmd"] == CMD
    assert any(t.spec.get("origin") == row["id"] for t in a.lg.list_tasks())


def test_local_keep_does_not_require_beating_the_verified_baseline(
    lab: tuple[str, list[Path]], monkeypatch: pytest.MonkeyPatch,
) -> None:
    from assay.integrations.autoresearch.runner import RunResult

    pid, roots = lab
    a = calibrate(pid, roots)
    baseline, _ = a.lg.baseline(a.partition)
    assert baseline is not None
    for lr, delta in [("0.05", 0.03), ("0.06", 0.015)]:
        set_knob(roots[0], "MATRIX_LR", lr)
        probe = RunResult("ok", 0, {"val_bpb": baseline + delta})
        monkeypatch.setattr(ops, "_run_isolated", lambda *a, run=probe, **k: (run, None))
        result = ops.run(a, f"matrix lr {lr}", CMD)
    assert result["keep"] and result["outcome"] == "worse" and result["verdict"] == "KEEP (local improvement)"
    assert a.lg.get(result["id"])["record"]["trace_ref"]["kept"]


def test_legacy_partition_reuses_history_intents_and_replication_tasks(
    lab: tuple[str, list[Path]], monkeypatch: pytest.MonkeyPatch,
) -> None:
    pid, roots = lab
    a = ctx(pid, roots[0], "a")
    src = a.co.train_bytes()
    old_harness = normalize((roots[0] / "prepare.py").read_bytes())
    old_code = normalize(src)
    assert old_harness is not None and old_code is not None
    legacy_part = f"{a.hw}/{short_hash(hashlib.sha256(old_harness.encode()).hexdigest())}"
    legacy_ref = hashlib.sha256(old_code.encode()).hexdigest()
    assert legacy_part != a.partition and legacy_ref != code_hash(src)
    intent = a.lg.create_intent("old worker is running", {
        "partition": legacy_part, "code_key": f"{legacy_part}|{legacy_ref}",
    }, exclusive=["code_key"])
    b = ctx(pid, roots[1], "b")
    assert b.partition == legacy_part
    with pytest.raises(ops.Refused, match="same code is running"):
        ops.run(b, "already running", CMD)
    a.lg.complete_task(intent.id)
    original = a.lg.record_experiment(
        {"partition": legacy_part, "knobs": {}, "changed": {}, "parent": None, "description": "old baseline"},
        {"val_bpb": 1.0}, seed=0, code_ref=legacy_ref,
        artifacts=[{"uri": a.lg.put_blob(src), "kind": "train.py"}])
    assert original.id is not None
    with pytest.raises(ops.Refused, match="already ran"):
        ops.run(b, "already recorded", CMD)
    tick(a.lg)
    # Simulate another runtime whose native legacy hash has a different spelling.
    native_keys = Checkout.partition_keys
    monkeypatch.setattr(Checkout, "partition_keys", lambda co, hw: (native_keys(co, hw)[0], "other/previous-hash"))
    c = ctx(pid, roots[2], "c")
    assert c.partition == legacy_part
    assert ops.replicate(b, cmd=CMD)[0]["run_status"] == "ok"
    assert ops.replicate(c, cmd=CMD)[0]["run_status"] == "ok"
    best = ops.partition_best(c)
    assert best is not None and best["origin_id"] == original.id


def test_interrupted_run_is_recorded(lab: tuple[str, list[Path]], monkeypatch: pytest.MonkeyPatch) -> None:
    """A4: SIGTERM during the training kills it (runner) and the run is still recorded before re-raising."""
    import signal

    from assay.integrations.autoresearch.runner import Interrupted

    pid, (root, *_) = lab
    a = ctx(pid, root, "a")

    def killed(cmd: str, cwd: Path, log_path: Path, *args: object, **kw: object) -> None:
        log_path.write_text("step 1\nstep 2\n")
        raise Interrupted(signal.SIGTERM)

    monkeypatch.setattr(ops, "run_command", killed)
    with pytest.raises(Interrupted):
        ops.run(a, "baseline", CMD)
    rows = a.lg.sql_internal("SELECT run_status, notes FROM assay.experiments WHERE project_id=%s", (pid,))
    assert len(rows) == 1 and rows[0]["run_status"] == "error" and "interrupted (SIGTERM)" in rows[0]["notes"]
    assert a.lg.my_intents() == []
    assert (root / "results.tsv").read_text().splitlines()[-1].split("\t")[3] == "crash"
    with pytest.raises(ops.Refused, match="retry-failed"):
        ops.run(a, "baseline", CMD)


def test_runs_the_hashed_bytes(lab: tuple[str, list[Path]], monkeypatch: pytest.MonkeyPatch) -> None:
    """A8: editing train.py while a run starts does not change what runs; the stored blob is what ran."""
    pid, (root, *_) = lab
    a = ctx(pid, root, "a")
    original = (root / "train.py").read_bytes()
    real = ops._isolated_dir

    def edit_meanwhile(co: ops.Checkout, src: bytes) -> Path:
        tmp = real(co, src)
        set_knob(root, "DEPTH", "14", commit=False)  # would crash (simulated OOM) if it were run
        return tmp

    monkeypatch.setattr(ops, "_isolated_dir", edit_meanwhile)
    r = ops.run(a, "baseline", CMD)
    assert r["run_status"] == "ok" and r["code"] == code_hash(original)[:8]
    rec = a.lg.get(r["id"])["record"]
    uri = ops.blob_of(rec)
    assert uri is not None and a.lg.get_blob(uri) == original


def test_over_budget_runs_are_invalid(lab: tuple[str, list[Path]]) -> None:
    """A9: a run that trains longer than the time budget is recorded invalid, not ok."""
    pid, (root, *_) = lab
    Ledger.open(project=pid, agent="human").update_settings({"time_budget_s": 0.01, "time_budget_slack_s": 0})
    a = ctx(pid, root, "a")
    r = ops.run(a, "baseline", CMD)
    assert r["run_status"] == "invalid" and r["verdict"].startswith("INVALID (over the time budget")
    assert a.lg.get(r["id"])["record"]["outcome"] == "failed"
    assert ops.time_budget(a)[0] == 0.01


def test_time_budget_defaults_to_prepare(lab: tuple[str, list[Path]]) -> None:
    pid, (root, *_) = lab
    assert ops.time_budget(ctx(pid, root, "a")) == (3.0, 30.0, 240.0)  # TIME_BUDGET = 3 in the simulator


def test_replicate_uses_the_task_command(lab: tuple[str, list[Path]]) -> None:
    """A9: a task that carries the origin's command runs it, whatever the replicator's --cmd is."""
    pid, roots = lab
    a = ctx(pid, roots[0], "a")
    r = ops.run(a, "baseline", CMD)
    origin = a.lg.get(r["id"])["record"]
    assert origin["trace_ref"]["cmd"] == CMD
    ref = Ledger.open(project=pid, agent="referee")
    ref.create_tasks([{"title": "replicate with cmd", "tags": ["replicate"], "priority": 100, "spec": {
        "kind": "replicate", "partition": a.partition, "origin": str(origin["id"]), "code_ref": origin["code_ref"],
        "code_blob": ops.blob_of(origin), "seed": 7, "exclude_agents": ["a"], "cmd": CMD}}])
    done = ops.replicate(ctx(pid, roots[1], "b"), cmd="exit 9")
    assert done[0]["seed"] == 7 and done[0]["run_status"] == "ok", done


def test_status_counts_only_refused_duplicates(lab: tuple[str, list[Path]]) -> None:
    """B5: a refusal later overridden with --retry-failed is not a blocked duplicate."""
    pid, (root, *_) = lab
    a = ctx(pid, root, "a")
    ops.run(a, "baseline", CMD)
    with pytest.raises(ops.Refused):
        ops.run(a, "baseline again", CMD)
    set_knob(root, "DEPTH", "14")
    ops.run(a, "deeper", CMD)
    with pytest.raises(ops.Refused):
        ops.run(a, "deeper again", CMD)
    ops.run(a, "deeper again", CMD, retry_failed=True)
    st = ops.status(a.lg)["partitions"][0]
    assert st["duplicates_blocked"] == 1


def test_import_needs_the_hardware(lab: tuple[str, list[Path]]) -> None:
    """A9: results.tsv does not say which GPU produced it, so import refuses to guess."""
    from assay.errors import AssayError

    pid, (root, *_) = lab
    base = git(root, "rev-parse", "--short=7", "HEAD").strip()
    (root / "old.tsv").write_text(f"commit\tval_bpb\tmemory_gb\tstatus\tdescription\n{base}\t1.0\t44.0\tkeep\tb\n")
    a = ctx(pid, root, "a")
    with pytest.raises(AssayError, match="--hw"):
        ops.import_results(a, root / "old.tsv")
    assert ops.import_results(a, root / "old.tsv", hw="RTX 4090")["imported"] == 1
    parts = a.lg.sql_internal("SELECT config->>'partition' AS p FROM assay.experiments WHERE project_id=%s", (pid,))
    assert parts[0]["p"].startswith("rtx-4090/") and parts[0]["p"] != a.partition
