"""The hook spool: a hook writes its input to a file in the spool of its project and writer and returns; a detached
processor (`python -m benchbook.hooks`, start_processor) drains the spool into the ledger, one lane at a time
(records, verify), retrying what failed for a passing reason and moving what failed for good to `dead/`.
`benchbook hook replay` drains it in the foreground and adopts the entries a writer's earlier token left."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import traceback
from collections.abc import Collection, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from .. import config
from ..config import env

RETRY_DELAYS = (1, 2, 4, 8, 15, 30)
# Spool entries that are check attempts (benchbook check run, the Bash hook); attempts.record_spooled records them.
ATTEMPT_EVENTS = frozenset({"check-run", "bash-result"})
SPOOL_IDENTITY_DOMAIN = b"benchbook:spool-writer\0"
WRITER_INFO = "writer.info"  # in a token's spool: its target and writer, and the agent the ledger said it is
ADOPTED_PREFIX = "adopted-"  # entries moved from the spool of the agent's earlier token
ADOPTED_DONE = "adopted.done"  # ids of adopted entries already processed (_adopt never copies them again)


def _state_dir() -> Path:
    return config.state_dir()


def log(msg: str) -> None:
    try:
        d = _state_dir()
        d.mkdir(parents=True, exist_ok=True)
        with open(d / "hook.log", "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {msg}\n")
    except OSError:
        pass


def writer_identity() -> str | None:
    """Who an entry is written as when it reaches the ledger: under BENCHBOOK_TOKEN the token's agent, so a digest
    of the token (never the token itself); without one the owner connection, None. Two agents that share a database
    URL and a state directory but not a token must never record each other's entries: the token, not the process
    that happens to drain the spool, decides the author."""
    token = env("TOKEN")
    if not token:
        return None
    return hashlib.sha256(SPOOL_IDENTITY_DOMAIN + token.encode()).hexdigest()[:32]


def _target_parts() -> list[Any]:
    url = env("DATABASE_URL")
    data_dir = env("DATA_DIR")
    xdg_dir = os.environ.get("XDG_DATA_HOME")
    embedded = [
        str(Path(d).expanduser().resolve()) if d else None
        for d in (data_dir, xdg_dir)
    ] if not url else []
    return [url, *embedded]


def _target_key() -> str:
    """The database target alone: the spool key without a token."""
    return hashlib.sha256(json.dumps(_target_parts()).encode()).hexdigest()


def _spool_dir() -> Path:
    # Different database targets must never pick up one another's pending writes, nor different tokens on one target
    # (writer_identity). The key is built from the values, not the variable names, so entries queued under ASSAY_*
    # variables are found; without a token it is the key spools had before tokens.
    return _state_dir() / "spool" / _spool_name(writer_identity())


def _spool_name(writer: str | None) -> str:
    parts = _target_parts()
    if writer is not None:
        parts.append({"writer": writer})
    return hashlib.sha256(json.dumps(parts).encode()).hexdigest()


def _read_info(path: Path) -> dict[str, Any] | None:
    try:
        info = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if (not isinstance(info, dict) or not isinstance(info.get("target"), str) or not isinstance(info.get("writer"), str)
            or not isinstance(info.get("agents"), dict)):
        return None
    return info


def remember_writer(project: str, agent: str) -> None:
    """Note, in this token's spool, which agent the ledger said the token is in project (WRITER_INFO). Called once a
    ledger has accepted the token. After the token is rotated, `hook replay` under the new token adopts the entries
    queued under the old one when they are the same agent's (_adopt); the old token no longer authenticates, so
    this note, written while it did, is what says whose they are. Best effort: a failure here loses nothing queued."""
    writer = writer_identity()
    if writer is None:
        return
    try:
        spool = _spool_dir()
        path = spool / WRITER_INFO
        info = _read_info(path) or {"target": _target_key(), "writer": writer, "agents": {}}
        if info["writer"] != writer or info["agents"].get(project) == agent:
            return
        info["agents"][project] = agent
        spool.mkdir(parents=True, exist_ok=True, mode=0o700)
        _write_info(path, info)
    except OSError:
        pass


@dataclass
class ReplaySummary:
    """What one drain of the spool did. `errors` has one line per entry left queued or moved to dead/."""
    captured: int = 0
    cited: int = 0
    recorded: int = 0  # check attempts
    adopted: int = 0  # entries moved here from the spool of this agent's earlier token
    held: int = 0  # entries under another token on this target that cannot be attributed to an agent
    skipped: int = 0
    dead: int = 0
    queued: int = 0
    requeued: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def processed(self) -> int:
        return self.captured + self.cited + self.recorded + self.skipped

    @property
    def ok(self) -> bool:
        return not self.dead and not self.queued and not self.held

    def add(self, path: Path, outcome: str) -> None:
        kind, _, detail = outcome.partition(":")
        if kind == "ok":
            res = detail.split(":", 1)[0]
            if res == "captured":
                self.captured += 1
            elif res == "cited":
                self.cited += 1
            elif res == "recorded":
                self.recorded += 1
            else:
                self.skipped += 1
        elif kind == "dead":
            self.dead += 1
            location = "quarantined as .invalid" if detail.startswith("invalid spool entry") else "moved to dead/"
            self.errors.append(f"{path.name}: {detail} ({location})")
        else:
            self.queued += 1
            self.errors.append(f"{path.name}: {detail} (still queued)")

    def as_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "processed": self.processed, "captured": self.captured, "cited": self.cited,
                "recorded": self.recorded, "skipped": self.skipped, "dead": self.dead, "queued": self.queued,
                "requeued": self.requeued, "adopted": self.adopted, "held": self.held, "errors": self.errors}

    def __str__(self) -> str:
        head = (f"processed {self.processed} (captured {self.captured}, cited {self.cited}, recorded {self.recorded}, "
                f"skipped {self.skipped});"
                f" dead {self.dead}; still queued {self.queued}"
                + (f"; adopted {self.adopted} from this agent's earlier token" if self.adopted else "")
                + (f"; held {self.held} queued under a token no processor confirmed" if self.held else ""))
        return "\n".join([head, *self.errors])


# A spool has two lanes, each drained by its own detached processor under its own lock: re-runs (verify-request,
# tens of seconds each) and everything else (reports, check runs, Bash hook attempts, a few milliseconds each), so
# a worker's attempt is not recorded only after every queued re-run. Before each re-run the verify lane settles the
# records lane, so whatever was queued before a request is in the ledger when it runs, as with one processor.
RECORDS, VERIFY = "records", "verify"
LANES = (RECORDS, VERIFY)
_LOCK_FILES = {RECORDS: ".lock", VERIFY: ".verify.lock"}


def start_processor(spool: Path, lane: str = RECORDS) -> None:
    """Start the detached processor for this spool's lane unless one is draining it already (check run and the Bash
    hook queue attempts in the records lane, the agent-result hook re-runs in the verify lane)."""
    if not _locked(spool, lane):
        subprocess.Popen([sys.executable, "-m", "benchbook.hooks", str(spool), lane], stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True)


@contextmanager
def _lock(spool: Path, blocking: bool, lane: str = RECORDS) -> Iterator[bool]:
    import fcntl

    with (spool / _LOCK_FILES[lane]).open("a") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def _locked(spool: Path, lane: str = RECORDS) -> bool:
    """Whether a processor is draining this lane of the spool now. It rescans after releasing the lock, so it sees an
    entry written before this check."""
    with _lock(spool, blocking=False, lane=lane) as got:
        return not got


@contextmanager
def _all_lanes(spool: Path, blocking: bool) -> Iterator[bool]:
    """Both lanes' locks, the verify lane's first (the order the verify lane takes them in, so they never deadlock).
    Without blocking, False when either processor is running."""
    with _lock(spool, blocking, VERIFY) as got_verify, _lock(spool, blocking, RECORDS) as got_records:
        yield got_verify and got_records


def _lane(path: Path) -> str:
    """The lane an entry belongs to; one that cannot be read goes to the records lane, which quarantines it."""
    try:
        return VERIFY if json.loads(path.read_text(encoding="utf-8")).get("event") == "verify-request" else RECORDS
    except (OSError, ValueError, AttributeError):
        return RECORDS


def _write_spool(spool: Path, entry: dict[str, Any], name: str | None = None) -> None:
    """Queue one entry, bound to the identity that queued it (writer_identity): only a processor or replay under the
    same identity records it. name: a fixed file name (an adopted entry's); otherwise a new one. Under a token the
    spool also gets its WRITER_INFO (target and writer, no agent yet), so a replay under a later token can tell
    whose database this queue is for even if the ledger was never reached."""
    writer = writer_identity()
    entry = {**entry, "writer": writer, "receipt_id": entry.get("receipt_id") or uuid4().hex}
    if writer is not None and not (spool / WRITER_INFO).exists():
        try:
            _write_info(spool / WRITER_INFO, {"target": _target_key(), "writer": writer, "agents": {}}, create=True)
        except OSError as e:
            log(f"could not describe hook spool {spool}: {e}")
    with tempfile.NamedTemporaryFile(mode="w", dir=spool, encoding="utf-8", delete=False) as f:
        path = Path(f.name)
        try:
            json.dump(entry, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
            path.replace(spool / name if name else path.with_suffix(".json"))
        finally:
            path.unlink(missing_ok=True)


def _write_info(path: Path, info: dict[str, Any], *, create: bool = False) -> None:
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, encoding="utf-8", delete=False) as f:
        tmp = Path(f.name)
        try:
            json.dump(info, f, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
            if create:
                try:
                    os.link(tmp, path)
                except FileExistsError:
                    pass  # a processor may already have confirmed the agent
            else:
                tmp.replace(path)
        finally:
            tmp.unlink(missing_ok=True)


# ---------------------------------------------------------------------- the spool


def _permanent(e: BaseException) -> bool:
    """Errors a retry cannot fix: a ledger rule (unknown project, schema newer than this benchbook), or data and
    SQL errors. Connection trouble and OS errors are transient."""
    import psycopg

    from ..errors import BenchbookError

    if isinstance(e, BenchbookError):
        return True
    if isinstance(e, (psycopg.OperationalError, psycopg.InterfaceError, OSError, TimeoutError)):
        return False
    return isinstance(e, (psycopg.Error, ValueError, TypeError, KeyError))


def _bury(path: Path, reason: str) -> None:
    dead = path.parent / "dead"
    dead.mkdir(exist_ok=True, mode=0o700)
    path.replace(dead / path.name)
    log(f"hook entry {path.name} moved to {dead}: {reason}")


def _entry_receipt(path: Path, entry: dict[str, Any]) -> UUID:
    if (receipt := entry.get("receipt_id")) is not None:
        if not isinstance(receipt, str):
            raise ValueError("invalid hook receipt")
        return UUID(receipt)
    if path.name.startswith(ADOPTED_PREFIX):
        return UUID(path.stem.removeprefix(ADOPTED_PREFIX))
    # Older entries have only a file name. Preserve the id earlier versions used when moving that file.
    spool = path.parent.parent if path.parent.name == "dead" else path.parent
    return UUID(hex=hashlib.sha256(f"{spool.name}/{path.name}".encode()).hexdigest()[:32])


def process_file(path: Path) -> str:
    """'ok:<process result>', 'dead:<reason>' (moved to dead/ or quarantined), 'retry:<reason>' (left queued) or
    'held:<reason>' (left queued, not retried: another identity queued it)."""
    from . import subagent  # the reports in the spool are its to record; it imports this module at load

    try:
        spooled = json.loads(path.read_text(encoding="utf-8"))
        if "data" in spooled:
            data = spooled["data"]
        else:
            # Older spools kept raw stdin and inferred the project from the working directory.
            data = json.loads(spooled["input"] or "{}")
        event = spooled["event"]
        if not isinstance(data, dict) or not isinstance(event, str):
            raise ValueError("invalid hook spool entry")
        accepted = spooled.get("accepted") is True
        agent_type = subagent._agent_type(data)
        writer = spooled.get("writer")  # absent: queued before entries were bound, when there were no tokens
        if writer is not None and not isinstance(writer, str):
            raise ValueError("invalid writer in hook spool entry")
    except OSError as e:
        log(f"cannot read hook spool {path} (queued to retry): " + traceback.format_exc())
        return f"retry:{type(e).__name__}: {e}"
    except (KeyError, TypeError, ValueError, AttributeError) as e:
        try:
            path.rename(path.with_suffix(".invalid"))
        except OSError:
            return f"retry:could not quarantine invalid spool entry ({e})"
        log(f"invalid hook spool {path} (quarantined): " + traceback.format_exc())
        return f"dead:invalid spool entry ({e})"
    if writer != writer_identity():
        # Queued under another token (or with none): recording it now would name this process's agent as the
        # author of another agent's report or run. It stays queued for a replay under the identity that queued it.
        return ("held:queued under " + ("another BENCHBOOK_TOKEN" if writer else "no BENCHBOOK_TOKEN")
                + "; replay it with the environment it was queued in (after a token rotation, `benchbook hook "
                "replay` with the agent's new token adopts it)")
    adopted_id = path.stem.removeprefix(ADOPTED_PREFIX) if path.name.startswith(ADOPTED_PREFIX) else None
    if adopted_id is not None and adopted_id in _adopted_done(path.parent):
        try:
            path.unlink(missing_ok=True)
        except OSError as e:
            return f"retry:recorded, but could not remove {path.name}: {e}"
        return "ok:skipped:already recorded"
    if not accepted and not subagent.is_worker(agent_type, require_type=False):
        # Queued by a build that did not record its decision: the BENCHBOOK_HOOK_AGENTS that accepted it may differ
        # from the current one. Keep it rather than drop it, so a replay with the original setting captures it.
        reason = (f"agent type {agent_type!r} is not in the current BENCHBOOK_HOOK_AGENTS and the entry predates "
                  "the recorded capture decision; to capture it run: BENCHBOOK_HOOK_AGENTS=<the setting it was queued "
                  "under> benchbook hook replay")
        try:
            _bury(path, reason)
        except OSError:
            return f"retry:{reason}"
        return f"dead:{reason}"
    try:
        if event in ATTEMPT_EVENTS:  # a check run (check run, the Bash hook): an attempt, not a report
            from ..checks.attempts import record_spooled

            result = record_spooled(event, data, spooled["project"], _entry_receipt(path, spooled))
        elif event == "verify-request":
            from ..checks.verify_requests import process as verify_request

            spool = path.parent.parent if path.parent.name == "dead" else path.parent
            result = verify_request(data, spooled["project"], _entry_receipt(path, spooled), spool)
        else:
            heading = spooled.get("out_of_scope_heading")
            result = subagent.process(event, data, spooled.get("cwd"), project=spooled.get("project"),
                             fired_at=spooled.get("spooled_at"), accepted=accepted,
                             out_of_scope_heading=heading if isinstance(heading, str) else None,
                             findings_only=spooled.get("findings_only") is True)
        log(f"{event}: {result}")
        if adopted_id is not None:  # before the removal: _adopt must never copy it again
            with (path.parent / ADOPTED_DONE).open("a", encoding="utf-8") as f:
                f.write(adopted_id + "\n")
                f.flush()
                os.fsync(f.fileno())
        path.unlink(missing_ok=True)
        if result.startswith(("captured", "recorded")) and isinstance(project := spooled.get("project"), str):
            from ..code.status import refresh_cache

            refresh_cache(path.parent.parent if path.parent.name == "dead" else path.parent, project)
    except Exception as e:  # noqa: BLE001
        reason = f"{type(e).__name__}: {str(e).splitlines()[0] if str(e) else ''}"
        if _permanent(e):
            log(f"hook processing failed permanently for {path}: " + traceback.format_exc())
            try:
                _bury(path, reason)
            except OSError:
                return f"retry:{reason}"
            return f"dead:{reason}"
        log(f"hook processing failed for {path} (queued to retry): " + traceback.format_exc())
        return f"retry:{reason}"
    return f"ok:{result}"


def _pending(spool: Path, lane: str | None = None) -> list[Path]:
    """The queued entries, of one lane or of both."""
    # Handbacks first: they mark their run, so its SubagentStop closing text is not captured as well.
    def key(p: Path) -> tuple[int, float]:
        try:
            first = '"tool_name": "SubagentHandback"' in p.read_text(encoding="utf-8")
            return (0 if first else 1, p.stat().st_mtime)
        except (OSError, UnicodeError):
            return (1, 0.0)

    paths = spool.glob("*.json")
    return sorted(paths if lane is None else (p for p in paths if _lane(p) == lane), key=key)


def _settle_records(spool: Path) -> None:
    """Record what waits in the records lane (one pass, under its lock) before a re-run starts. An entry written
    while the lock was held found it held and started no processor: one is started for it, as replay's rescan does,
    and the re-run goes on (that entry came after the request)."""
    with _lock(spool, blocking=True, lane=RECORDS):
        seen = _drain(spool, (), ReplaySummary(), lane=RECORDS)
    if any(p.name not in seen for p in _pending(spool, RECORDS)):
        start_processor(spool, RECORDS)


def _start_waiting_lanes(spool: Path, lane: str) -> None:
    """Start the other lane's processor when entries wait there with none running: a re-run left by a stopped
    processor or an older version, or a retry, would otherwise wait for the next request of its own kind (one
    processor used to take everything whenever any hook started it)."""
    other = VERIFY if lane == RECORDS else RECORDS
    if _pending(spool, other) and not _locked(spool, other):
        start_processor(spool, other)


def _drain(spool: Path, delays: tuple[float, ...], summary: ReplaySummary,
           attempted: Collection[str] = (), lane: str | None = None) -> set[str]:
    seen: set[str] = set()
    left: list[tuple[Path, str]] = []
    for delay in (0, *delays):
        if delay:
            time.sleep(delay)
        left = []  # the last pass decides what is still queued
        for path in _pending(spool, lane):
            if path.name in attempted:
                continue
            seen.add(path.name)
            if lane == VERIFY:
                _settle_records(spool)
            try:
                outcome = process_file(path)
            except Exception as e:  # noqa: BLE001 (one entry must not stop the others)
                log(f"hook entry {path} could not be processed (queued to retry): " + traceback.format_exc())
                outcome = f"retry:{type(e).__name__}: {e}"
            if outcome.startswith("retry:"):
                left.append((path, outcome))
            else:
                summary.add(path, outcome)
        if not left:
            break
    for path, outcome in left:
        summary.add(path, outcome)
    return seen


def replay(spool: Path, lane: str = RECORDS) -> None:
    """Background processor of one lane: drain with retries, unless another processor already holds that lane."""
    _start_waiting_lanes(spool, lane)
    while True:
        with _lock(spool, blocking=False, lane=lane) as got:
            if not got:
                return
            seen = _drain(spool, RETRY_DELAYS, ReplaySummary(), lane=lane)
        # an entry written while the lock was held got no processor of its own
        if not any(p.name not in seen for p in _pending(spool, lane)):
            return


def replay_now(spool: Path) -> ReplaySummary:
    """`benchbook hook replay`: try each pending or dead report once in the foreground."""
    dead = spool / "dead"
    summary = ReplaySummary()
    with _all_lanes(spool, blocking=True):
        _adopt(spool, summary)
        if dead.is_dir():
            for p in dead.glob("*.json"):
                p.replace(spool / p.name)
                summary.requeued += 1
        attempted = _drain(spool, (), summary)
    # A hook that enqueued after the snapshot may have seen our lock and skipped starting a processor.
    while any(p.name not in attempted for p in spool.glob("*.json")):
        with _all_lanes(spool, blocking=True):
            attempted.update(_drain(spool, (), summary, attempted))
    log(f"replay {spool}: {summary}")  # doctor reads which spool a clean replay settled
    return summary


def _adopted_done(spool: Path) -> set[str]:
    try:
        return set((spool / ADOPTED_DONE).read_text(encoding="utf-8").split())
    except OSError:
        return set()


def _copy_exists(spool: Path, name: str) -> bool:
    """The adopted copy is still waiting here, in dead/, or quarantined."""
    stem = name[:-len(".json")]
    return any(p.exists() for p in (spool / name, spool / "dead" / name, spool / f"{stem}.invalid"))


def _adopt(spool: Path, summary: ReplaySummary) -> None:
    """Move here the entries queued under this agent's earlier token (rotated or replaced), so they are recorded
    under its current one. An entry is the agent's when the old spool's WRITER_INFO, written while the old token
    authenticated, names for the entry's project the same agent the ledger says the current token is. Entries of
    other agents and of other database targets stay where they are; entries whose token no processor ever
    confirmed stay too, and are counted as held, with the reason.

    The copy keeps its original receipt as its file name (adopted-<id>.json) across rotations. If the copy already
    exists or is marked as done, only the original is removed. The old spool's lock is held meanwhile, so its own
    processor does not move the same files."""
    me = writer_identity()
    if me is None or not spool.parent.is_dir():
        return
    from ..errors import InvalidAgentToken

    target = _target_key()
    current: dict[str, str | None] = {}
    unavailable: dict[str, str] = {}
    done = _adopted_done(spool)

    def agent_in(project: str) -> str | None:
        if project not in current:
            try:
                from ..core.ledger import Ledger

                lg = Ledger.open(project=project)  # the current token's agent, or an error
                current[project] = lg.agent
                remember_writer(project, lg.agent)
            except InvalidAgentToken:
                # A token belongs to one project. Other projects' queues on this DB are not ours to adopt.
                current[project] = None
            except Exception as e:  # noqa: BLE001 - not confirmed, so nothing is moved
                unavailable[project] = type(e).__name__
                current[project] = None
        return current[project]

    for old in sorted(p for p in spool.parent.iterdir() if p.is_dir() and p != spool):
        if old.name == target:  # the owner spool has no token
            continue
        info = _read_info(old / WRITER_INFO)
        if info is not None and (info["target"] != target or info["writer"] == me):
            continue
        with _all_lanes(old, blocking=False) as got:
            if not got:
                summary.errors.append(f"{old.name}: its processor is running; entries queued there under an earlier "
                                      "token were not looked at (run replay again)")
                continue
            info = _read_info(old / WRITER_INFO)
            if info is not None and (info["target"] != target or info["writer"] == me):
                continue
            dead = old / "dead"
            unconfirmed = 0
            old_done = _adopted_done(old)
            for path in [*old.glob("*.json"), *(dead.glob("*.json") if dead.is_dir() else [])]:
                try:
                    entry = json.loads(path.read_text(encoding="utf-8"))
                    project = entry["project"]
                    if not isinstance(project, str):
                        continue
                    receipt = _entry_receipt(path, entry)
                except (OSError, ValueError, KeyError, TypeError):
                    continue
                if info is None:
                    old_writer = entry.get("writer")
                    if isinstance(old_writer, str) and old_writer != me and old.name == _spool_name(old_writer):
                        unconfirmed += 1
                    continue
                if entry.get("writer") != info["writer"]:
                    continue  # not queued under the token that spool belongs to
                known = info["agents"].get(project)
                if not isinstance(known, str):
                    unconfirmed += 1
                    continue
                agent = agent_in(project)
                if project in unavailable:
                    summary.queued += 1
                if agent != known:
                    continue  # another agent's, or identity could not be checked
                mid = receipt.hex
                name = f"{ADOPTED_PREFIX}{mid}.json"
                if mid not in done and mid not in old_done and not _copy_exists(spool, name):
                    entry["receipt_id"] = mid
                    _write_spool(spool, entry, name=name)  # bound to the current token
                    summary.adopted += 1
                    log(f"replay: adopted {path.name} from {old.name} as {name} ({known} in {project})")
                try:
                    path.unlink(missing_ok=True)
                except OSError as e:
                    summary.queued += 1
                    summary.errors.append(f"{old.name}/{path.name}: adopted, but the original could not be removed "
                                          f"({e}); the next replay removes it and copies nothing")
        if unconfirmed:
            summary.held += unconfirmed
            summary.errors.append(
                f"{old.name}: {unconfirmed} entr{'y' if unconfirmed == 1 else 'ies'} queued under another token on "
                "this database, which no processor ever confirmed as an agent (the ledger was never reached with "
                f"it); held there, not recorded. If it was this agent's token before a rotation, these are its "
                f"reports: record them by hand from {old}")
    for project, reason in unavailable.items():
        summary.errors.append(f"could not confirm the current token's agent in {project} ({reason}); "
                              "earlier entries are still queued")
