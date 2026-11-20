"""The deterministic referee (§10): calibration, replication tasks and verdicts. No LLM calls.

`tick(ledger)` is idempotent: it looks at the ledger and writes only what is missing, under a session
advisory lock so concurrent ticks run one after another. Rules T1-T7 are named in the comments.
"""
from __future__ import annotations

import statistics
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import psycopg

from ...errors import InvalidTransition, PermissionDenied
from ...ids import short
from ...ledger import Ledger, partition_epsilon
from .code import short_hash

REFEREE = "referee"
REPLICATE, CALIBRATION = "replicate", "calibration"
TERMINAL_TASK = ("done", "failed", "cancelled")
JUDGED = ("verified", "refuted", "inconclusive", "superseded", "retracted")
DEFAULTS: dict[str, Any] = {"replicates": 2, "replicate_max_queue": 4, "sanity_min": 0.5, "sanity_max_gain": 0.1,
                            "epsilon_floor": 0.0005, "replicate_priority": 10, "calibration_priority": 20,
                            "calibration_rounds": 3}


def setting(lg: Ledger, key: str) -> Any:
    return lg.project.settings.get(key, DEFAULTS[key])


def referee_ledger(lg: Ledger) -> Ledger:
    ref = lg if lg.agent == REFEREE else lg.as_agent(REFEREE, role="orchestrator")
    if ref.role != "orchestrator":
        raise PermissionDenied(f"agent {REFEREE!r} must have the orchestrator role (has {ref.role})")
    return ref


class Tick:
    def __init__(self, lg: Ledger):
        self.lg = referee_ledger(lg)
        self.lg.refresh()
        self.pid = self.lg.project_id
        self.pk = self.lg.project.settings.get("partition_key", "partition")
        self.min = self.lg.project.metric_direction == "min"
        self.actions: list[str] = []

    # ------------------------------------------------------------------ helpers
    def q(self, query: str, params: Any = None) -> list[dict[str, Any]]:
        return self.lg.sql_internal(query, params)

    def better(self, a: float, b: float, eps: float) -> bool:
        return a < b - eps if self.min else a > b + eps

    def worse(self, a: float, b: float, eps: float) -> bool:
        return a > b + eps if self.min else a < b - eps

    def eps(self, part: str) -> float:
        floor = float(setting(self.lg, "epsilon_floor"))
        return partition_epsilon(self.lg.project.settings, part, max(floor, self.lg.project.improve_epsilon))

    def verified_best(self, part: str) -> dict[str, Any] | None:
        rows = self.q("SELECT * FROM assay.v_partition_best WHERE project_id=%s AND partition=%s"
                      " AND status='verified' AND rank=1", (self.pid, part))
        return rows[0] if rows else None

    def blob_uri(self, exp: dict[str, Any]) -> str | None:
        for a in exp.get("artifacts") or []:
            if isinstance(a, dict) and str(a.get("uri", "")).startswith("assay://blob/"):
                return str(a["uri"])
        return None

    def result_claim(self, origin: UUID, rnd: int = 1) -> dict[str, Any] | None:
        """The result claim of an origin's replication round (calibration can be repeated, A2)."""
        rows = self.q(
            "SELECT s.id, s.status FROM assay.links l JOIN assay.v_claim_status s"
            " ON s.project_id = l.project_id AND s.id = l.dst_id JOIN assay.claims c ON c.id = s.id"
            " WHERE l.project_id=%s AND l.src_id=%s AND l.relation='supports' AND s.kind='result'"
            " AND coalesce((c.payload->>'round')::int, 1) = %s"
            " ORDER BY s.created_at LIMIT 1", (self.pid, origin, rnd))
        return rows[0] if rows else None

    def tasks_for(self, origin: UUID, rnd: int | None = None) -> list[dict[str, Any]]:
        return self.q("SELECT * FROM assay.tasks WHERE project_id=%s AND spec->>'origin'=%s"
                      " AND tags && ARRAY['replicate','calibration']"
                      " AND (%s::int IS NULL OR coalesce((spec->>'round')::int, 1) = %s::int) ORDER BY created_at",
                      (self.pid, str(origin), rnd, rnd))

    def make_tasks(self, kind: str, exp: dict[str, Any], seeds: list[int], priority: int, rnd: int = 1) -> None:
        part = exp["config"][self.pk]
        uri = self.blob_uri(exp)
        h8 = short_hash(exp["code_ref"])
        desc = str(exp["config"].get("description") or "")[:80]
        trace = exp.get("trace_ref") or {}
        cmd = trace.get("cmd") if isinstance(trace, dict) else None
        spec: dict[str, Any] = {"kind": kind, "partition": part, "origin": str(exp["id"]), "code_ref": exp["code_ref"],
                                "code_blob": uri, "exclude_agents": [exp["author"]], "value": exp["primary_value"],
                                # A3: an agent that already re-ran this origin (in this round) does not get a
                                # second seed of it
                                "distinct_agents_for": "replication_group",
                                "replication_group": f"{exp['id']}#{rnd}"}
        if cmd:
            spec["cmd"] = cmd  # A9: the replicator runs what the original ran
        if isinstance(trace, dict) and trace.get("parse_regex"):
            spec["parse_regex"] = trace["parse_regex"]
        if rnd > 1:
            spec["round"] = rnd
        self.lg.create_tasks([{
            "title": f"{kind} {h8} on {part} (run {s} of {max(seeds)}){': ' + desc if desc else ''}",
            "spec": {**spec, "seed": s}, "tags": [kind], "priority": priority, "max_attempts": 3} for s in seeds])
        self.actions.append(f"{kind}: {len(seeds)} task(s) for {short('experiment', exp['id'])} on {part}"
                            + (f" (round {rnd})" if rnd > 1 else ""))

    # ------------------------------------------------------------------ T1, T2: calibration
    def calibrate(self) -> None:
        cal = self.lg.project.settings.get("partitions") or {}
        parts = [r["p"] for r in self.q(
            "SELECT DISTINCT config->>%s AS p FROM assay.experiments_valid WHERE project_id=%s AND run_status='ok'"
            " AND primary_value IS NOT NULL AND config->>%s IS NOT NULL ORDER BY 1", (self.pk, self.pid, self.pk))]
        for part in parts:
            tasks = self.q("SELECT * FROM assay.tasks WHERE project_id=%s AND 'calibration'=ANY(tags)"
                           " AND spec->>'partition'=%s ORDER BY created_at", (self.pid, part))
            if not tasks:  # T1
                if part in cal:
                    continue  # calibrated by hand (settings.partitions)
                first = self.q(
                    "SELECT * FROM assay.experiments_valid WHERE project_id=%s AND config->>%s=%s AND run_status='ok'"
                    " AND primary_value IS NOT NULL AND coalesce(seed,0)=0 ORDER BY created_at, id LIMIT 1",
                    (self.pid, self.pk, part))
                if first and self.blob_uri(first[0]):
                    self.make_tasks(CALIBRATION, first[0], [1, 2], int(setting(self.lg, "calibration_priority")))
                continue
            if any(t["status"] not in TERMINAL_TASK for t in tasks):
                continue
            rnd = max(int(t["spec"].get("round", 1)) for t in tasks)
            origin = UUID(tasks[0]["spec"]["origin"])
            entry_round = int((cal.get(part) or {}).get("round", 1)) if part in cal else 0
            if entry_round < rnd:
                self.calibration_entry(part, origin, rnd)  # T2
                continue
            # A2: a calibration that did not verify leaves the partition without a baseline, and no candidate
            # can be judged against nothing. Run the baseline code again with new seeds (bounded).
            judged = self.result_claim(origin, rnd)
            if judged is None or judged["status"] not in JUDGED or judged["status"] == "verified":
                continue
            if self.verified_best(part) is not None or rnd >= int(setting(self.lg, "calibration_rounds")):
                continue
            exp = self.q("SELECT * FROM assay.experiments_valid WHERE id=%s", (origin,))
            if not exp:
                continue
            used = self.q("SELECT coalesce(max(seed), 0) AS m FROM assay.experiments WHERE project_id=%s"
                          " AND dedup_key=%s", (self.pid, exp[0]["dedup_key"]))[0]["m"]
            used = max(int(used), max(int(t["spec"].get("seed", 0)) for t in tasks))
            self.make_tasks(CALIBRATION, exp[0], [used + 1, used + 2], int(setting(self.lg, "calibration_priority")),
                            rnd=rnd + 1)
        self.lg.refresh()

    def calibration_entry(self, part: str, origin: UUID, rnd: int) -> None:
        """T2: noise estimate from the origin and the runs of calibration round `rnd`."""
        tasks = [t for t in self.tasks_for(origin, rnd) if CALIBRATION in t["tags"]]
        runs = self.runs(origin, tasks)
        values = [r["primary_value"] for r in runs if r["run_status"] == "ok" and r["primary_value"] is not None]
        floor = float(setting(self.lg, "epsilon_floor"))
        sigma = statistics.stdev(values) if len(values) >= 2 else None
        epsilon = max(2 * sigma, floor) if sigma is not None else floor
        status = "ok" if len(values) >= 3 else ("partial" if sigma is not None else "failed")
        entry = {"sigma": sigma, "epsilon": epsilon, "n": len(values), "status": status, "round": rnd,
                 "calibrated_at": datetime.now(UTC).isoformat(timespec="seconds"), "origin": str(origin)}
        self.lg.update_settings({"partitions": {part: entry}})
        sig = "n/a" if sigma is None else f"{sigma:.6f}"
        self.lg.propose_claim(
            "calibration", f"Noise calibration for {part}: sigma={sig}, epsilon={epsilon:.6f} "
                           f"from {len(values)} runs of {short_hash(runs[0]['code_ref'])} ({status})"
                           + (f", round {rnd}" if rnd > 1 else ""),
            payload={"partition": part, "sigma": sigma, "epsilon": epsilon, "n": len(values),
                     "experiment_ids": [str(r["id"]) for r in runs]},
            tags=["calibration"], evidence=[(r["id"], "supports") for r in runs if r["run_status"] == "ok"])
        self.actions.append(f"calibrated {part}: sigma={sig} epsilon={epsilon:.6f}"
                            + (f" (round {rnd})" if rnd > 1 else ""))

    def runs(self, origin: UUID, tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """The origin run followed by the runs recorded for its replicate/calibration tasks."""
        rows = self.q("SELECT * FROM assay.experiments_valid WHERE project_id=%s AND (id=%s OR task_id = ANY(%s))"
                      " ORDER BY (id=%s) DESC, seed NULLS FIRST, created_at",
                      (self.pid, origin, [t["id"] for t in tasks], origin))
        return rows

    # ------------------------------------------------------------------ T3, T4: replication tasks
    def replicate(self) -> None:
        cands = self.q(
            "SELECT e.* FROM assay.experiments_valid e WHERE e.project_id=%s AND e.outcome='improved'"
            " AND e.run_status='ok' AND coalesce(e.seed,0)=0 AND e.config->>%s IS NOT NULL"
            # A1: a run its author discarded for not beating its own parent is not queued for replication
            " AND coalesce((e.trace_ref->>'beats_parent')::boolean, true)"
            " AND NOT EXISTS (SELECT 1 FROM assay.tasks t WHERE t.project_id=e.project_id"
            "                 AND t.spec->>'origin' = e.id::text)"
            " AND NOT EXISTS (SELECT 1 FROM assay.links l JOIN assay.claims c ON c.id=l.dst_id AND c.kind='result'"
            "                 WHERE l.project_id=e.project_id AND l.src_id=e.id AND l.relation='supports')"
            " ORDER BY e.created_at, e.id", (self.pid, self.pk))
        n = int(setting(self.lg, "replicates"))
        max_queue = int(setting(self.lg, "replicate_max_queue"))
        for e in cands:
            part = e["config"][self.pk]
            cal_tasks = self.q("SELECT 1 FROM assay.tasks WHERE project_id=%s AND 'calibration'=ANY(tags)"
                               " AND spec->>'partition'=%s LIMIT 1", (self.pid, part))
            if not cal_tasks and part not in (self.lg.project.settings.get("partitions") or {}):
                continue  # T1 runs first for a new partition; its first run is the calibration origin
            if not self.blob_uri(e):
                continue
            value = float(e["primary_value"])
            best = self.verified_best(part)
            eps = self.eps(part)
            self.sanity(e, value, best)  # T4
            if best is not None and best["mean"] is not None and self.worse(value, float(best["mean"]), eps):
                self.close(e, "inconclusive", "none",
                           f"not replicated: its single run {value:.6f} cannot beat the verified best "
                           f"{best['mean']:.6f} recorded since", [e["id"]])
                continue
            queued = self.q(
                "SELECT spec->>'origin' AS origin, (spec->>'value')::float8 AS value,"
                " bool_or(status='claimed') AS running FROM assay.tasks WHERE project_id=%s"
                " AND 'replicate'=ANY(tags) AND spec->>'partition'=%s AND status IN ('open','claimed')"
                " GROUP BY 1, 2", (self.pid, part))
            if len(queued) >= max_queue:
                idle = [r for r in queued if not r["running"] and r["value"] is not None]
                worst = max(idle, key=lambda r: r["value"] if self.min else -r["value"], default=None)
                if worst is not None and self.better(value, worst["value"], 0.0):
                    for t in self.q("SELECT id FROM assay.tasks WHERE project_id=%s AND spec->>'origin'=%s"
                                    " AND status='open'", (self.pid, worst["origin"])):
                        self.lg.cancel_task(t["id"], "replication queue full: a larger improvement arrived")
                    self.actions.append(f"queue full: dropped {short('experiment', worst['origin'])}")
                else:
                    self.close(e, "inconclusive", "none",
                               f"not replicated: the replication queue for {part} is full ({max_queue}) "
                               "with larger improvements", [e["id"]])
                    continue
            self.make_tasks(REPLICATE, e, list(range(1, n + 1)), int(setting(self.lg, "replicate_priority")))

    def sanity(self, e: dict[str, Any], value: float, best: dict[str, Any] | None) -> None:
        reasons = []
        lo = float(setting(self.lg, "sanity_min"))
        if self.min and value < lo:
            reasons.append(f"below sanity_min {lo:g}")
        gain_max = float(setting(self.lg, "sanity_max_gain"))
        if best is not None and best["mean"] is not None:
            gain = (float(best["mean"]) - value) if self.min else (value - float(best["mean"]))
            if gain > gain_max:
                reasons.append(f"improves the verified best by {gain:.4f} > sanity_max_gain {gain_max:g}")
        if reasons:
            self.lg.propose_claim(
                "observation", f"Suspicious result {short('experiment', e['id'])}: "
                               f"{self.lg.project.primary_metric}={value:.6f} ({'; '.join(reasons)}). "
                               "It is replicated like any candidate; a single run never becomes verified.",
                tags=["suspicious"], evidence=[(e["id"], "supports")])
            self.actions.append(f"suspicious {short('experiment', e['id'])}")

    # ------------------------------------------------------------------ T5, T6: verdicts
    def judge(self) -> None:
        origins = self.q(
            "SELECT t.spec->>'origin' AS origin, coalesce((t.spec->>'round')::int, 1) AS rnd, min(e.created_at) AS at"
            " FROM assay.tasks t JOIN assay.experiments e ON e.id = (t.spec->>'origin')::uuid"
            " WHERE t.project_id=%s AND t.tags && ARRAY['replicate','calibration'] GROUP BY 1, 2 ORDER BY 3, 1, 2",
            (self.pid,))
        for row in origins:
            origin, rnd = UUID(row["origin"]), int(row["rnd"])
            tasks = self.tasks_for(origin, rnd)
            if any(t["status"] not in TERMINAL_TASK for t in tasks):
                continue
            existing = self.result_claim(origin, rnd)
            if existing is not None and existing["status"] in JUDGED:
                continue
            self.verdict(origin, tasks, rnd)

    def verdict(self, origin: UUID, tasks: list[dict[str, Any]], rnd: int = 1) -> None:
        runs = self.runs(origin, tasks)
        exp = runs[0]
        part = exp["config"][self.pk]
        is_cal = any(CALIBRATION in t["tags"] for t in tasks)
        eps = self.eps(part)
        by_task = {r["task_id"] for r in runs[1:]}
        missing = [t for t in tasks if t["id"] not in by_task]
        bad = [r for r in runs if r["run_status"] != "ok" or r["primary_value"] is None]
        values = [float(r["primary_value"]) for r in runs if r["run_status"] == "ok" and r["primary_value"] is not None]
        mean = statistics.fmean(values) if values else None
        std = statistics.stdev(values) if len(values) >= 2 else None
        # The first run was selected for being good (winner's curse); the replications must win on their own too.
        rep_values = [float(r["primary_value"]) for r in runs[1:] if r["run_status"] == "ok"
                      and r["primary_value"] is not None]
        rep_mean = statistics.fmean(rep_values) if rep_values else None
        best = self.verified_best(part)
        baseline = float(best["mean"]) if best is not None and best["mean"] is not None else None
        metric = self.lg.project.primary_metric
        rep_authors = [r["author"] for r in runs[1:]]
        if not (missing or bad) and not is_cal and baseline is None:
            return  # A2: nothing verified to beat yet; wait for (re)calibration instead of verifying by default
        if missing or bad:
            why = []
            if missing:
                states = ", ".join(sorted({t["status"] for t in missing}))
                why.append(f"{len(missing)} replication(s) produced no run ({states})")
            if bad:
                why.append(f"{len(bad)} run(s) failed")
            status, rationale = "inconclusive", "; ".join(why)
        elif exp["author"] in rep_authors or len(set(rep_authors)) < len(rep_authors):
            # A3: "two other agents" means two distinct agents, neither of them the original author
            status = "inconclusive"
            rationale = (f"replications were not independent: original by {exp['author']}, replications by "
                         f"{', '.join(rep_authors)}")
        elif is_cal:
            status = "verified"
            rationale = (f"{len(values)} runs by {len({r['author'] for r in runs})} agents agree: mean "
                         f"{mean:.6f}" + (f" ± {std:.6f}" if std is not None else "")
                         + " (calibration run of the first code in this partition)")
        elif self.better(mean, baseline, eps) and self.better(rep_mean, baseline, eps):  # type: ignore[arg-type]
            status = "verified"
            rationale = (f"mean {mean:.6f} over {len(values)} runs (replications alone {rep_mean:.6f}) beats the "
                         f"verified best {baseline:.6f} by more than epsilon {eps:.6f}")
        else:
            status = "refuted"
            rationale = (f"mean {mean:.6f} over {len(values)} runs (replications alone {rep_mean:.6f}) does not "
                         f"beat the verified best {baseline:.6f} by epsilon {eps:.6f} "
                         f"(first run: {exp['primary_value']:.6f})")
        h8 = short_hash(exp["code_ref"])
        sm = "n/a" if mean is None else f"{mean:.6f}"
        statement = (f"Code {h8} ({short('experiment', origin)}, {exp['author']}) on {part} reaches {metric} "
                     f"{sm}" + (f" ± {std:.6f}" if std is not None else "") + f" over {len(values)} runs"
                     + ("" if is_cal or baseline is None else f", beating the verified best {baseline:.6f}")
                     + (f" (calibration round {rnd})" if rnd > 1 else ""))
        payload: dict[str, Any] = {
            "partition": part, "code_ref": exp["code_ref"], "experiment_ids": [str(r["id"]) for r in runs],
            "mean": mean, "std": std, "n": len(values), "origin": str(origin), "origin_author": exp["author"],
            "baseline": baseline, "epsilon": eps, "replicate_mean": rep_mean}
        if is_cal:
            payload["measurement"] = True  # a calibration states the baseline's value; it beats nothing (C1)
        if rnd > 1:
            payload["round"] = rnd
        existing = self.result_claim(origin, rnd)
        if existing is None:
            cid = self.lg.propose_claim("result", statement[:500], payload=payload, tags=["result", part],
                                        evidence=[(origin, "supports")]).id
            current = "proposed"
        else:
            cid, current = existing["id"], existing["status"]
        evidence = [r["id"] for r in runs]
        detail = {"mean": mean, "std": std, "n": len(values), "baseline": baseline, "epsilon": eps,
                  "replicate_mean": rep_mean}
        if status == "verified":
            try:
                if current == "proposed":
                    self.lg.decide(cid, "supported", "metric", f"first run {exp['primary_value']:.6f} improved",
                                   [origin], oracle_detail=detail)
                self.lg.decide(cid, "verified", "rerun", rationale, evidence, oracle_detail=detail)
            except InvalidTransition as err:
                self.lg.decide(cid, "inconclusive", "rerun", f"replication rule not met: {err}"[:1000], evidence,
                               oracle_detail=detail)
                status = "inconclusive"
        else:
            self.lg.decide(cid, status, "rerun", rationale[:1000], evidence, oracle_detail=detail)
        self.actions.append(f"{status}: {short('experiment', origin)} on {part} ({rationale[:80]})")

    def close(self, e: dict[str, Any], status: str, oracle: str, rationale: str, evidence: list[Any]) -> None:
        """Judge a candidate without replication (queue full, already beaten)."""
        part = e["config"][self.pk]
        payload = {"partition": part, "code_ref": e["code_ref"], "experiment_ids": [str(e["id"])],
                   "mean": e["primary_value"], "std": None, "n": 1, "origin": str(e["id"]),
                   "origin_author": e["author"]}
        statement = (f"Code {short_hash(e['code_ref'])} ({short('experiment', e['id'])}, {e['author']}) on {part}: "
                     f"single run {e['primary_value']:.6f}, not replicated")
        cid = self.lg.propose_claim("result", statement[:500], payload=payload, tags=["result", part],
                                    evidence=[(e["id"], "supports")]).id
        self.lg.decide(cid, status, oracle, rationale[:1000], evidence)
        self.actions.append(f"{status}: {short('experiment', e['id'])} ({rationale[:60]})")

    # ------------------------------------------------------------------ T7: prune hopeless replications
    def prune(self) -> None:
        rows = self.q(
            "SELECT t.id, t.spec->>'partition' AS part, (t.spec->>'value')::float8 AS value FROM assay.tasks t"
            " WHERE t.project_id=%s AND 'replicate'=ANY(t.tags) AND t.status='open'", (self.pid,))
        for t in rows:
            best = self.verified_best(t["part"])
            if best is None or best["mean"] is None or t["value"] is None:
                continue
            if self.worse(float(t["value"]), float(best["mean"]), self.eps(t["part"])):
                self.lg.cancel_task(t["id"], f"cannot beat the new verified best {best['mean']:.6f}")
                self.actions.append(f"cancelled {short('task', t['id'])}: beaten by verified best")

    def run(self) -> list[str]:
        self.calibrate()
        self.judge()
        self.prune()
        self.replicate()
        self.judge()
        return self.actions


def tick(lg: Ledger) -> list[str]:
    """Run every referee rule once. Serialised per project; safe to call from every agent."""
    ref = referee_ledger(lg)
    with psycopg.connect(ref.url, autocommit=True) as conn:
        conn.execute("SELECT pg_advisory_lock(hashtext(%s))", ("assay-referee:" + ref.project_id,))
        try:
            return Tick(ref).run()
        finally:
            conn.execute("SELECT pg_advisory_unlock(hashtext(%s))", ("assay-referee:" + ref.project_id,))


