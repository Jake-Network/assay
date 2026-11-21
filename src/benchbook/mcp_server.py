"""MCP server: a thin adapter over the SDK (P10). Tools are prefixed benchbook_ and their descriptions
start with when to call them (§7.3). Every tool takes `agent`, the caller's self-reported identity:
unknown names are registered as workers."""
from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any, TypeVar

import anyio
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from .errors import BenchbookError, ValidationFailed
from .ids import short
from .ledger import Ledger, jsonable
from .models import ExperimentSummary

T = TypeVar("T")
TOOLSETS = ("worker", "orchestrator", "all")

INSTRUCTIONS = """benchbook is the shared lab notebook for this project. Results live in the ledger, not in messages.
Workers: benchbook_next_task, then benchbook_brief. Before every evaluation call benchbook_check_tried; right after it \
call
benchbook_record_experiment (failures too). Propose generalisable findings with benchbook_propose_claim and \
evidence ids.
Finish with benchbook_complete_task and reply to the lead in at most 3 sentences plus record ids.
If no project has been initialized, run benchbook init in the project directory before calling a ledger tool.
Only the orchestrator decides (benchbook_decide). verified needs a non-LLM oracle and evidence."""

SCHEMA_DOC = {
    "v_claim_status": "claim id, kind, statement, status (latest decision or proposed), last_oracle, n_support, "
                      "n_refute, n_support_authors, evidence_invalidated",
    "v_best": "the best valid ok experiment (direction-aware): id, author, config, metrics, primary_value",
    "v_leaderboard": "top 50 valid experiments with rank",
    "v_tried": "per dedup_key: attempts, seeds, ok_runs, mean_value, std_value, best_value, n_improved/no_change/"
               "worse/failed/unscored, summary_outcome, config, code_ref, last_id, last_notes",
    "v_negative": "dedup_keys that never improved: config, attempts, n_no_change, n_worse, n_failed, notes",
    "v_open_tasks": "claimable tasks: open or lease expired, attempts left, dependencies done",
    "v_progress": "best-so-far curve: id, created_at, primary_value, best_so_far, n, tokens_cum",
    "v_agent_stats": "per agent: role, experiments, improved, claims, dedup_avoided, dedup_rejected, last_active_at",
    "v_dedup_savings": "rejected (blocked duplicate records) and avoided (benchbook_check_tried matches)",
    "v_activity": "recent records of every kind: kind, id, agent, summary, created_at",
}


class Server:
    def __init__(self, project: str | None = None, toolset: str = "all") -> None:
        if toolset not in TOOLSETS:
            raise BenchbookError(f"toolset must be one of {TOOLSETS}")
        self._project = project
        self._base: Ledger | None = None
        self._agents: dict[str, Ledger] = {}
        self._lock = threading.Lock()

    def lg(self, agent: str) -> Ledger:
        agent = (agent or "").strip()
        if not agent:
            raise BenchbookError("agent is required: pass your benchbook agent name, e.g. worker-3")
        with self._lock:
            if self._base is None:
                # Claude Code discovers tools before the lead skill has a chance to run benchbook init.
                self._base = Ledger.open(project=self._project, agent="lead")
            if agent == "lead":
                return self._base
            if agent not in self._agents:
                self._agents[agent] = self._base.as_agent(agent)
            return self._agents[agent]


async def _run(fn: Callable[[], T]) -> T:
    try:
        return await anyio.to_thread.run_sync(fn)
    except ValidationFailed as e:
        raise ToolError(str(e) + "\nFix the listed fields and call again.") from None
    except BenchbookError as e:
        raise ToolError(str(e)) from None
    except Exception as e:  # noqa: BLE001
        # Not a ledger rule: the database is down, unreachable or migrated by another benchbook version.
        raise ToolError(f"benchbook internal error ({type(e).__name__}: {e}). This is not a problem with your input. "
                        "Ask the user to run `benchbook doctor` in the project directory; if the database was "
                        "stopped, restart the MCP server (it reconnects on start).") from None


def build(project: str | None = None, toolset: str = "all") -> MCPServer:
    srv = Server(project, toolset)
    mcp: MCPServer = MCPServer(name="benchbook", instructions=INSTRUCTIONS)
    orchestrator = toolset in ("orchestrator", "all")

    # ---------------------------------------------------------------- worker set
    @mcp.tool()
    async def benchbook_brief(agent: str, task_id: str | None = None, budget_tokens: int = 2000) -> str:
        """Call right after taking a task, and again after a long step: returns what you need to know now
        (goal, current best, verified claims, what was already tried, open questions) as markdown with ids."""
        return await _run(lambda: srv.lg(agent).brief(task_id, budget_tokens))

    @mcp.tool()
    async def benchbook_next_task(agent: str, tags: list[str] | None = None,
                              lease_seconds: int | None = None) -> dict[str, Any]:
        """Call when you need work: atomically claims the next open task. Omit lease_seconds to use the
        project's default. Returns {task: null} if none."""
        t = await _run(lambda: srv.lg(agent).next_task(tags, lease_seconds))
        if t is None:
            return {"task": None}
        return {"task": {"id": t.short_id, "title": t.title, "tags": t.tags, "spec": t.spec,
                         "lease_expires_at": t.lease_expires_at, "attempt": t.attempts}}

    @mcp.tool()
    async def benchbook_heartbeat(agent: str, task_id: str) -> dict[str, Any]:
        """Call every 5 minutes while working on a task to keep your lease. 'lease lost' means stop working on
        the task; record results without task_id."""
        exp = await _run(lambda: srv.lg(agent).heartbeat(task_id))
        return {"lease_expires_at": exp.isoformat()}

    @mcp.tool()
    async def benchbook_check_tried(agent: str, config: dict[str, Any], code_ref: str | None = None) -> dict[str, Any]:
        """Call BEFORE every evaluation: says whether this exact config (and code_ref) was already run, how many
        seeds replicate it and its best valid value. If tried, choose a different config or see the note about
        invalidated runs whose seeds are still reserved."""
        t = await _run(lambda: srv.lg(agent).check_tried(config, code_ref))

        def summary(e: ExperimentSummary) -> dict[str, Any]:
            return {"id": short("experiment", e.id), "author": e.author, "seed": e.seed, "outcome": e.outcome,
                    "value": e.primary_value, "notes": e.notes}

        return {"tried": bool(t.exact or t.invalidated), "replicates": t.replicates, "best_value": t.best_value,
                "exact": [summary(e) for e in t.exact[:5]], "invalidated": [summary(e) for e in t.invalidated[:5]],
                "note": t.note}

    @mcp.tool()
    async def benchbook_record_experiment(agent: str, config: dict[str, Any], metrics: dict[str, Any],
                                      run_status: str = "ok", seed: int | None = None, code_ref: str | None = None,
                                      notes: str | None = None, artifacts: list[dict[str, Any]] | None = None,
                                      task_id: str | None = None,
                                      trace_ref: dict[str, Any] | None = None) -> dict[str, Any]:
        """Call RIGHT AFTER every evaluation, including failures, errors and timeouts (run_status error|timeout|
        invalid, with the cause in notes). The server computes the outcome. Returns id, outcome, is_new_best,
        duplicate_of (the earlier identical run; id is null when the record was refused, set with a notice when
        the project records repeats)."""
        r = await _run(lambda: srv.lg(agent).record_experiment(
            config, metrics, run_status=run_status, seed=seed, code_ref=code_ref, notes=notes, artifacts=artifacts,
            task_id=task_id, trace_ref=trace_ref))
        out = {"id": short("experiment", r.id) if r.id else None, "outcome": r.outcome,
               "is_new_best": r.is_new_best,
               "duplicate_of": short("experiment", r.duplicate_of) if r.duplicate_of else None}
        if r.notice is not None:
            out["notice"] = r.notice
        return out

    @mcp.tool()
    async def benchbook_propose_claim(agent: str, kind: str, statement: str, payload: dict[str, Any] | None = None,
                                  tags: list[str] | None = None, task_id: str | None = None,
                                  evidence: list[list[str]] | None = None) -> dict[str, Any]:
        """Call when you have a generalisable observation or hypothesis. kind: hypothesis|observation|conclusion|
        note. statement <= 500 chars. evidence: [[record_id, "supports"|"refutes"], ...]."""
        ev = [(e[0], e[1]) for e in (evidence or []) if len(e) == 2]
        if evidence and len(ev) != len(evidence):
            raise ToolError("evidence items must be [record_id, relation] pairs")
        r = await _run(lambda: srv.lg(agent).propose_claim(kind, statement, payload, tags, task_id, ev))
        return {"id": short("claim", r.id), "duplicate_of": short("claim", r.duplicate_of) if r.duplicate_of else None}

    @mcp.tool()
    async def benchbook_link(agent: str, src: str, relation: str, dst: str, note: str | None = None) -> dict[str, Any]:
        """Call when an existing record bears on another: src supports|refutes|derived_from|depends_on|supersedes|
        replicates|cites dst."""
        lid = await _run(lambda: srv.lg(agent).link(src, relation, dst, note))
        return {"id": short("link", lid)}

    @mcp.tool()
    async def benchbook_complete_task(agent: str, task_id: str, status: str, summary: str) -> dict[str, Any]:
        """Call when you finish a task. status: done|failed. summary: at most 3 sentences; details are in the
        records."""
        t = await _run(lambda: srv.lg(agent).complete_task(task_id, status, summary))
        return {"ok": True, "task": t.short_id, "status": t.status}

    @mcp.tool()
    async def benchbook_get(agent: str, id: str) -> dict[str, Any]:
        """Call only when you need the details behind an id from a brief: the full record plus its links."""
        return await _run(lambda: srv.lg(agent).get(id))

    # ---------------------------------------------------------------- orchestrator set
    if orchestrator:
        @mcp.tool()
        async def benchbook_create_tasks(agent: str, tasks: list[dict[str, Any]]) -> dict[str, Any]:
            """Call to split the search space into independent tasks. Each: {title, spec: {config_space: ...},
            tags, priority, depends_on}. Orchestrator only."""
            ids = await _run(lambda: srv.lg(agent).create_tasks(tasks))
            return {"ids": [short("task", i) for i in ids]}

        @mcp.tool()
        async def benchbook_decide(agent: str, target_id: str, new_status: str, oracle: str, rationale: str,
                               evidence_ids: list[str] | None = None,
                               oracle_detail: dict[str, Any] | None = None) -> dict[str, Any]:
            """Call when evidence settles a claim (supported|refuted|inconclusive|verified|superseded|retracted) or
            an experiment must be invalidated. verified needs oracle metric|rerun|pytest|lean|human and evidence.
            For rerun, cite the original experiment in the supported decision or link it as support. Verification
            requires valid runs of the same config and code across >=3 seeds, with a rerun by another agent.
            Orchestrator only."""
            d = await _run(lambda: srv.lg(agent).decide(target_id, new_status, oracle, rationale,
                                                         evidence_ids or [], oracle_detail))
            return {"id": short("decision", d.id), "target": short(d.target_type, d.target_id),
                    "status": d.new_status}

        @mcp.tool()
        async def benchbook_cancel_task(agent: str, task_id: str, reason: str) -> dict[str, Any]:
            """Call to withdraw an open or claimed task that is no longer useful. Orchestrator only."""
            t = await _run(lambda: srv.lg(agent).cancel_task(task_id, reason))
            return {"ok": True, "task": t.short_id, "status": t.status}

        @mcp.tool()
        async def benchbook_status(agent: str) -> dict[str, Any]:
            """Call at the start of each round: project numbers (best, experiment counts, duplicates avoided,
            claim status counts, tasks, active agents)."""
            return await _run(lambda: srv.lg(agent).status())

        @mcp.tool()
        async def benchbook_sql(agent: str, query: str) -> dict[str, Any]:
            """Call for aggregate questions the brief does not answer. One read-only SELECT over the v_* views
            (read resource benchbook://schema first). Already scoped to this project. Max 200 rows, 2s."""
            return await _run(lambda: srv.lg(agent).sql(query))

        @mcp.tool()
        async def benchbook_search(agent: str, text: str | None = None, kind: str | None = None,
                               status: str | None = None, tags: list[str] | None = None,
                               limit: int = 20, all_tags: list[str] | None = None) -> list[dict[str, Any]]:
            """Call to find records by text, kind (claim|experiment|task|hypothesis|...), status or tags.
            tags matches records with any of them; all_tags only records with every one of them.
            Returns one line per record with its id; the line ends with the record's tags."""
            return await _run(lambda: srv.lg(agent).search(text, kind, status, tags, limit, all_tags=all_tags))

    @mcp.resource("benchbook://schema", name="schema", mime_type="text/markdown",
                  description="The v_* views benchbook_sql can read, with their columns. Read before benchbook_sql.")
    async def schema_resource() -> str:
        def build_doc() -> str:
            rows = srv.lg("lead").sql_internal(
                "SELECT table_name, string_agg(column_name, ', ' ORDER BY ordinal_position) AS cols"
                " FROM information_schema.columns WHERE table_schema='benchbook_q' GROUP BY table_name ORDER BY 1")
            out = ["# benchbook views (benchbook_sql)", "Each view is already filtered to the current project.", ""]
            for r in rows:
                out += [f"## {r['table_name']}", SCHEMA_DOC.get(r["table_name"], ""), f"columns: {r['cols']}", ""]
            return "\n".join(out)

        return await anyio.to_thread.run_sync(build_doc)

    @mcp.resource("benchbook://brief", name="brief", mime_type="text/markdown",
                  description="The project's default brief (no task).")
    async def brief_resource() -> str:
        return await anyio.to_thread.run_sync(lambda: srv.lg("lead").brief())

    return mcp


def run_stdio(toolset: str = "all", project: str | None = None) -> None:
    build(project, toolset).run("stdio")


def tool_names(mcp: MCPServer) -> list[str]:
    import asyncio

    return sorted(t.name for t in asyncio.run(mcp.list_tools()))


__all__ = ["build", "run_stdio", "jsonable"]
