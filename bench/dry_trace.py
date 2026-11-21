"""What a real run leaves behind, written by the dry simulation: a stream-json event log and saved session
transcripts in Claude Code's shapes.

With these, bench.real's instrumentation (transcript collection, usage reconciliation, ledger export) and
bench.analyze_cost run on a dry run directory exactly as they run on a real one. The token counts are the chars/4
estimate of the scripted text on top of a made-up starting prompt: they check the pipeline and say nothing about cost.
As in a real run, a stream message carries the output count from the start of its response and only the transcript
has the final one, so the stream reconciles on the prompt classes and the transcripts on all four.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from benchbook.tokens import estimate_tokens

from .real import MODEL_USAGE_KEYS, TOKEN_KEYS

MCP = "mcp__plugin_benchbook_benchbook__benchbook_"
WORKER_TYPE = {"A": "general-purpose", "B": "general-purpose", "C": "benchbook:benchbook-worker"}
START_PROMPT = {"lead": 1200, "worker": 800}  # stands in for the system prompt and the tool definitions
STREAM_OUTPUT = 1  # a stream message reports the output tokens counted when its response started


@dataclass
class Agent:
    """One simulated agent. `prompt` is what its next model call will read."""
    tool_use_id: str | None  # the Agent call that started it; None for the lead
    agent_id: str | None
    description: str | None
    prompt: int
    read: int = 0  # the previous call's prompt, served from the cache on the next one
    transcript: Path | None = None


class Trace:
    def __init__(self, out: Path, mode: str) -> None:
        out.mkdir(parents=True, exist_ok=True)
        self.out, self.mode = out, mode
        self.session_id = str(uuid4())
        # A stand-in for ~/.claude, laid out the way Claude Code lays it out, so the real collection code finds it.
        self.config_dir = out / "claude-config"
        folder = self.config_dir / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(out / "work"))
        self.agents_dir = folder / self.session_id / "subagents"
        self.agents_dir.mkdir(parents=True)
        self.lead = Agent(None, None, None, START_PROMPT["lead"], transcript=folder / f"{self.session_id}.jsonl")
        self.stream = (out / "lead.stream.jsonl").open("w", encoding="utf-8")
        self.stamps = (out / "lead.timestamps").open("w", encoding="utf-8")
        self.totals = {"lead": [0, 0, 0, 0], "session": [0, 0, 0, 0]}
        self.turns = 0
        self._event({"type": "system", "subtype": "init", "cwd": str(out / "work"), "model": "dry",
                     "claude_code_version": "dry"})

    def _event(self, event: dict[str, Any]) -> None:
        self.stream.write(json.dumps({**event, "session_id": self.session_id, "uuid": str(uuid4())}) + "\n")
        self.stamps.write(f"{time.monotonic_ns()}\n")

    def _saved(self, agent: Agent, kind: str, message: dict[str, Any]) -> None:
        assert agent.transcript is not None
        row = {"type": kind, "uuid": str(uuid4()), "sessionId": self.session_id, "message": message,
               "isSidechain": agent.agent_id is not None, **({"agentId": agent.agent_id} if agent.agent_id else {})}
        with agent.transcript.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")

    def _tags(self, agent: Agent) -> dict[str, Any]:
        if agent.agent_id is None:
            return {"parent_tool_use_id": None}
        return {"parent_tool_use_id": agent.tool_use_id, "agent_id": agent.agent_id,
                "subagent_type": WORKER_TYPE[self.mode], "task_description": agent.description}

    def _respond(self, agent: Agent, blocks: list[dict[str, Any]]) -> None:
        """One model call: the agent reads its prompt and writes `blocks`, which its next call will read too."""
        written = sum(estimate_tokens(json.dumps(block)) for block in blocks)
        usage = {"input_tokens": 2, "cache_creation_input_tokens": agent.prompt - agent.read - 2,
                 "cache_read_input_tokens": agent.read, "output_tokens": written}
        message = {"id": f"msg_{uuid4().hex[:24]}", "type": "message", "role": "assistant", "model": "dry",
                   "stop_reason": None}
        for block in blocks:  # as in a real stream and transcript: one record per content block, same ID and usage
            started = {**usage, "output_tokens": STREAM_OUTPUT}
            self._event({"type": "assistant", **self._tags(agent),
                         "message": {**message, "content": [block], "usage": started}})
            self._saved(agent, "assistant", {**message, "content": [block], "stop_reason": "tool_use",
                                             "usage": usage})
        for scope in ("session", *(("lead",) if agent.agent_id is None else ())):
            self.totals[scope] = [n + usage[key] for n, key in zip(self.totals[scope], TOKEN_KEYS, strict=True)]
        self.turns += 1
        agent.read = agent.prompt - 2
        agent.prompt += written

    def _receive(self, agent: Agent, block: dict[str, Any]) -> None:
        message = {"role": "user", "content": [block]}
        self._event({"type": "user", **self._tags(agent), "message": message})
        self._saved(agent, "user", message)
        agent.prompt += estimate_tokens(json.dumps(block))

    def tool(self, agent: Agent, name: str, tool_input: dict[str, Any], result: Any) -> None:
        """A model call that makes one tool call, and the result coming back."""
        tool_use_id = f"toolu_{uuid4().hex[:24]}"
        tool_input = json.loads(json.dumps(tool_input, default=str))  # ledger IDs are UUIDs
        self._respond(agent, [{"type": "tool_use", "id": tool_use_id, "name": name, "input": tool_input}])
        text = result if isinstance(result, str) else json.dumps(result, default=str)
        self._receive(agent, {"type": "tool_result", "tool_use_id": tool_use_id, "content": text})

    def spawn(self, description: str, prompt: str) -> Agent:
        """The lead delegates: an Agent tool call whose result arrives with report()."""
        tool_use_id, agent_id = f"toolu_{uuid4().hex[:24]}", f"a{uuid4().hex[:16]}"
        call = {"description": description, "subagent_type": WORKER_TYPE[self.mode], "prompt": prompt}
        self._respond(self.lead, [{"type": "tool_use", "id": tool_use_id, "name": "Agent", "input": call}])
        path = self.agents_dir / f"agent-{agent_id}.jsonl"
        path.with_suffix(".meta.json").write_text(json.dumps(
            {"agentType": WORKER_TYPE[self.mode], "description": description, "toolUseId": tool_use_id}))
        worker = Agent(tool_use_id, agent_id, description, START_PROMPT["worker"] + estimate_tokens(prompt),
                       transcript=path)
        self._event({"type": "system", "subtype": "task_started", "task_id": agent_id, "tool_use_id": tool_use_id,
                     "description": description, "subagent_type": WORKER_TYPE[self.mode], "prompt": prompt})
        opening = {"role": "user", "content": [{"type": "text", "text": prompt}]}
        self._event({"type": "user", **self._tags(worker), "message": opening})
        self._saved(worker, "user", opening)
        return worker

    def report(self, worker: Agent, text: str, delivered: bool = True) -> None:
        """The subagent's closing message and, unless the simulation dropped it, its arrival in the lead's context."""
        self._respond(worker, [{"type": "text", "text": text}])
        handed = [{"type": "text", "text": text if delivered else ""}]
        self._receive(self.lead, {"type": "tool_result", "tool_use_id": worker.tool_use_id, "content": handed})

    def close(self, summary: str) -> None:
        """The lead's structured output and the final result event with both totals."""
        report = {"summary": summary, "evaluations": []}
        self.tool(self.lead, "StructuredOutput", report, "Structured output provided successfully")
        lead, session = self.totals["lead"], self.totals["session"]
        self._event({"type": "result", "subtype": "success", "is_error": False, "num_turns": self.turns,
                     "total_cost_usd": 0.0, "usage": dict(zip(TOKEN_KEYS, lead, strict=True)),
                     "modelUsage": {"dry": dict(zip(MODEL_USAGE_KEYS, session, strict=True))},
                     "structured_output": report})
        self.stream.close()
        self.stamps.close()
