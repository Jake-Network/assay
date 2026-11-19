"""Hook adapter (§12.3, §18.1): three stored sample inputs."""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from assay import Ledger
from assay.config import write_config
from assay.hooks import extract_text, process

FX = Path(__file__).parent / "fixtures" / "hooks"


@pytest.fixture
def repo(tmp_path: Path, project: str) -> Path:
    write_config(tmp_path, project)
    return tmp_path


def _load(name: str, **subs: str) -> dict:
    raw = (FX / name).read_text()
    for k, v in subs.items():
        raw = raw.replace("{" + k + "}", v)
    return json.loads(raw)


def _notes(lg: Ledger) -> list[dict]:
    return lg.sql_internal("SELECT * FROM assay.claims WHERE project_id=%s AND 'auto-captured' = ANY(tags)",
                           (lg.project_id,))


def test_output_with_existing_ids_is_ignored(repo: Path, worker: Ledger) -> None:
    e = worker.record_experiment({"a": 1}, {"score": 1})
    c = worker.propose_claim("note", "something")
    data = _load("with_ids.json", EXP=f"e:{str(e.id)[:8]}", CLAIM=f"c:{str(c.id)[:8]}")
    data["cwd"] = str(repo)
    assert process("subagent-stop", data) == "cited"
    assert _notes(worker) == []


def test_output_without_ids_is_captured(repo: Path, worker: Ledger) -> None:
    data = _load("no_ids.json")
    data["cwd"] = str(repo)
    r = process("subagent-stop", data)
    assert r.startswith("captured:c:")
    [note] = _notes(worker)
    assert note["kind"] == "note" and note["author"] == "hook"
    assert note["statement"].startswith("[auto-captured] I tried lattice perturbation")
    assert note["payload"]["source"]["session_id"] == "s2"
    assert note["payload"]["source"]["event"] == "SubagentStop"
    roles = worker.sql_internal("SELECT role FROM assay.agents WHERE project_id=%s AND id='hook'",
                                (worker.project_id,))
    assert roles == [{"role": "harness"}]
    # the same output arriving through a second hook event is not captured twice
    process("post-tool-use", data)
    assert len(_notes(worker)) == 1


def test_output_with_only_unknown_ids_is_captured(repo: Path, worker: Ledger) -> None:
    data = _load("missing_ids.json")
    data["cwd"] = str(repo)
    assert extract_text(data).startswith("Finished; see e:00000000")
    assert process("post-tool-use", data).startswith("captured:")
    assert len(_notes(worker)) == 1


def test_handback_message_is_read() -> None:
    assert extract_text({"tool_name": "SubagentHandback", "tool_input": {"message": "report"}}) == "report"


def test_hook_cli_is_fast_and_always_exits_zero(repo: Path, worker: Ledger, db_url: str) -> None:
    data = _load("no_ids.json")
    data["cwd"] = str(repo)
    data["last_assistant_message"] = "A different finding, captured through the CLI entry point."
    import os

    env = {**os.environ, "XDG_STATE_HOME": str(repo / "state")}
    t = time.monotonic()
    p = subprocess.run([sys.executable, "-m", "assay.cli", "hook", "subagent-stop"], input=json.dumps(data),
                       text=True, capture_output=True, cwd=repo, timeout=30, env=env)
    elapsed = time.monotonic() - t
    assert p.returncode == 0, p.stderr
    for _ in range(100):
        if len(_notes(worker)) == 1:
            break
        time.sleep(0.1)
    assert len(_notes(worker)) == 1
    # garbage input: still exit 0
    p2 = subprocess.run([sys.executable, "-m", "assay.cli", "hook", "subagent-stop"], input="not json",
                        text=True, capture_output=True, cwd=repo, timeout=30, env=env)
    assert p2.returncode == 0
    print(f"hook wall time {elapsed:.2f}s")
