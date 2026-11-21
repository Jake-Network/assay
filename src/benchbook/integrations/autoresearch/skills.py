"""Run records of the autoresearch skills, read into rows that ops.import_results records (PR 2-R1).

Upstream autoresearch has not moved since 2026-03; its users run the loop through skills for other host agents,
and each skill keeps its own log. The three formats below were read from each repository's source (the
commits are named in tests/fixtures/autoresearch/skills/README.md):

- pi-autoresearch (davebcn87): `.auto/log.jsonl` (legacy `autoresearch.jsonl`), written by the extension's
  init_experiment and log_experiment tools.
- uditgoenka/autoresearch: the results TSV of `/autoresearch` in `autoresearch/loop-<stamp>/`, written by the
  agent from the command's instructions.
- codex-autoresearch (leo-lilinxiao): `autoresearch-results/events.jsonl` beside `run.json`, written by the
  skill's control script.

Pure parsing: nothing here touches git or the ledger.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any

from ...errors import BenchbookError

_SHA = re.compile(r"^[0-9a-fA-F]{7,40}$")
_DIRECTION = re.compile(r"^#\s*metric_direction:\s*(higher|lower)_is_better", re.IGNORECASE)


@dataclass(frozen=True)
class Row:
    """One run a skill logged, in the ledger's terms."""

    key: str  # identity of the record in its log: the same record always gets the same key (idempotent import)
    label: str  # how notes name the record: "run 3", "iteration 3", "baseline"
    status: str  # the skill's own word, verbatim
    run_status: str  # ok | error | invalid: the existing run_status this status maps to
    kept: bool
    value: float | None
    description: str
    commit: str | None = None  # the commit whose train.py is the code that ran; None when the skill kept none
    parent_commit: str | None = None  # for a row without code: the commit the reverted change started from
    metric: str | None = None  # the name the log gives its primary metric, when it gives one
    direction: str | None = None  # min | max, when the log says
    metrics: dict[str, float] = field(default_factory=dict)  # secondary metrics
    detail: dict[str, Any] = field(default_factory=dict)  # kept in trace_ref


@dataclass(frozen=True)
class Log:
    rows: list[Row]
    skipped: int = 0  # records that are not runs (nothing was measured) or carry a status the skill never writes


@dataclass(frozen=True)
class Skill:
    read: Callable[[Path], Log]
    candidates: tuple[str, ...]  # where the log is, relative to a directory, in the skill's own order of preference
    log_hint: str  # the path the one-line import command shows
    attach: str  # where the skill's agent reads standing instructions from


def _key(source: str, *identity: Any) -> str:
    raw = json.dumps([source, *identity], sort_keys=True, default=str, ensure_ascii=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _number(v: Any) -> float | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return float(v) if math.isfinite(v) else None


def _sha(v: Any) -> str | None:
    s = v.strip() if isinstance(v, str) else ""
    return s if _SHA.match(s) else None


# ------------------------------------------------------------------------------------------ pi-autoresearch

# log_experiment's status enum. discard, crash and checks_failed revert the working tree before anything is
# committed, so only a keep names the commit of the code that ran; for the others `commit` is the HEAD the
# change started from.
PI_STATUS: dict[str, tuple[str, bool]] = {"keep": ("ok", True), "discard": ("ok", False), "crash": ("error", False),
                                          "checks_failed": ("invalid", False)}


def read_pi(path: Path) -> Log:
    """`{"type":"config",...}` opens a segment (name, metric, direction); a line with a numeric `run` is one
    experiment; other lines (hook entries) and lines that do not parse are ignored, as the extension does."""
    metric: str | None = None
    direction: str | None = None
    rows: list[Row] = []
    skipped = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if not isinstance(entry, dict):
            continue
        if entry.get("type") == "config":
            if isinstance(entry.get("metricName"), str):
                metric = entry["metricName"]
            direction = "max" if entry.get("bestDirection") == "higher" else "min"
            continue
        run = entry.get("run")
        if isinstance(run, bool) or not isinstance(run, (int, float)):
            continue
        status = entry.get("status")
        if status not in PI_STATUS:
            skipped += 1
            continue
        run_status, kept = PI_STATUS[status]
        value = _number(entry.get("metric"))
        if run_status == "ok" and value is None:
            skipped += 1
            continue
        commit = _sha(entry.get("commit"))
        secondary = entry.get("metrics")
        rows.append(Row(
            key=_key("pi-autoresearch", run, entry.get("timestamp"), entry.get("commit"), entry.get("metric"), status),
            label=f"run {int(run)}", status=status, run_status=run_status, kept=kept,
            value=value if run_status != "error" else None,
            description=str(entry.get("description") or ""), commit=commit if kept else None,
            parent_commit=None if kept else commit, metric=metric, direction=direction,
            metrics={k: n for k, v in (secondary if isinstance(secondary, dict) else {}).items()
                     if (n := _number(v)) is not None},
            detail={k: entry[k] for k in ("run", "segment", "timestamp", "confidence") if entry.get(k) is not None}))
    return Log(rows, skipped)


# ------------------------------------------------------------------------------------------ uditgoenka/autoresearch

# The eight status values of the loop's TSV. A no-op or a hook-blocked iteration measured nothing, so it is not
# a run. Only baseline and keep rows name a commit; every other row has "-" there (the change was reverted).
# Fork-and-flush rounds add `fork-<k>` and `fork-select` rows that summarise branches (their best metric, not a
# run of their own); like any other status not listed here they are counted as skipped.
UDIT_STATUS: dict[str, tuple[str, bool] | None] = {
    "baseline": ("ok", True), "keep": ("ok", True), "keep (reworked)": ("ok", True), "discard": ("ok", False),
    "crash": ("error", False), "metric-error": ("error", False), "no-op": None, "hook-blocked": None}
# The loop's columns (v2.0.03 files lack timestamp). Other subcommands write TSVs with other columns, such as
# /autoresearch:fix with error_type and error_fixed, whose metric is not a training result: those are refused.
UDIT_COLUMNS = ("iteration", "timestamp", "commit", "metric", "delta", "guard", "guard-metric", "status", "description")
_NO_HEADER = "not an autoresearch results TSV (no header row starting with 'iteration')"


def read_uditgoenka(path: Path) -> Log:
    """`# metric_direction: ...`, a header row starting with `iteration`, then one row per iteration. Columns are
    found by name: v2.0.03 files have no timestamp column. The TSV does not name its metric, only its direction."""
    direction: str | None = None
    header: list[str] | None = None
    rows: list[Row] = []
    skipped = 0
    incumbent: str | None = None  # the last kept commit: what a reverted change started from (the delta column's base)
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        if line.startswith("#"):
            if m := _DIRECTION.match(line):
                direction = "max" if m.group(1).lower() == "higher" else "min"
            continue
        cells = line.split("\t")
        if cells[0].strip().lower() == "iteration":
            header = [c.strip().lower() for c in cells]
            missing = [c for c in ("commit", "metric", "status") if c not in header]
            other = [c for c in header if c not in UDIT_COLUMNS]
            if missing or other:
                what = f"no {' or '.join(missing)} column" if missing else f"columns {', '.join(other)}"
                raise BenchbookError(f"{path}: {what}. Only the results TSV of the /autoresearch loop can be "
                                     "imported, not the TSV of another subcommand.")
            continue
        if header is None:
            raise BenchbookError(f"{path}: {_NO_HEADER}")
        if len(cells) > len(header):  # a tab inside the description, the last column
            cells = [*cells[:len(header) - 1], " ".join(cells[len(header) - 1:])]
        rec = dict(zip(header, (c.strip() for c in cells), strict=False))
        status = rec.get("status", "").lower()
        mapped = UDIT_STATUS.get(status)
        if mapped is None:
            skipped += 1
            continue
        run_status, kept = mapped
        try:
            value = _number(float(rec.get("metric", "")))
        except ValueError:
            value = None
        if run_status == "ok" and value is None:
            skipped += 1
            continue
        if run_status == "ok" and rec.get("guard", "").lower() == "fail":
            run_status, kept = "invalid", False  # measured, but the guard command failed: not a result
        commit = _sha(rec.get("commit")) if kept else None
        rows.append(Row(
            # not the description: an agent that rewords a row it already logged must not create a second record
            key=_key("uditgoenka", rec.get("iteration"), rec.get("timestamp"), rec.get("commit"), rec.get("metric"),
                     status),
            label=f"iteration {rec.get('iteration', '?')}", status=status, run_status=run_status, kept=kept,
            value=value if run_status != "error" else None, description=rec.get("description", ""),
            commit=commit, parent_commit=None if commit else incumbent, direction=direction,
            detail={k: rec[k] for k in ("iteration", "timestamp", "delta", "guard")
                    if rec.get(k) not in (None, "", "-")}))
        if commit:
            incumbent = commit
    if header is None:
        raise BenchbookError(f"{path}: {_NO_HEADER}")
    return Log(rows, skipped)


# ------------------------------------------------------------------------------------------ codex-autoresearch

CODEX_SCHEMA = 2


def _strict_json(text: str, where: str) -> dict[str, Any]:
    try:
        data = json.loads(text)
    except ValueError as e:
        raise BenchbookError(f"{where}: invalid JSON ({e})") from e
    if not isinstance(data, dict):
        raise BenchbookError(f"{where}: expected a JSON object")
    return data


def read_codex(path: Path) -> Log:
    """`run.json` (immutable settings: run id, metric name and direction) and `events.jsonl`: one `baseline`
    event, then an `iteration` event per trial. Every trial is committed before it is measured and reverted with
    `git revert` when it is not kept, so `trial_commit` names the code that ran for discards and crashes too.
    The skill treats a damaged log as an error instead of guessing, and so does this reader."""
    run_path = path.with_name("run.json")
    if not run_path.is_file():
        raise BenchbookError(f"{run_path} is missing: codex-autoresearch keeps the metric name and direction there")
    run = _strict_json(run_path.read_text(encoding="utf-8"), str(run_path))
    if run.get("schema_version") != CODEX_SCHEMA:
        raise BenchbookError(f"{run_path}: schema_version {run.get('schema_version')!r}; this adapter reads "
                             f"schema {CODEX_SCHEMA}")
    spec = run.get("metric")
    spec = spec if isinstance(spec, dict) else {}
    metric = spec.get("name") if isinstance(spec.get("name"), str) else None
    direction = {"lower": "min", "higher": "max"}.get(str(spec.get("direction")))
    run_id = run.get("run_id")
    rows: list[Row] = []
    skipped = 0
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        ev = _strict_json(line, f"{path}:{n}")
        if ev.get("schema_version") != CODEX_SCHEMA or ev.get("run_id") != run_id:
            raise BenchbookError(f"{path}:{n}: the event does not belong to the run in {run_path.name}")
        kind = ev.get("event")
        key = _key("codex-autoresearch", run_id, ev.get("seq"))
        detail = {"run_id": run_id, "seq": ev.get("seq"), "time": ev.get("time")}
        if kind == "baseline":
            value = _number(ev.get("metric"))
            if value is None:
                skipped += 1
                continue
            rows.append(Row(key=key, label="baseline", status="baseline", run_status="ok", kept=True, value=value,
                            description="baseline", commit=_sha(ev.get("head")), metric=metric, direction=direction,
                            detail=detail))
        elif kind == "iteration":
            outcome, guard = ev.get("outcome"), ev.get("guard")
            value = None if outcome == "crash" else _number(ev.get("trial_metric"))
            if outcome not in ("keep", "discard", "crash") or (outcome != "crash" and value is None):
                skipped += 1
                continue
            # A discard with a failed guard improved the metric and broke the guard command: measured, not a result.
            run_status = "error" if outcome == "crash" else ("invalid" if guard == "fail" else "ok")
            rows.append(Row(key=key, label=f"iteration {ev.get('iteration')}", status=str(outcome),
                            run_status=run_status, kept=outcome == "keep", value=value,
                            description=str(ev.get("description") or ""), commit=_sha(ev.get("trial_commit")),
                            metric=metric, direction=direction,
                            detail={**detail, "iteration": ev.get("iteration"), "guard": guard}))
        # blocked, complete, error, resumed and stopped events describe the run, not a trial
    return Log(rows, skipped)


# ------------------------------------------------------------------------------------------ registry

SKILLS: dict[str, Skill] = {
    "pi-autoresearch": Skill(read_pi, (".auto/log.jsonl", "log.jsonl", "autoresearch.jsonl"), ".auto/log.jsonl",
                             ".auto/prompt.md"),
    "uditgoenka": Skill(read_uditgoenka, (), "autoresearch/loop-<stamp>/<results>.tsv", "CLAUDE.md"),
    "codex-autoresearch": Skill(read_codex, ("autoresearch-results/events.jsonl", "events.jsonl"),
                                "autoresearch-results", "AGENTS.md"),
}


def locate(source: str, path: Path) -> Path:
    """The skill's log file. A directory is searched the way the skill itself looks for its log."""
    if path.is_file():
        return path
    if not path.is_dir():
        raise BenchbookError(f"no such file: {path}")
    skill = SKILLS[source]
    for rel in skill.candidates:
        if (path / rel).is_file():
            return path / rel
    if source == "uditgoenka":
        found = sorted(path.glob("*.tsv"))
        if len(found) == 1:
            return found[0]
        raise BenchbookError(f"pass the results TSV itself: {path} holds {len(found)} .tsv files "
                             "(the loop writes its TSV into autoresearch/loop-<stamp>/)")
    raise BenchbookError(f"no {source} log in {path} (looked for {', '.join(skill.candidates)})")


def read(source: str, path: Path) -> Log:
    try:
        return SKILLS[source].read(path)
    except (OSError, UnicodeDecodeError) as e:
        raise BenchbookError(f"cannot read {path}: {e}") from e


def fragment(source: str, hw: str) -> str:
    """The program fragment for a checkout driven by this skill, with the hardware class filled in."""
    text = resources.files("benchbook.integrations.autoresearch").joinpath(
        f"templates/program.{source}.md").read_text(encoding="utf-8")
    return text.replace("{hw}", hw)
