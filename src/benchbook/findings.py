"""Out-of-scope findings in an implementer's final report (spec §8 P1, docs/code.md).

When .benchbook/config.toml sets [report] out_of_scope_heading, the result-capture hook (hooks.process) reads the
section of the subagent's final report under that heading and records one observation claim per top-level list
item, tagged out-of-scope. The parsing here is standard library only and pure; capture() does the ledger writes and
runs where the hook already writes, in the detached processor, never in the hook process itself.

Section: a markdown heading of any level, or a line that is bold as a whole, whose text is the configured heading
(compared after NFKC, case folding, whitespace collapse and dropping a trailing colon). A heading section ends at the
next heading of the same or a higher level; a bold-line section at the next heading of any level or the next bold
line. A thematic break (---) ends both. Every matching section in the report is read.

One line: outside a section, a line "<heading>: <text>" (optionally a list item, optionally bold) is one finding
whose text is what follows the colon (T05: the report formats seen in the field).

Items: each top-level list item (-, *, + or 1. / 1)) is one finding: its first paragraph is the finding's text;
nested items and indented paragraphs under it are its details. Lines inside fenced code blocks never start or end
anything. Empty items and items that only say there is nothing ("none", "없음", ...) are not findings.
"""
from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any

from .dedup import normalize_statement

if TYPE_CHECKING:
    from .ledger import Ledger

# Tags (docs/code.md "Out-of-scope findings"): the brief and the PR summary select findings by these.
FINDING_TAG = "out-of-scope"
FILE_TAG = "file:"
PIECE_TAG = "piece:"
MOD_TAG = "mod:"  # lifted from the item's own text, never inferred from a path
CLAIM_KIND = "observation"
# Domain separator of the finding dedup hash, stored in claims.statement_hash: changing it would make a finding
# reported again after an upgrade a second claim.
FINDING_DOMAIN = b"benchbook:out-of-scope\0"
STATEMENT_MAX = 500
TEXT_MAX = 4000
MAX_FINDINGS = 100  # per report; a report is not a bulk import
MAX_PATHS = 10
REPORT_URI = "subagent-report:sha256:"  # the source row of a report that named findings
NOTHING = frozenset({"none", "n/a", "na", "nothing", "nothing found", "no findings", "없음", "해당 없음", "없습니다",
                     "발견 없음", "특이 사항 없음", "특이사항 없음"})
# File names without a directory count only with one of these extensions: "hooks.process" or "os.path" is code, not a
# file, while "hooks.py" is a file.
KNOWN_EXT = frozenset("""
    py pyi ipynb ts tsx js jsx mjs cjs go rs java kt kts rb php c h cc cpp cxx hpp cs swift scala sql sh bash zsh
    toml yaml yml json jsonl md rst txt ini cfg conf lock html htm css scss sass less vue svelte proto graphql gql
    tf xml gradle mk env csv tsv
""".split())

_ATX = re.compile(r"^ {0,3}(#{1,6})(?:[ \t]+(.*?))?(?:[ \t]+#+)?[ \t]*$")
_BOLD = re.compile(r"^ {0,3}(\*\*|__)(?=\S)(.+?)(?<=\S)\1[ \t]*[:：]?[ \t]*$")
_BREAK = re.compile(r"^ {0,3}([-*_])(?:[ \t]*\1){2,}[ \t]*$")
_FENCE = re.compile(r"^[ \t]*(`{3,}|~{3,})")
_ITEM = re.compile(r"^([ \t]*)([-*+]|\d{1,9}[.)])(?:[ \t]+(.*))?$")
_CHECKBOX = re.compile(r"^\[[ xX]\][ \t]+")
_TICKED = re.compile(r"`([^`\n]+)`")
_BARE = re.compile(r"(?<![A-Za-z0-9_./:@~-])(?:/|\.{1,2}/)?\.?[A-Za-z0-9_][A-Za-z0-9_.-]*(?:/[A-Za-z0-9_.-]*)*")
_PATH = re.compile(r"(?:/|\.{1,2}/)?\.?[A-Za-z0-9_][A-Za-z0-9_.-]*(?:/[A-Za-z0-9_.-]*)*")
_LINE_SUFFIX = re.compile(r"(?::\d+(?:[:-]\d+)?|#L\d+(?:-L?\d+)?)$")
_EXT = re.compile(r"\.([A-Za-z][A-Za-z0-9]{0,7})$")
_ONE_LINE = re.compile(r"^[ \t]*(?:[-*+][ \t]+)?(.+?)[:：][ \t]*(.*)$")
_MOD = re.compile(r"(?<![A-Za-z0-9_:/-])mod:([A-Za-z0-9](?:[A-Za-z0-9_.-]{0,62}[A-Za-z0-9_])?)")


@dataclass(frozen=True)
class Finding:
    text: str  # the item's first paragraph, markup kept, whitespace collapsed
    details: str  # nested items and paragraphs under it, one per line
    paths: tuple[str, ...]  # repository-relative paths it names, the item's own text first

    @property
    def file(self) -> str | None:
        return self.paths[0] if self.paths else None

    @property
    def key(self) -> str:
        return finding_hash(self.file, self.text)

    @property
    def modules(self) -> tuple[str, ...]:
        """mod:<name> tags the item names itself (the tag lead and workers already use for a module)."""
        return tuple(dict.fromkeys(MOD_TAG + m.group(1).lower() for m in _MOD.finditer(self.text)))

    @property
    def statement(self) -> str:
        return self.text if len(self.text) <= STATEMENT_MAX else self.text[:STATEMENT_MAX - 1] + "…"


def _label(s: str) -> str:
    """A heading's text as compared with the configured one: "**Out of scope:**", "Out of scope:" and "out of
    scope" are the same heading."""
    s = unicodedata.normalize("NFKC", s).strip()
    for _ in range(2):  # "**Out of scope:**" and "**Out of scope**:"
        s = s.rstrip(":").strip()
        for mark in ("**", "__", "*", "_"):
            if len(s) > 2 * len(mark) and s.startswith(mark) and s.endswith(mark):
                s = s[len(mark):-len(mark)].strip()
                break
    return " ".join(s.rstrip(":").casefold().split())


def normalize_finding(text: str) -> str:
    """The sentence half of the dedup key: inline code and bold markers dropped, then the statement normalisation
    the ledger uses for every claim (NFKC, lower case, whitespace collapsed, punctuation at both ends dropped)."""
    return normalize_statement(text.replace("`", "").replace("**", ""))


def finding_hash(file: str | None, text: str) -> str:
    """Same file (or none) and same normalised sentence: same finding, whoever reported it."""
    raw = FINDING_DOMAIN + (file or "").encode("utf-8") + b"\0" + normalize_finding(text).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _indent(line: str) -> int:
    return len(line[:len(line) - len(line.lstrip(" \t"))].expandtabs(4))


def parse(report: str, heading: str, root: str | None = None) -> list[Finding]:
    """The findings under every section titled heading, in report order, one per distinct key. root: the
    directory the subagent worked in, to turn absolute paths under it into repository-relative ones."""
    want = _label(heading)
    if not want:
        return []
    found: list[Finding] = []
    seen: set[str] = set()
    for text, details in _items(report.splitlines(), want):
        sentence = " ".join(text.split())
        if not sentence or normalize_finding(sentence) in NOTHING:
            continue
        paths = _paths(sentence, root) or _paths(" ".join(details), root)
        f = Finding(sentence, "\n".join(details), paths)
        if f.key not in seen:
            seen.add(f.key)
            found.append(f)
        if len(found) >= MAX_FINDINGS:
            break
    return found


def _items(lines: list[str], want: str) -> list[tuple[str, list[str]]]:
    """(first paragraph, detail lines) of each top-level item in the sections titled want."""
    out: list[tuple[str, list[str]]] = []
    section: tuple[str, int] | None = None  # ("atx", level) or ("bold", 0)
    fence: str | None = None
    top: int | None = None  # indentation of the section's top-level items
    item: tuple[list[str], list[str]] | None = None  # (text lines, details)
    in_text = False  # the next indented or lazy line continues the item's first paragraph

    def close() -> None:
        nonlocal item, in_text
        if item is not None:
            out.append((" ".join(item[0]), item[1]))
        item, in_text = None, False

    for line in lines:
        opening = _FENCE.match(line) if fence is None else None
        if fence is not None or opening is not None:
            if opening is not None:
                fence = opening.group(1)[0] * 3
            elif fence is not None and line.strip().startswith(fence):
                fence = None
            if section is not None and item is not None:
                item[1].append(line.strip())
                in_text = False
            continue
        atx = _ATX.match(line)
        bold = None if atx else _BOLD.match(line)
        if section is not None and (
                _BREAK.match(line)
                or (atx is not None and (section[0] == "bold" or len(atx.group(1)) <= section[1]))
                or (bold is not None and section[0] == "bold")):
            close()
            section, top = None, None
        if section is None:
            if atx is not None and _label(atx.group(2) or "") == want:
                section = ("atx", len(atx.group(1)))
            elif bold is not None and _label(bold.group(2)) == want:
                section = ("bold", 0)
            elif atx is None and (one := _ONE_LINE.match(line.replace("**", "").replace("__", ""))) \
                    and _label(one.group(1)) == want and one.group(2).strip():
                out.append((one.group(2).strip(), []))
            continue
        if atx is not None or bold is not None:  # a sub-heading or a bold group label inside the section
            close()
            continue
        if not line.strip():
            in_text = False
            continue
        if m := _ITEM.match(line):
            ind = _indent(line)
            body = _CHECKBOX.sub("", (m.group(3) or "").strip())
            if top is None or ind <= top:
                close()
                top = ind if top is None else min(top, ind)
                item, in_text = ([body], []), True
            elif item is not None:
                item[1].append(f"{m.group(2)} {body}".rstrip())
                in_text = False
            continue
        if item is None:
            continue
        if in_text:
            item[0].append(line.strip())
        elif top is not None and _indent(line) > top:
            item[1].append(line.strip())
        else:
            close()  # a paragraph after the list
    close()
    return out


def _paths(text: str, root: str | None) -> tuple[str, ...]:
    """Repository-relative paths named in text, in order (docs/code.md defines what counts as one)."""
    out: list[str] = []
    ticked = [(m.group(1).strip(), True) for m in _TICKED.finditer(text)]
    bare = [(m.group(0), False) for m in _BARE.finditer(_TICKED.sub(" ", text))]
    for token, in_ticks in ticked + bare:
        p = _path(token, in_ticks, root)
        if p is not None and p not in out:
            out.append(p)
            if len(out) >= MAX_PATHS:
                break
    return tuple(out)


def _path(token: str, in_ticks: bool, root: str | None) -> str | None:
    token = _LINE_SUFFIX.sub("", token.strip()).rstrip(".,;")
    if not token or not _PATH.fullmatch(token):
        return None
    last = token.rstrip("/").rsplit("/", 1)[-1]
    ext = _EXT.search(last)
    if "/" in token:
        if not (in_ticks or token.endswith("/") or ext):
            return None  # "and/or", "read/write"
    elif not (ext and ext.group(1).lower() in KNOWN_EXT and len(last) > len(ext.group(0))):
        return None
    if token.startswith("/"):
        base = (root or "").rstrip("/")
        if not base or not token.startswith(base + "/"):
            return None  # outside the checkout the subagent worked in: not a repository path
        token = token[len(base) + 1:]
    parts = [p for p in PurePosixPath(token).parts if p not in (".", "", "/")]
    if not parts or ".." in parts:
        return None
    return "/".join(parts) + ("/" if token.endswith("/") else "")


@dataclass
class Captured:
    findings: int = 0  # distinct findings in the report
    new: int = 0  # of those, claims this report created (the others got the report as evidence)
    claims: tuple[str, ...] = ()


def capture(lg: Ledger, report: str, heading: str, *, source: dict[str, Any], piece: str | None = None,
            root: str | None = None) -> Captured:
    """Record the report's findings: one observation claim per distinct finding, with the report itself as a
    source that supports each of them. A finding already in the ledger (same key) gets the report as evidence
    instead of a second claim, so the out-of-scope claims follow the distinct findings, not the reports.
    Idempotent: a retry of the same report finds its source, its claims and its links again."""
    found = parse(report, heading, root)
    if not found:
        return Captured()
    sid = _report_source(lg, report, source)
    out = Captured(findings=len(found))
    ids: list[str] = []
    for f in found:
        payload: dict[str, Any] = {"text": f.text[:TEXT_MAX], "heading": heading, "source": source}
        if f.details:
            payload["details"] = f.details[:TEXT_MAX]
        if f.file:
            payload["file"] = f.file
        if piece:
            payload["piece"] = piece
        tags = [FINDING_TAG, *([PIECE_TAG + piece] if piece else []), *f.modules, *(FILE_TAG + p for p in f.paths)]
        r = lg._propose_claim(CLAIM_KIND, f.statement, payload=payload, tags=tags,
                              evidence=[(f"s:{sid}", "supports")], _dedup_hash=f.key)
        out.new += r.duplicate_of is None
        ids.append(f"c:{str(r.id)[:8]}")
    out.claims = tuple(ids)
    return out


def _report_source(lg: Ledger, report: str, source: dict[str, Any]) -> Any:
    """The report as a source row, keyed by its digest like the auto-captured note: the same text arriving through
    a second hook event, or a retry, finds the row again instead of counting as another report."""
    digest = hashlib.sha256(report.encode("utf-8")).hexdigest()
    uri = REPORT_URI + digest
    rows = lg.sql_internal("SELECT id FROM benchbook.sources WHERE project_id = %s AND uri = %s"
                           " ORDER BY created_at LIMIT 1", (lg.project_id, uri))
    if rows:
        return rows[0]["id"]
    who = ", ".join(f"{k} {source[k]}" for k in ("agent_type", "agent_id", "session_id") if source.get(k))
    return lg.add_source(uri, title=f"subagent final report ({who or 'unknown subagent'})"[:300], sha256=digest)
