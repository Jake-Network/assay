"""`benchbook get <id>` as text an agent can afford (T12): one record, its status and how it was checked, its
statement or signature, up to five links each way and the last three decisions, within LIMIT_TOKENS. What is cut
is counted, never silently dropped. `--json` keeps the whole record (Ledger.get).

Every lookup is appended to lookups.jsonl in the state directory (time, project, id), like observed.jsonl: the ids a
brief carried that were then looked up are the signal that the brief was read."""
from __future__ import annotations

import json
import time
from typing import Any

from .tokens import estimate_tokens

LIMIT_TOKENS = 400
LINKS_SHOWN = 5
DECISIONS_SHOWN = 3
LOOKUPS = "lookups.jsonl"
# How a decision's oracle reads in a brief or a lookup ("checked: re-run"); pack.toml [brief.labels] overrides.
LABELS = {"rerun": "re-run", "pytest": "re-run", "metric": "re-run", "lean": "kernel", "human": "person",
          "llm_judge": "LLM judgement", "none": "none"}
# The payload and metrics keys worth a line, in this order.
PAYLOAD_KEYS = ("piece", "commit", "signature", "approach", "file", "reported_by", "paths", "sha256")
METRIC_KEYS = ("checks_passed", "checks_total", "failure_signature", "guard_weakened", "checks_changed",
               "environment", "via")


def checked(oracle: str | None, labels: dict[str, str] | None = None) -> str:
    """The "checked:" label of an oracle; "none" when nothing decided the record."""
    if not oracle:
        return "none"
    return {**LABELS, **(labels or {})}.get(oracle, oracle)


def _clip(v: Any, n: int) -> str:
    s = " ".join(str(v).split())
    return s if len(s) <= n else s[:n - 1] + "…"


def _pairs(d: Any, keys: tuple[str, ...], n: int) -> str:
    if not isinstance(d, dict):
        return ""
    out = []
    for k in keys:
        v = d.get(k)
        if v is None or v == [] or v == "":
            continue
        if k == "commit" and isinstance(v, str):
            v = v[:12]
        out.append(f"{k}={_clip(v if not isinstance(v, list) else ','.join(map(str, v)), n)}")
    return ", ".join(out)


def render(got: dict[str, Any], limit: int = LIMIT_TOKENS, labels: dict[str, str] | None = None) -> str:
    """Ledger.get's record as at most `limit` tokens of text."""
    kind, rid, rec = got.get("kind"), got.get("id"), got.get("record") or {}
    status = got.get("status") or {}
    head = f"{kind} {rid}"
    if kind == "claim":
        head += (f" ({rec.get('kind')}) · {status.get('status', 'proposed')} · "
                 f"checked: {checked(status.get('last_oracle'), labels)}")
    elif kind == "experiment":
        head += f" · run {rec.get('run_status')} by {rec.get('author')}"
        if rec.get("code_ref"):
            head += f" at {str(rec['code_ref'])[:12]}"
        if got.get("invalidated"):
            head += " · invalidated"
    elif kind == "task":
        head += f" · {rec.get('status')} · {rec.get('lease_owner') or rec.get('assignee') or ''}".rstrip(" ·")
    body: list[str] = []
    if rec.get("statement"):
        body.append(f"statement: {_clip(rec['statement'], 300)}")
    if rec.get("title"):
        body.append(f"title: {_clip(rec['title'], 200)}")
    if (p := _pairs(rec.get("payload"), PAYLOAD_KEYS, 120)):
        body.append(f"payload: {p}")
    if kind == "experiment":
        if (c := _pairs(rec.get("config"), ("piece", "checks", "approach"), 80)):
            body.append(f"config: {c}")
        if (m := _pairs(rec.get("metrics"), METRIC_KEYS, 120)):
            body.append(f"metrics: {m}")
    if rec.get("tags"):
        body.append(f"tags: {_clip(', '.join(rec['tags']), 200)}")

    def links(title: str, rows: list[dict[str, Any]], other: str, shown: int) -> list[str]:
        if not rows:
            return []
        out = [f"{title} ({len(rows)}):"]
        out += [f"- {r['relation']} {r[other]} by {r['author']}" for r in rows[:shown]]
        if len(rows) > shown:
            out.append(f"- … {len(rows) - shown} more (--json)")
        return out

    decisions = list(got.get("decisions") or [])

    def build(shown: int, dshown: int) -> str:
        lines = [head, *body]
        lines += links("links out", list(got.get("links_out") or []), "dst", shown)
        lines += links("links in", list(got.get("links_in") or []), "src", shown)
        if decisions:
            lines.append(f"decisions ({len(decisions)}, newest first):")
            for d in list(reversed(decisions))[:dshown]:
                lines.append(f"- {d['status']} by {d['by']} (checked: {checked(d['oracle'], labels)}): "
                             f"{_clip(d.get('rationale') or '', 120)}")
            if len(decisions) > dshown:
                lines.append(f"- … {len(decisions) - dshown} more (--json)")
        return "\n".join(lines)

    shown, dshown = LINKS_SHOWN, DECISIONS_SHOWN
    out = build(shown, dshown)
    while estimate_tokens(out) > limit and (shown or dshown):
        if shown:
            shown -= 1
        else:
            dshown -= 1
        out = build(shown, dshown)
    while estimate_tokens(out) > limit and body:  # long statements in a non-ASCII language
        body[-1] = _clip(body[-1], max(20, len(body[-1]) // 2))
        if len(body[-1]) <= 21:
            body.pop()
        out = build(shown, dshown)
    return out


def log_lookup(project: str | None, ref: str) -> None:
    """Append one line to lookups.jsonl in the state directory; never raises."""
    from ..config import state_dir

    try:
        d = state_dir()
        d.mkdir(parents=True, exist_ok=True)
        with (d / LOOKUPS).open("a", encoding="utf-8") as f:
            f.write(json.dumps({"at": time.time(), "project": project, "id": ref}, sort_keys=True) + "\n")
    except OSError:
        pass
