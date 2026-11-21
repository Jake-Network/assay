"""`benchbook finding accept`: accept an out-of-scope finding the hook captured, with its follow-up issue
(docs/code.md).

Rejecting needs nothing new: `benchbook decide <claim> refuted --oracle ... --rationale ...`. Accepting is
`supported` with the follow-up issue linked as a source, and no other command creates a source; this one adds the
issue, links it to the finding and decides, all through the existing ledger calls.
"""
from __future__ import annotations

import re
from typing import Annotated

import typer

finding_app = typer.Typer(no_args_is_help=True, help="Out-of-scope findings: accept one with its follow-up issue.")

URL = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://\S+")


@finding_app.command("accept")
def accept(
    claim: Annotated[str, typer.Argument(help="The finding's claim id (c:...)")],
    issue: Annotated[str, typer.Option("--issue", help="URL of the follow-up issue")],
    oracle: Annotated[str, typer.Option(help="Who judged: human, or llm_judge when an agent lead decides")],
    rationale: Annotated[str | None, typer.Option(help="Why (default: accepted, follow-up <issue>)")] = None,
    as_: Annotated[str | None, typer.Option("--as", help="agent to decide as (e.g. human)")] = None,
) -> None:
    """Accept a finding: link the follow-up issue as a source and decide the claim supported (orchestrator or
    human only)."""
    from .cli import emit, ledger
    from .errors import BenchbookError, ValidationFailed
    from .findings import FINDING_TAG
    from .ids import short
    from .ledger import ORACLES, TRANSITIONS

    issue = issue.strip()
    why = rationale or f"accepted, follow-up {issue}"
    problems = []
    if not URL.fullmatch(issue) or len(issue) > 2000:
        problems.append("--issue: the follow-up issue's URL, such as https://github.com/org/repo/issues/12")
    if oracle not in ORACLES:
        problems.append(f"--oracle: must be one of {', '.join(ORACLES)}")
    if len(why) > 1000:
        problems.append("--rationale: at most 1000 characters")
    if problems:
        raise ValidationFailed(problems)
    lg = ledger(as_)
    lg._require_decider()  # checked before the source is written, not only by decide()
    _, cid = lg.resolve(claim, ["claim"])
    rec = lg.get(cid)
    if FINDING_TAG not in (rec["record"]["tags"] or []):
        raise BenchbookError(f"{short('claim', cid)} is not an out-of-scope finding (no {FINDING_TAG} tag); use "
                             "`benchbook decide` for other claims")
    status = rec["status"]["status"]
    if "supported" not in TRANSITIONS.get(status, set()):
        raise BenchbookError(f"{short('claim', cid)} is already decided ({status})")
    sid = lg.add_source(issue, title="follow-up issue")
    lid = lg.link(sid, "cites", cid, note="follow-up issue of an accepted out-of-scope finding")
    d = lg.decide(cid, "supported", oracle, why, [sid])
    emit({"ok": True, "id": short("decision", d.id), "claim": short("claim", cid), "status": d.new_status,
          "source": short("source", sid), "link": short("link", lid)},
         f"{short('decision', d.id)} {short('claim', cid)} -> supported (follow-up {short('source', sid)}: {issue})")
