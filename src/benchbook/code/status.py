"""Code project status and the local cache read by a harness status line."""
from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..core.ledger import Ledger

RECORDING_STOPPED = "benchbook: recording stopped: "

# Count each piece at its most recently reported active commit, even when several agents reported that commit.
# Verification and disagreements are read from the ledger. A failed rerun leaves a refutation before a decision.
COUNTS = """
WITH reports AS (
  SELECT c.id, c.payload ->> 'piece' AS piece, c.payload ->> 'commit' AS commit,
         s.created_at, s.status, s.last_oracle, s.n_support, s.n_refute, s.evidence_invalidated
  FROM benchbook.claims c JOIN benchbook.v_claim_status s ON s.project_id = c.project_id AND s.id = c.id
  WHERE c.project_id = %(project)s AND c.kind = 'completion'
    AND s.status NOT IN ('retracted', 'superseded')
    AND c.payload ->> 'piece' IS NOT NULL AND c.payload ->> 'commit' IS NOT NULL
), latest AS (
  SELECT DISTINCT ON (piece) piece, commit FROM reports ORDER BY piece, created_at DESC, id DESC
), pieces AS (
  SELECT r.piece,
         CASE WHEN bool_or(r.status = 'refuted' OR (r.status = 'inconclusive' AND r.last_oracle = 'rerun')
                           OR (r.status <> 'verified' AND r.n_refute > 0)) THEN 'rerun_mismatch'
              WHEN bool_or(r.status = 'verified' AND NOT r.evidence_invalidated) THEN 'verified'
              WHEN bool_or(r.status IN ('proposed', 'supported', 'inconclusive') AND r.n_support > 0)
                THEN 'rerun_undecided'
              ELSE 'reported_only' END AS state
  FROM reports r JOIN latest l USING (piece, commit)
  GROUP BY r.piece
)
SELECT count(*) FILTER (WHERE state = 'verified') AS verified,
       count(*) FILTER (WHERE state = 'rerun_undecided') AS rerun_undecided,
       count(*) FILTER (WHERE state = 'reported_only') AS reported_only,
       count(*) FILTER (WHERE state = 'rerun_mismatch') AS rerun_mismatch,
       (SELECT count(*) FROM benchbook.v_claim_status
        WHERE project_id = %(project)s AND kind = 'observation' AND 'out-of-scope' = ANY(tags)
          AND status IN ('proposed', 'inconclusive')) AS open_findings
FROM pieces
"""


def counts(lg: Ledger) -> dict[str, int]:
    with lg._conn() as conn:
        result: dict[str, int] | None = conn.execute(COUNTS, {"project": lg.project_id}).fetchone()
    assert result is not None
    return result


def status(lg: Ledger, root: Path) -> dict[str, Any]:
    from ..cli.doctor import hook_health

    current = counts(lg)
    h = hook_health(root)
    return {"project": lg.project_id, "goal": lg.project.goal, "pack": "code", "pieces": {
        "verified": current["verified"], "rerun_undecided": current["rerun_undecided"],
        "reported_only": current["reported_only"], "rerun_mismatch": current["rerun_mismatch"]},
        "open_findings": current["open_findings"],
        "spool": {"pending": h.pending, "dead": h.dead, "stuck": h.stuck}, "hook_failures_24h": h.failures}


def render(s: dict[str, Any]) -> str:
    p, spool = s["pieces"], s["spool"]
    return "\n".join([
        f"{s['project']}: {s['goal']}",
        f"pieces    {p['verified']} verified, {p['rerun_undecided']} re-run not decided, {p['reported_only']} reported "
        f"only, {p['rerun_mismatch']} rerun mismatch",
        f"findings  {s['open_findings']} open out-of-scope",
        f"spool     {spool['pending']} pending, {spool['dead']} dead, {spool['stuck']} overdue",
        f"hooks     {s['hook_failures_24h']} failure line(s) in the last 24h",
    ])


def recording_warning(root: Path) -> str:
    from ..cli.doctor import hook_health

    h = hook_health(root)
    reasons = []
    if h.dead:
        reasons.append(f"{h.dead} report(s) in dead/")
    if h.failures:
        reasons.append(f"{h.failures} hook failure line(s) in the last 24h")
    if h.stuck:
        reasons.append(f"{h.stuck} overdue report(s)")
    if reasons:
        return f"{RECORDING_STOPPED}{', '.join(reasons)}. Fix: benchbook hook replay"
    return ""


def _cache_path(spool: Path, project: str) -> Path:
    key = hashlib.sha256(project.encode("utf-8")).hexdigest()
    return spool / "statusline" / (key + ".txt")


def read_cache(project: str | None = None) -> str:
    """Read the cache for the current project and spool without opening a ledger."""
    from ..config import load
    from ..errors import BenchbookError
    from ..hooks.spooler import _spool_dir

    try:
        project = project or load().project
        if isinstance(project, str) and project:
            with _cache_path(_spool_dir(), project).open(encoding="utf-8") as cache:
                return cache.readline(200).strip()
    except (OSError, ValueError, BenchbookError):
        pass
    return ""


def refresh_cache(spool: Path, project: str) -> None:
    """Best-effort after processing. A cache failure never makes a recorded entry retry."""
    from .. import config
    from ..core.ledger import Ledger

    try:
        lg = Ledger.open(project=project, agent=config.default_agent("hook"), role="harness")
        if lg.pack.name != "code":
            return
        current = counts(lg)
        verified = current["verified"]
        total = verified + current["rerun_undecided"] + current["reported_only"] + current["rerun_mismatch"]
        line = f"benchbook: verified {verified}/{total}, open findings {current['open_findings']}\n"
        path = _cache_path(spool, project)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temp: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(dir=path.parent, delete=False, encoding="utf-8", mode="w") as f:
                temp = Path(f.name)
                f.write(line)
            os.replace(temp, path)
        finally:
            if temp is not None:
                temp.unlink(missing_ok=True)
    except Exception:  # noqa: BLE001 - cache only; keep the outcome already recorded
        return
