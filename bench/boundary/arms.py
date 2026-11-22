"""Claude Code configurations for the six preregistered arms."""
from __future__ import annotations

import json
import shlex
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TOOLS = "Bash,Read,Edit,Write,Glob,Grep"
DELEGATE = (
    "Delegate implementation pieces to general-purpose Agent workers. Give each worker a separate branch "
    "or worktree and ask for a text report ending in DONE <full commit SHA> if they complete it. "
    "Review those reports and decide what to integrate."
)
PROGRESS = (
    "In the first session, initialize claude-progress.txt and feature-list.json with the task's acceptance "
    "criteria. Give every feature a stable id, verification steps, and a passes field initially false. "
    "Start subsequent sessions by reading those files and git log. Complete one feature at a time, "
    "run its checks before flipping passes to true, and leave the feature definitions unchanged. "
    "End each session with a progress entry and a descriptive git commit."
)
SEARCH = shlex.join([sys.executable, str(Path(__file__).with_name("search_transcript.py"))])


@dataclass(frozen=True)
class Arm:
    label: str
    instructions: str
    fresh_sessions: bool = True


ARMS = {
    "A": Arm("Single session", "", fresh_sessions=False),
    "B": Arm("Text subagents", DELEGATE),
    "C": Arm("Progress files", f"{DELEGATE}\n{PROGRESS}"),
    "D": Arm("claude-mem", DELEGATE),
    "E": Arm("Transcript search", f"{DELEGATE}\nSearch prior raw session events with `{SEARCH} QUERY`."),
    "F": Arm("benchbook boundary mode", DELEGATE),
}


def command(
    arm: str, prompt: str, session: str, *, resume: bool, model: str, budget: float, max_turns: int,
    claude_mem: Path | None, schema: dict | None = None,
) -> list[str]:
    """The probe uses read-only tools for every arm so asking a question can't modify the checkout."""
    tools = (TOOLS if schema is None else "Bash,Read,Glob,Grep") + (",Agent" if schema is None and arm != "A" else "")
    allowed = tools
    if schema is not None:
        allowed = "Read,Glob,Grep,Bash(git log *),Bash(git status *),Bash(git diff *)"
        if arm == "E":
            allowed += f",Bash({SEARCH} *)"
    cmd = [
        "claude", "-p", prompt, "--output-format", "stream-json", "--verbose", "--forward-subagent-text",
        "--resume" if resume else "--session-id", session,
        "--setting-sources", "", "--model", model, "--max-turns", str(max_turns),
        "--max-budget-usd", str(budget), "--tools", tools, "--allowedTools", allowed,
    ]
    if arm == "D":
        if claude_mem is None:
            raise ValueError("arm D needs the pinned claude-mem plugin directory")
        cmd.extend(["--plugin-dir", str(claude_mem)])
        # The pinned plugin's own memory-search tools must remain available.
        cmd[cmd.index("--allowedTools") + 1] += ",mcp__plugin_claude-mem_mcp-search__*"
    else:
        cmd.append("--strict-mcp-config")
    if arm == "F":
        cmd.extend(["--plugin-dir", str(ROOT / "integrations" / "claude-code")])
    if schema is not None:
        cmd.extend(["--json-schema", json.dumps(schema)])
    return cmd
