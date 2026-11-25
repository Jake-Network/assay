"""The merge gate (merge_gate.py): a lead's git merge of a reported commit waits for its re-run's verdict."""
from __future__ import annotations

import hashlib
import io
import json
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from benchbook.hooks import checks as check_hooks
from benchbook.hooks import merge_gate, session_notes, spooler
from tests.conftest import CONFIG, git, make_repo, piece

SESSION = "lead-session"


@pytest.fixture
def setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    repo = make_repo(tmp_path / "repo", CONFIG)
    sha = piece(repo, "media", {"impl.txt": "fixed\n"})
    notes = session_notes.notes_dir(SESSION)
    assert notes is not None
    notes.mkdir(parents=True)
    key = hashlib.sha256(json.dumps(["p", "media", sha]).encode()).hexdigest()
    merge_gate.mark(notes, sha, "media", [f"verify-{key}.txt"], "general-purpose")
    return {"repo": repo, "sha": sha, "notes": notes, "verdict": notes / f"verify-{key}.txt"}


def call(setup: dict[str, Any], command: str, wait: float = 0.2, **extra: Any) -> dict[str, Any] | None:
    data = {"session_id": SESSION, "cwd": str(setup["repo"]), "tool_name": "Bash", "hook_event_name": "PreToolUse",
            "tool_input": {"command": command}, **extra}
    return merge_gate.decide(data, wait)


PASSED = "benchbook: media abc1234: re-run passed (2 checks, 1.0s), tests not weakened, checked: re-run (c:1)"
WEAK = ("benchbook: media abc1234: re-run passed (2 checks, 1.0s), tests weakened or check declarations changed; "
        "needs review, checked: none (c:1)")


@pytest.mark.parametrize(("command", "refs"), [
    ("git merge media", [(None, "media")]),
    ("git merge --no-ff -m 'Merge media' media && git branch -d media", [(None, "media")]),
    ("cd x; git -C repo merge -X theirs a b", [("repo", "a"), ("repo", "b")]),
    ("git merge -S media", [(None, "media")]),
    ("git merge -SDEADBEEF media", [(None, "media")]),
    ("git merge --gpg-sign media", [(None, "media")]),
    ("git merge --gpg-sign=DEADBEEF media", [(None, "media")]),
    ("git merge --abort", []),
    ("git log --merges", []),
    ("echo 'git merge media'", []),
])
def test_merge_targets(command: str, refs: list[tuple[str | None, str]]) -> None:
    assert merge_gate.merge_targets(command) == refs


def test_a_clean_verdict_lets_the_merge_go_ahead_and_hands_it_over(setup: dict[str, Any]) -> None:
    setup["verdict"].write_text(PASSED + "\n")
    answer = call(setup, "git merge --no-ff media")
    assert answer == {"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": PASSED}}
    assert not setup["verdict"].exists()  # taken: the next tool result does not repeat it
    assert session_notes.deliver(SESSION) == ""
    assert call(setup, "git merge media") is None  # nothing pending any more


def test_a_verdict_that_needs_review_holds_the_merge_once(setup: dict[str, Any]) -> None:
    setup["verdict"].write_text(WEAK + "\n")
    answer = call(setup, "git merge media")
    assert answer is not None
    out = answer["hookSpecificOutput"]
    assert out["permissionDecision"] == "deny" and out["permissionDecisionReason"].startswith(WEAK)
    assert "merge again to go ahead" in out["permissionDecisionReason"]
    assert call(setup, "git merge media") is None


FLAKY = ("benchbook: media abc1234: re-run failed (unit:tests/test_net.py::test_timeout:Timeout), then passed on a "
         "retry (2 checks, 1.0s), tests not weakened; attempts disagree, checked: none (c:5f3bebad; failed run "
         "e:73d83ab7)")
TWICE = ("benchbook: media abc1234: re-run failed, and again on a retry (2 checks, 1.0s), tests not weakened, "
         "checked: none (c:5f3bebad; runs e:11111111, e:22222222)")
OTHER = TWICE.replace("on a retry (", "on a retry with a different failure (")
FAILED = "benchbook: media abc1234: re-run failed (2 checks, 1.0s), tests not weakened, checked: none (c:5f3bebad)"


@pytest.mark.parametrize(("line", "wanted", "unwanted"), [
    # the third boundary pilot's case: a flaky failure outside the piece; the lead can settle it itself
    (FLAKY, ["benchbook --agent lead decide e:73d83ab7 invalidated --oracle human",
             "benchbook --agent lead check verify media {sha} --reported-by general-purpose",
             "benchbook decide c:5f3bebad verified|refuted"], []),
    (TWICE, ["failed twice the same way", "reports a new commit", "benchbook decide c:5f3bebad"], ["check verify"]),
    (OTHER, ["decide e:11111111 invalidated", "decide e:22222222 invalidated",
             "check verify media {sha} --reported-by general-purpose"], []),
    # a line from before the retry (a cached verdict): either way, with the commands
    (FAILED, ["reports a new commit", "invalidated --oracle human", "check verify media {sha}"], []),
    (WEAK, ["a re-run cannot verify it", "benchbook decide c:1"], ["check verify"]),
])
def test_a_held_merge_names_what_the_lead_can_run(setup: dict[str, Any], line: str, wanted: list[str],
                                                  unwanted: list[str]) -> None:
    setup["verdict"].write_text(line + "\n")
    answer = call(setup, "git merge media")
    assert answer is not None
    reason = answer["hookSpecificOutput"]["permissionDecisionReason"]
    assert reason.startswith(line) and "merge again to go ahead" in reason.lower()
    assert all(w.format(sha=setup["sha"]) in reason for w in wanted), reason
    assert not any(u in reason for u in unwanted), reason


def test_a_passing_but_disputed_verdict_holds_a_signed_merge(setup: dict[str, Any]) -> None:
    disputed = PASSED.replace("not weakened,", "not weakened; attempts disagree,").replace("checked: re-run",
                                                                                         "checked: none")
    setup["verdict"].write_text(disputed + "\n")
    answer = call(setup, "git merge -S media")
    assert answer is not None
    out = answer["hookSpecificOutput"]
    assert out["permissionDecision"] == "deny" and out["permissionDecisionReason"].startswith(disputed)
    assert "a person decides" in out["permissionDecisionReason"]


def test_a_new_report_displaces_an_older_environment_verdict(setup: dict[str, Any]) -> None:
    notes, sha = setup["notes"], setup["sha"]
    commit_verdict = setup["verdict"].name
    old_names = [commit_verdict, "verify-old-report.txt"]
    new_names = [commit_verdict, "verify-new-report.txt"]
    delivered = notes / session_notes.DELIVERED
    delivered.mkdir()
    stale = PASSED.replace("re-run passed", "environment failure")
    (delivered / old_names[1]).write_text(stale + "\n")
    merge_gate.mark(notes, sha, "media", old_names)
    merge_gate.mark(notes, sha, "media", new_names)

    answer = call(setup, "git merge media", wait=0)

    assert answer is not None
    reason = answer["hookSpecificOutput"]["permissionDecisionReason"]
    assert "has not reported" in reason and stale not in reason
    mark = merge_gate.pending_dir(notes) / f"{sha}.json"
    assert json.loads(mark.read_text())["verdicts"] == new_names


def test_a_report_replaced_while_waiting_uses_the_new_verdict(setup: dict[str, Any],
                                                             monkeypatch: pytest.MonkeyPatch) -> None:
    notes, sha = setup["notes"], setup["sha"]
    commit_verdict = setup["verdict"].name
    old_names = [commit_verdict, "verify-old-report.txt"]
    new_names = [commit_verdict, "verify-new-report.txt"]
    merge_gate.mark(notes, sha, "media", old_names)

    def deliver_both(_: float) -> None:
        with spooler._lock(notes, blocking=True):
            merge_gate.mark(notes, sha, "media", new_names)
            (notes / old_names[1]).write_text("old environment failure\n")
            (notes / new_names[1]).write_text("new environment failure\n")

    monkeypatch.setattr(merge_gate.time, "sleep", deliver_both)
    answer = call(setup, "git merge media", wait=10)

    assert answer is not None
    assert answer["hookSpecificOutput"]["permissionDecisionReason"].startswith("new environment failure")
    assert (notes / old_names[1]).exists()


def test_a_verdict_arriving_while_the_merge_waits(setup: dict[str, Any]) -> None:
    timer = threading.Timer(0.5, lambda: setup["verdict"].write_text(PASSED + "\n"))
    timer.start()
    started = time.monotonic()
    answer = call(setup, "git merge media", wait=10)
    assert answer is not None and answer["hookSpecificOutput"]["additionalContext"] == PASSED
    assert time.monotonic() - started < 5


def test_no_verdict_in_time_holds_once_then_lets_it_through(setup: dict[str, Any]) -> None:
    answer = call(setup, "git merge media", wait=0.3)
    assert answer is not None
    reason = answer["hookSpecificOutput"]["permissionDecisionReason"]
    assert answer["hookSpecificOutput"]["permissionDecision"] == "deny" and "has not reported" in reason
    assert f"benchbook --agent lead check verify media {setup['sha']} --reported-by general-purpose" in reason
    started = time.monotonic()
    assert call(setup, "git merge media", wait=5) is None and time.monotonic() - started < 1


def test_only_the_lead_and_only_pending_commits(setup: dict[str, Any]) -> None:
    assert call(setup, "git merge media", agent_id="worker-1") is None  # a subagent's merge
    git(setup["repo"], "branch", "other", "main")
    assert call(setup, "git merge other") is None
    assert call(setup, "git status") is None


def test_the_hook_entry_answers_in_json(setup: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
                                        capsys: pytest.CaptureFixture[str]) -> None:
    setup["verdict"].write_text(WEAK + "\n")
    data = {"session_id": SESSION, "cwd": str(setup["repo"]), "tool_name": "Bash",
            "tool_input": {"command": "git merge media"}}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(data)))
    assert check_hooks.main("bash-start") == 0
    out, err = capsys.readouterr()
    assert json.loads(out)["hookSpecificOutput"]["permissionDecision"] == "deny" and err == ""
