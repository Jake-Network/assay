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

finding_app = typer.Typer(no_args_is_help=True,
                          help="Out-of-scope findings: add your own, or accept one with its follow-up issue.")

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


@finding_app.command("add")
def add(
    text: Annotated[str, typer.Argument(help="The finding, one sentence: what is wrong or missing, and where")],
    file: Annotated[list[str] | None, typer.Option("--file", help="A file it is about (repeatable)")] = None,
    piece: Annotated[str | None, typer.Option(help="The piece it was found in")] = None,
) -> None:
    """Record an out-of-scope finding you found yourself (a lead's, or one a worker told you in a message), the same
    way a report's "Out of scope" section is recorded: the brief lists it under open findings."""
    from .cli import emit, ledger
    from .errors import ValidationFailed
    from .findings import TEXT_MAX, record_noted

    if not text.strip() or len(text) > TEXT_MAX:
        raise ValidationFailed([f"text: 1-{TEXT_MAX} characters"])
    lg = ledger()
    out = record_noted(lg, text, list(file or []), piece)
    if not out.claims:
        raise ValidationFailed(["text: nothing to record"])
    emit({"ok": True, "claims": list(out.claims), "new": out.new},
         f"{out.claims[0]} {'recorded' if out.new else 'already recorded: this note added as evidence'}")
