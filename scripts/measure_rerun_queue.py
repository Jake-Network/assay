"""How long the automatic re-run queue takes when several pieces report done at once (PR #4 issue 4, B2 step 1).

Model-free. A synthetic Python repository (90 tests in 9 files over a 30-module package, one JUnit check, tests/**
guarded) gets N pieces, each changing one module and adding one test file, so every verify also runs the check at
the merge-base (checks.count_tests). N verify requests are queued at once the way the agent-result hook queues them,
and the spool processor's loop (hooks.replay) drains them in this process, so the check runs can be timed: the
candidate commit's against the merge-base's. A few seconds in, a worker runs `benchbook check run unit` in its own
worktree; its attempt waits in the same spool, and the time until it is recorded is the delay issue 4 asks about.
Separately, the check command is run twice in one worktree: the difference is what a fresh worktree's missing
__pycache__ costs.

    uv run python scripts/measure_rerun_queue.py [--pieces 8] [--runs 2] [--json out.json]

Needs git and the embedded PostgreSQL that the tests use (pixeltable_pgserver). Writes only under a temporary folder.
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any

MODULES, TEST_FILES, TESTS_PER_FILE = 30, 9, 10


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


def module(i: int, extra: str = "") -> str:
    body = [f"def f{i}_{k}(x):\n    total = 0\n    for n in range(x):\n        total += (n * {k}) % 7\n"
            f"    return total + {i}\n" for k in range(60)]
    return "\n\n".join(body) + extra


def make_repo(root: Path, project: str, parallel: bool = False) -> str:
    py = shlex.quote(sys.executable)
    cfg = (f'project = "{project}"\n\n[checks.unit]\n'
           f'cmd = "{py} -m pytest -q -p no:cacheprovider tests --junitxml={{junit}}"\njunit = true\ntimeout = 300\n\n'
           '[guard]\npaths = ["tests/**", "**/conftest.py", ".benchbook/config.toml"]\nbase = "main"\n'
           + ("\n[verify]\nparallel_baseline = true\n" if parallel else ""))
    files = {".benchbook/config.toml": cfg,
             "pkg/__init__.py": "import asyncio, decimal, email.mime.text, json, xml.etree.ElementTree  # noqa\n"
                                + "".join(f"from . import mod{i}  # noqa\n" for i in range(MODULES))}
    files.update({f"pkg/mod{i}.py": module(i) for i in range(MODULES)})
    for t in range(TEST_FILES):
        tests = []
        for k in range(TESTS_PER_FILE):
            m = (t * TESTS_PER_FILE + k) % MODULES
            tests.append(f"\ndef test_{t}_{k}():\n    time.sleep(SLEEP)\n    assert pkg.mod{m}.f{m}_{k}(50) >= 0\n")
        head = ("import os\nimport time\n\nimport pkg\n\n"
                "SLEEP = float(os.environ.get('MEASURE_TEST_MS', '0')) / 1000\n\n")
        files[f"tests/test_part{t}.py"] = head + "".join(tests)
    root.mkdir(parents=True)
    git(root, "init", "-q", "-b", "main")
    for k, v in (("user.email", "m@example.com"), ("user.name", "m"), ("commit.gpgsign", "false")):
        git(root, "config", k, v)
    for path, text in files.items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text(text)
    git(root, "add", "-A")
    git(root, "commit", "-qm", "base")
    return git(root, "rev-parse", "HEAD")


def make_piece(repo: Path, i: int) -> str:
    name = f"piece-{i}"
    git(repo, "checkout", "-q", "-b", name, "main")
    mod = repo / "pkg" / f"mod{i % MODULES}.py"
    mod.write_text(module(i % MODULES, f"\n\ndef added_{i}():\n    return {i}\n"))
    m = i % MODULES
    (repo / "tests" / f"test_piece{i}.py").write_text(
        f"from pkg import mod{m}\n\n\ndef test_piece_{i}():\n    assert mod{m}.added_{i}() == {i}\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", name)
    sha = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "-q", "main")
    return sha


def pycache_share(repo: Path, rounds: int = 3) -> dict[str, float]:
    """The check command twice in one fresh worktree: the first run compiles the package, the second reuses it."""
    from benchbook import config

    spec = config.load(repo).checks["unit"]
    cold, warm = [], []
    for r in range(rounds):
        wt = repo.parent / f"pycache-{r}"
        git(repo, "worktree", "add", "--detach", "-q", str(wt), "main")
        for bucket in (cold, warm):
            cmd = spec.cmd.replace("{junit}", shlex.quote(str(wt.parent / f"pycache-{r}.xml")))
            t = time.perf_counter()
            subprocess.run(cmd, shell=True, cwd=wt, check=True, capture_output=True)
            bucket.append(time.perf_counter() - t)
        git(repo, "worktree", "remove", "--force", str(wt))
    return {"cold_s": statistics.median(cold), "warm_s": statistics.median(warm)}


def one_run(db_url: str, work: Path, pieces: int, worker_delay: float, parallel: bool) -> dict[str, Any]:
    from benchbook import Ledger, checks, hooks, verify_requests
    from benchbook.packs import load_pack

    project = f"m-{uuid.uuid4().hex[:10]}"
    d = load_pack("code").project_defaults
    Ledger.init_project(project, goal="Measure the re-run queue.", metric=d["metric"], direction=d["direction"],
                        database_url=db_url, pack="code", settings=dict(d["settings"]))
    repo = work / project / "repo"
    base = make_repo(repo, project, parallel)
    shas = {f"piece-{i}": make_piece(repo, i) for i in range(1, pieces + 1)}
    worker_wt = work / project / "worker"
    git(repo, "worktree", "add", "-q", str(worker_wt), f"piece-{pieces}")

    runs: list[dict[str, Any]] = []
    verifies: list[dict[str, Any]] = []
    recorded: list[float] = []
    real_run_checks, real_process = checks.run_checks, verify_requests.process

    def timed_run_checks(repo_: Path, commit: str, *a: Any, **k: Any) -> Any:
        t = time.monotonic()
        out = real_run_checks(repo_, commit, *a, **k)
        runs.append({"kind": "merge-base" if commit == base else "candidate", "s": time.monotonic() - t})
        return out

    def timed_process(data: dict[str, Any], *a: Any, **k: Any) -> str:
        t = time.monotonic()
        out = real_process(data, *a, **k)
        verifies.append({"piece": data["piece"], "start": t, "end": time.monotonic(), "result": out})
        return out

    checks.run_checks, verify_requests.process = timed_run_checks, timed_process
    real_start, hooks.start_processor = hooks.start_processor, lambda *args: None
    try:
        session = f"m-{uuid.uuid4().hex[:8]}"
        for name, sha in shas.items():
            verify_requests.enqueue({
                "session_id": session, "cwd": str(repo), "hook_event_name": "PostToolUse", "tool_name": "Agent",
                "tool_input": {"subagent_type": "general-purpose"},
                "tool_response": {"status": "completed", "content": [{"type": "text",
                                                                       "text": f"piece: {name}\nDONE {sha}"}]}})
        spool = hooks._spool_dir()
        t0 = time.monotonic()
        spooled: dict[str, float] = {}

        def worker() -> None:
            time.sleep(worker_delay)
            before = set(spool.glob("*.json"))
            env = {**os.environ, "BENCHBOOK_AGENT": f"worker-{pieces}"}
            p = subprocess.run([str(Path(sys.executable).with_name("benchbook")), "check", "run", "unit"],
                               cwd=worker_wt, env=env, capture_output=True, text=True)
            spooled["at"] = time.monotonic()
            spooled["exit"] = p.returncode
            mine = set(spool.glob("*.json")) - before
            while any(path.exists() for path in mine):  # recorded when the processor removes its entry
                time.sleep(0.05)
            recorded.append(time.monotonic())

        th = threading.Thread(target=worker)
        th.start()
        # The re-runs' processor, in this process so the checks can be timed: with lanes (B2 step 2b) only the verify
        # lane, and the worker's check run starts the records lane's own detached processor; without them the one
        # processor picks the attempt up on its rescan.
        lanes = getattr(hooks, "VERIFY", None)
        hooks.replay(spool, lanes) if lanes else hooks.replay(spool)  # type: ignore[call-arg]
        t_end = time.monotonic()
        if lanes is None:
            th.join(timeout=0.5)
            if th.is_alive():  # the attempt landed after the last rescan: the next processor start takes it
                hooks.replay(spool)
        th.join()
    finally:
        checks.run_checks, verify_requests.process = real_run_checks, real_process
        hooks.start_processor = real_start
    cand = [r["s"] for r in runs if r["kind"] == "candidate"]
    mb = [r["s"] for r in runs if r["kind"] == "merge-base"]
    per = [v["end"] - v["start"] for v in verifies]
    return {
        "pieces": pieces, "verify_results": sorted({v["result"] for v in verifies}),
        "verify_s_median": statistics.median(per), "candidate_s_median": statistics.median(cand),
        "merge_base_s_median": statistics.median(mb) if mb else 0.0, "merge_base_runs": len(mb),
        "merge_base_share": sum(mb) / sum(per) if per else 0.0,
        "first_verdict_s": min(v["end"] for v in verifies) - t0, "last_verdict_s": max(v["end"] for v in verifies) - t0,
        "attempt_spooled_s": spooled.get("at", t0) - t0, "check_run_exit": spooled.get("exit"),
        "attempt_delay_s": (recorded[0] - spooled["at"]) if recorded and "at" in spooled else None,
        "all_s": t_end - t0, "pycache": pycache_share(repo),
    }


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--pieces", type=int, default=8)
    p.add_argument("--runs", type=int, default=2)
    p.add_argument("--worker-delay", type=float, default=3.0, help="seconds before the worker's check run starts")
    p.add_argument("--test-ms", type=float, default=0.0, help="time each of the 90 base tests spends running")
    p.add_argument("--parallel-baseline", action="store_true", help="declare [verify] parallel_baseline = true")
    p.add_argument("--json", type=Path)
    args = p.parse_args()
    with tempfile.TemporaryDirectory(prefix="benchbook-rerun-queue-") as tmp:
        work = Path(tmp)
        os.environ["XDG_STATE_HOME"] = str(work / "state")
        os.environ["XDG_DATA_HOME"] = str(work / "data")
        os.environ["MEASURE_TEST_MS"] = str(args.test_ms)  # check verify runs the checks with this environment
        for k in [k for k in os.environ if k.startswith(("BENCHBOOK_", "ASSAY_"))]:
            del os.environ[k]
        from pixeltable_pgserver import get_server

        from benchbook.db import conn as dbconn

        pgdata = work / "pg"
        server = get_server(pgdata, cleanup_mode=None, postgres_version=dbconn.EMBEDDED_PG_VERSION)
        try:
            db_url = dbconn.ensure_database(server.get_uri(), "benchbook_measure")
            os.environ["BENCHBOOK_DATABASE_URL"] = db_url
            results = [one_run(db_url, work, args.pieces, args.worker_delay, args.parallel_baseline)
                       for _ in range(args.runs)]
        finally:
            subprocess.run([str(server.bin_path / "pg_ctl"), "-D", str(pgdata), "-m", "immediate", "stop"],
                           capture_output=True, check=False)
    for r in results:
        r["test_ms"], r["parallel_baseline"] = args.test_ms, args.parallel_baseline
    print(json.dumps(results, indent=2))
    if args.json:
        args.json.write_text(json.dumps(results, indent=2) + "\n")


if __name__ == "__main__":
    main()
