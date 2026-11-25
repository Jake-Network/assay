"""benchbook exp run: check_tried, the command (through the core runner, with tee) and record_experiment in one
command. Exit codes and output pass through; every run that starts is recorded."""
from __future__ import annotations

import json
import os
import shlex
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from benchbook import Ledger, cli
from benchbook.exp_run import command_line, parse_output
from benchbook.hooks import subagent as hooks
from tests.conftest import make_project
from tests.test_dedup_mode import notify_project  # noqa: F401 - fixture

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")


def _script(tmp_path: Path, name: str, body: str) -> list[str]:
    """The argv after --: this interpreter running a script file (no quoting differs between shells)."""
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return [sys.executable, str(path)]


@pytest.fixture
def run(tmp_path: Path, db_url: str, project: str):  # type: ignore[no-untyped-def]
    env = {**os.environ, "BENCHBOOK_DATABASE_URL": db_url, "BENCHBOOK_PROJECT": project, "BENCHBOOK_AGENT": "w1"}

    def go(*args: str, project_id: str | None = None, **kw: Any) -> subprocess.CompletedProcess[str]:
        e = {**env, "BENCHBOOK_PROJECT": project_id} if project_id else env
        return subprocess.run([sys.executable, "-m", "benchbook.cli", "exp", "run", *args], capture_output=True,
                              text=True, cwd=tmp_path, env=e, timeout=120, **kw)
    return go


def _exps(lg: Ledger) -> list[dict[str, Any]]:
    return lg.sql_internal("SELECT * FROM benchbook.experiments WHERE project_id=%s ORDER BY created_at",
                           (lg.project_id,))


def _receipt_id(stdout: str) -> str:
    last = stdout.rstrip("\n").splitlines()[-1]
    assert last.startswith("benchbook: recorded e:"), stdout
    return last.split()[2]


# output parsing ------------------------------------------------------------------------------------
def test_metrics_come_from_the_formats_the_examples_print() -> None:
    # runner.parse_summary lines (autoresearch's summary)
    assert parse_output("step 1\nval_bpb: 0.99\npeak_vram_mb: 100\n").metrics == {"val_bpb": 0.99, "peak_vram_mb": 100}
    # examples/circle-packing/evaluator.py: a JSON object on the last line
    p = parse_output('working\n{"sum_radii": 2.5, "max_violation": 0.0}\n')
    assert p.metrics == {"sum_radii": 2.5, "max_violation": 0.0} and p.run_status is None
    p = parse_output('{"invalid": "constraint violated by 0.1", "run_status": "invalid"}')
    assert p.metrics == {} and p.run_status == "invalid" and p.note == "constraint violated by 0.1"
    # bench/tools/evaluate.py's receipt: "value" is the primary metric, the seed is not a metric
    receipt = {"eval_id": "x", "config": {"iters": 10}, "code_ref": "c", "seed": 3, "run_status": "ok",
               "value": 2.6, "notes": "SLSQP did not converge"}
    p = parse_output(json.dumps(receipt), primary="sum_radii")
    assert p.metrics == {"sum_radii": 2.6} and p.run_status == "ok" and p.note == "SLSQP did not converge"
    assert parse_output(json.dumps({"metrics": {"seed": 1, "a": True}, "b": 2})).metrics == {"seed": 1}
    assert parse_output('{"x": NaN, "y": Infinity, "z": 1}\nnot json').metrics == {}
    assert parse_output('{"x": NaN, "y": Infinity, "z": 1}').metrics == {"z": 1}


def test_command_line_keeps_one_argument_as_a_shell_line() -> None:
    assert command_line(["make eval && cat score"]) == "make eval && cat score"
    if sys.platform != "win32":
        assert shlex.split(command_line(["python", "my file.py", "--x=1"])) == ["python", "my file.py", "--x=1"]


# one command per attempt ---------------------------------------------------------------------------
def test_ok_run_passes_output_through_and_records_its_metrics(run, tmp_path: Path, lead: Ledger) -> None:  # type: ignore[no-untyped-def]
    body = ("import os, sys\nprint('cfg', os.environ['BENCHBOOK_EXP_CONFIG'], os.environ['BENCHBOOK_EXP_SEED'])\n"
            "sys.stderr.write('to stderr\\n')\nprint('score: 2.5')\nsys.stdout.write('no newline at the end')\n")
    argv = _script(tmp_path, "ok.py", body)
    exp_env = {**os.environ, "BENCHBOOK_EXP_CONFIG": '{"lr": 0.1}', "BENCHBOOK_EXP_SEED": "4"}
    plain = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=exp_env).stdout
    r = run("--config", '{"lr": 0.1}', "--seed", "4", "--code-ref", "v1", "--notes", "first try", "--", *argv)
    assert r.returncode == 0, r.stderr
    assert 'cfg {"lr": 0.1} 4\n' in plain and plain.endswith("no newline at the end")
    out, receipt = r.stdout.rsplit("\n", 2)[:2]
    assert out == plain  # unchanged; the line exp run adds starts on a line of its own
    assert receipt.startswith("benchbook: recorded e:") and "improved score=2.5  ★ new best (ok, " in receipt
    assert r.stdout.endswith("\n")
    rid = _receipt_id(r.stdout)
    [e] = _exps(lead)
    assert f"e:{str(e['id'])[:8]}" == rid and e["author"] == "w1"
    assert (e["config"], e["seed"], e["code_ref"], e["run_status"]) == ({"lr": 0.1}, 4, "v1", "ok")
    assert e["metrics"] == {"score": 2.5} and e["notes"] == "first try" and e["duration_ms"] >= 0
    assert e["trace_ref"]["exit_code"] == 0 and str(tmp_path / "ok.py") in e["trace_ref"]["cmd"]
    assert hooks._receipt(r.stdout.strip() + "\n" + r.stderr, "e", False) == rid  # the capture hook's view


def test_json_mode_puts_one_json_line_after_the_output(run, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    argv = _script(tmp_path, "eval.py", 'import json\nprint(json.dumps({"score": 1.5, "max_violation": 0.0}))\n')
    r = run("--config", '{"a": 1}', "--json", "--", *argv)
    assert r.returncode == 0, r.stderr
    lines = r.stdout.splitlines()
    assert json.loads(lines[0]) == {"score": 1.5, "max_violation": 0.0} and len(lines) == 2
    res = json.loads(lines[1])
    assert res["ok"] and res["ran"] and res["run_status"] == "ok" and res["exit_code"] == 0
    assert res["metrics"] == {"score": 1.5, "max_violation": 0.0} and res["outcome"] == "improved"
    assert hooks._receipt(r.stdout.strip(), "e", False) == res["id"]


def test_invalid_result_named_by_the_output_is_recorded_as_invalid(run, tmp_path: Path, lead: Ledger) -> None:  # type: ignore[no-untyped-def]
    line = '{"invalid": "expected 26 circles, got 25", "run_status": "invalid"}'
    argv = _script(tmp_path, "bad.py", f"print({line!r})\n")
    r = run("--config", '{"a": 2}', "--", *argv)
    assert r.returncode == 0, r.stderr  # the command's exit code
    assert "failed (invalid: expected 26 circles, got 25" in r.stdout.splitlines()[-1]
    [e] = _exps(lead)
    assert e["run_status"] == "invalid" and e["outcome"] == "failed" and "expected 26 circles" in e["notes"]


def test_refuse_mode_does_not_run_an_exact_repeat(run, tmp_path: Path, lead: Ledger) -> None:  # type: ignore[no-untyped-def]
    argv = _script(tmp_path, "count.py", "open('ran', 'a').write('x')\nprint('score: 1')\n")
    first = run("--config", '{"a": 3}', "--seed", "0", "--", *argv)
    assert first.returncode == 0, first.stderr
    fid = _receipt_id(first.stdout)
    again = run("--config", '{"a": 3}', "--seed", "0", "--", *argv)
    assert again.returncode == 2  # as exp record exits for a refused duplicate
    assert (tmp_path / "ran").read_text() == "x"  # not run again
    assert again.stdout.startswith(f"benchbook: not run: duplicate of {fid} by w1 (seed 0): ok improved 1.0")
    assert hooks._receipt(again.stdout.strip(), "e", False) is None
    js = run("--config", '{"a": 3}', "--seed", "0", "--json", "--", *argv)
    data = json.loads(js.stdout)
    assert js.returncode == 2 and data["ok"] is False and data["ran"] is False and data["duplicate_of"] == fid
    assert data["earlier"]["value"] == 1.0
    other = run("--config", '{"a": 3}', "--seed", "1", "--", *argv)  # another seed is a replicate: it runs
    assert other.returncode == 0 and (tmp_path / "ran").read_text() == "xx"
    assert fid in other.stderr and "other seeds" in other.stderr
    assert len(_exps(lead)) == 2


def test_failures_are_recorded_and_retried_only_on_request(run, tmp_path: Path, lead: Ledger) -> None:  # type: ignore[no-untyped-def]
    argv = _script(tmp_path, "crash.py", "import sys\nprint('loading')\nprint('Traceback: boom')\nsys.exit(3)\n")
    r = run("--config", '{"a": 4}', "--", *argv)
    assert r.returncode == 3  # passed through
    assert r.stdout.startswith("loading\nTraceback: boom\n") and "failed (error: exit 3" in r.stdout
    [e] = _exps(lead)
    assert e["run_status"] == "error" and e["trace_ref"]["exit_code"] == 3
    assert e["notes"].startswith("[exit 3] output tail:") and "Traceback: boom" in e["notes"]
    again = run("--config", '{"a": 4}', "--", *argv)
    assert again.returncode == 2 and "--retry-failed" in again.stdout and len(_exps(lead)) == 1
    retry = run("--config", '{"a": 4}', "--retry-failed", "--", *argv)
    assert retry.returncode == 3 and "it failed; running it again" in retry.stderr and len(_exps(lead)) == 2


def test_timeout_is_recorded_as_timeout(run, tmp_path: Path, lead: Ledger) -> None:  # type: ignore[no-untyped-def]
    argv = _script(tmp_path, "slow.py", "import time\nprint('started', flush=True)\ntime.sleep(60)\n")
    t0 = time.monotonic()
    r = run("--config", '{"a": 5}', "--timeout", "1", "--", *argv)
    assert r.returncode == 124 and time.monotonic() - t0 < 30
    assert r.stdout.startswith("started\n") and "failed (timeout: timeout after 1s" in r.stdout
    [e] = _exps(lead)
    assert e["run_status"] == "timeout" and e["trace_ref"]["exit_code"] is None and "started" in e["notes"]


@posix_only
@pytest.mark.parametrize(("sig", "code"), [(signal.SIGTERM, 143), (signal.SIGINT, 130)])
def test_interrupt_is_recorded_before_exiting(tmp_path: Path, db_url: str, project: str, lead: Ledger,
                                              sig: int, code: int) -> None:
    argv = _script(tmp_path, "long.py", "import time\nopen('started', 'w').write('1')\nprint('working', flush=True)\n"
                                        "time.sleep(60)\n")
    env = {**os.environ, "BENCHBOOK_DATABASE_URL": db_url, "BENCHBOOK_PROJECT": project, "BENCHBOOK_AGENT": "w1"}
    p = subprocess.Popen([sys.executable, "-m", "benchbook.cli", "exp", "run", "--config", '{"a": 6}', "--", *argv],
                         cwd=tmp_path, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    deadline = time.monotonic() + 60
    while not (tmp_path / "started").exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    time.sleep(0.3)
    p.send_signal(sig)
    out, err = p.communicate(timeout=60)
    assert p.returncode == code, err
    assert out.startswith("working\n") and "failed (error: interrupted (" in out
    [e] = _exps(lead)
    assert e["run_status"] == "error" and e["notes"].startswith(f"[interrupted ({signal.Signals(sig).name})]")


def test_a_required_metric_missing_from_the_output_is_an_error(run, tmp_path: Path, db_url: str) -> None:  # type: ignore[no-untyped-def]
    pid = make_project(db_url, metric="val_bpb", direction="min", pack="ml-research")
    argv = _script(tmp_path, "train.py", "print('loss: 1.0')\n")
    r = run("--config", '{"partition": "p"}', "--", *argv, project_id=pid)
    assert r.returncode == 0 and "failed (error: no val_bpb in the output" in r.stdout
    lg = Ledger.open(project=pid, agent="lead", database_url=db_url)
    [e] = _exps(lg)
    assert e["run_status"] == "error" and e["metrics"] == {"loss": 1.0}


def test_a_bad_config_never_runs(run, tmp_path: Path, lead: Ledger) -> None:  # type: ignore[no-untyped-def]
    argv = _script(tmp_path, "touch.py", "open('ran', 'w').write('x')\n")
    r = run("--config", "{not json", "--", *argv)
    assert r.returncode == 1 and "--config" in r.stderr
    assert run("--config", "[1]", "--", *argv).returncode == 1
    assert run("--config", "{}").returncode == 1  # no command
    assert not (tmp_path / "ran").exists() and _exps(lead) == []


def test_task_linkage_follows_the_one_lease(run, tmp_path: Path, lead: Ledger, db_url: str, project: str) -> None:  # type: ignore[no-untyped-def]
    [tid] = lead.create_tasks([{"title": "explore"}])
    w1 = Ledger.open(project=project, agent="w1", database_url=db_url)
    assert w1.next_task() is not None
    argv = _script(tmp_path, "ok.py", "print('score: 1')\n")
    assert run("--config", '{"a": 7}', "--", *argv).returncode == 0
    assert _exps(lead)[0]["task_id"] == tid


# notify mode ---------------------------------------------------------------------------------------
def _cli(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    with pytest.raises(SystemExit) as exit_info:
        cli.main(list(argv))
    out = capsys.readouterr()
    code = exit_info.value.code
    return (code if isinstance(code, int) else 0), out.out, out.err


def test_notify_mode_runs_a_repeat_and_names_the_earlier_run(db_url: str, notify_project: str, tmp_path: Path,  # noqa: F811
                                                             monkeypatch: pytest.MonkeyPatch,
                                                             capsys: pytest.CaptureFixture[str]) -> None:
    # In process: the temporary notify pack exists only here.
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BENCHBOOK_DATABASE_URL", db_url)
    for key in ("json", "project", "agent"):
        monkeypatch.setitem(cli.STATE, key, False if key == "json" else None)
    argv = _script(tmp_path, "check.py", "open('ran', 'a').write('x')\nprint('score: 2')\n")
    cmd = ("--project", notify_project, "exp", "run", "--config", '{"part": "p1", "x": 1}', "--seed", "0",
           "--code-ref", "a1b2c3d")
    code, out, err = _cli(capsys, "--agent", "implementer", *cmd, "--json", "--", *argv)
    first = json.loads(out.splitlines()[-1])
    assert code == 0 and first["duplicate_of"] is None
    code, out, err = _cli(capsys, "--agent", "verifier", *cmd, "--json", "--", *argv)
    again = json.loads(out.splitlines()[-1])
    assert code == 0 and (tmp_path / "ran").read_text() == "xx"  # it ran again
    assert again["id"] != first["id"] and again["duplicate_of"] == first["id"] and first["id"] in again["notice"]
    assert hooks._receipt(out.strip(), "e", False) == again["id"]
    monkeypatch.setitem(cli.STATE, "json", False)
    code, out, err = _cli(capsys, "--agent", "verifier", *cmd, "--", *argv)
    assert code == 0 and (tmp_path / "ran").read_text() == "xxx"
    assert f"same config, code_ref and seed as {first['id']}" in err and "dedup_mode notify" in err
    assert out.splitlines()[-1].endswith(f", repeat of {first['id']}")


# the capture hook ----------------------------------------------------------------------------------
def test_hook_reads_an_exp_run_bash_call_as_a_write() -> None:
    block = {"type": "tool_use", "id": "b", "name": "Bash",
             "input": {"command": "benchbook --agent worker-3 exp run --config '{\"a\": 1}' --seed 0 -- python e.py"}}
    assert hooks._write_tool(block) == ("e", "worker-3", False)
    text = "some output\nbenchbook: recorded e:1a2b3c4d improved score=1  ★ new best (ok, 0.1s)\nnote: x"
    assert hooks._receipt(text, "e", False) == "e:1a2b3c4d"
    assert hooks._receipt("benchbook: recorded e:1a2b3c4d no_change (ok, 0.1s)", "e", False) == "e:1a2b3c4d"
    assert hooks._receipt("benchbook: not run: duplicate of e:1a2b3c4d by w (seed 0): ok", "e", False) is None
    assert hooks._receipt("x\nbenchbook: not recorded: duplicate of e:1a2b3c4d", "e", False) is None
    assert hooks._receipt('{"a": 1}\n{"ok": true, "ran": true, "id": "e:1a2b3c4d"}', "e", False) == "e:1a2b3c4d"
    assert hooks._receipt("benchbook: recorded e:1a2b3c4d improved (ok, 0.1s)", "e", True) is None  # MCP: JSON only
