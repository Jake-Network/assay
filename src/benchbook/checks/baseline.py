"""The merge-base as a control: run its checks to tell a failure the commit introduced from one that was already there,
count the tests on both sides, and find passing runs of the piece elsewhere."""
from __future__ import annotations

import json
import threading
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..config import CheckSpec
from . import rerun, signature
from .declarations import Base
from .guard import Integrity
from .rerun import MAX_FAILED_SHOWN, CheckResult, Prepared, _signals_held
from .runner import Interrupted, forget_stopped, stop_commands

if TYPE_CHECKING:
    from ..core.ledger import Ledger

# ---------------------------------------------------------------------------------------------------- baseline (T03)

@dataclass
class BaseRun:
    """One check's result at the merge-base."""
    status: str
    signature: str | None
    failed: list[str]
    tests: int | None


def baseline(repo: Path, b: Base, specs: Sequence[CheckSpec],
             env: dict[str, str] | None = None) -> tuple[dict[str, BaseRun], BaseException | None]:
    """Run the reference checks now, using the same environment as the candidate."""
    prepared = Prepared()
    results, interrupted = rerun.run_checks(repo, b.merge_base, specs, env, b.prep, prepared)
    if interrupted is not None or prepared.error is not None:
        return {}, interrupted
    return {r.name: BaseRun(r.status, r.signature, [t for t, _ in r.failed_tests], r.tests)
            for r in results}, None


class EarlyBaseline:
    """[verify] parallel_baseline: count_tests's merge-base run, started in a thread beside the commit's checks. Its
    result is used only when count_tests would have run exactly these checks; otherwise it runs them as before."""

    def __init__(self, repo: Path, b: Base, specs: list[CheckSpec], env: dict[str, str] | None) -> None:
        self.names = [s.name for s in specs]
        self._box: dict[str, Any] = {}
        # Set when the run (and its worktree's removal) is over. Waiting on it, not on Thread.join or is_alive: before
        # Python 3.12 a signal that interrupts join can leave is_alive() False while the thread still runs.
        self._done = threading.Event()
        self._thread = threading.Thread(target=self._run, args=(repo, b, specs, env), daemon=True,
                                        name="benchbook-merge-base")
        self._thread.start()

    def _run(self, repo: Path, b: Base, specs: list[CheckSpec], env: dict[str, str] | None) -> None:
        try:
            self._box["value"] = baseline(repo, b, specs, env)
        except BaseException as e:  # noqa: BLE001 - handed to the caller
            self._box["error"] = e
        finally:
            self._done.set()

    def result(self) -> tuple[dict[str, BaseRun], BaseException | None]:
        """The run's result; an interruption while waiting for it (verify traps SIGTERM and SIGHUP while the run
        lives) stops the run and comes back as the run's interruption, as a sequential baseline's would."""
        try:
            while not self._done.wait(0.1):
                pass
        except (Interrupted, KeyboardInterrupt) as e:
            with _signals_held():
                self.stop()
            return {}, e
        if "error" in self._box:
            raise self._box["error"]
        return self._box["value"]  # type: ignore[no-any-return]

    def stop(self) -> None:
        """End the run if it is still going (its check's process group is killed) and wait for its cleanup."""
        ident = self._thread.ident
        if ident is not None and not self._done.is_set():
            stop_commands(ident)
            self._done.wait()
            forget_stopped(ident)


def count_tests(repo: Path, b: Base, results: list[CheckResult], integ: Integrity,
                env: dict[str, str] | None, early: EarlyBaseline | None = None) -> BaseException | None:
    """T04: the JUnit testcases that ran here and at the merge-base. Each JUnit check that reported a count is
    compared with its own merge-base run (integ.fewer_tests_in names those that ran fewer); tests_before and
    tests_after are the sums, for the record. Run only when guarded files changed; the merge-base runs come from
    the current environment. Returns an interruption of that run."""
    counted = [r for r in results if b.checks[r.name].junit and r.tests is not None]
    if not counted:
        return None
    if early is not None and early.names == [r.name for r in counted]:
        runs, interrupted = early.result()
    else:
        runs, interrupted = baseline(repo, b, [b.checks[r.name] for r in counted], env)
    if interrupted is not None:
        return interrupted
    before = after = 0
    seen = False
    fewer = []
    for r in counted:
        was = runs[r.name].tests if r.name in runs else None
        if isinstance(was, int) and r.tests is not None:
            before, after, seen = before + was, after + r.tests, True
            if r.tests < was:
                fewer.append(r.name)
    if seen:
        integ.tests_before, integ.tests_after, integ.fewer_tests_in = before, after, fewer
    return None


def same_failure(r: CheckResult, base: BaseRun) -> bool:
    """Whether a failing check fails the same way at the merge-base: an equal signature element, or (JUnit) every
    test failing here fails there too."""
    if base.status not in ("fail", "timeout"):
        return False
    if r.signature and base.signature and r.signature == base.signature:
        return True
    mine = {t for t, _ in r.failed_tests}
    return bool(mine) and len(r.failed_tests) < MAX_FAILED_SHOWN and mine <= set(base.failed)


# The checks an implementer's own clean run of PIECE at COMMIT passed (check run, the Bash hook: metrics.via): run
# by one of the piece's implementers (the attempt's author, or metrics.run_by, the agent type the Bash hook saw) and
# of the definition verify runs (metrics.check_definitions, CheckSpec.definition). A run without either says nothing
# about this check. A raw Bash pass of a JUnit check proves nothing (docs/claude-code.md) and is left out, as the
# completion gate does.
_PASSED_ELSEWHERE = (
    "SELECT DISTINCT c.key FROM benchbook.experiments_valid e,"
    " jsonb_each_text(CASE WHEN jsonb_typeof(e.metrics -> 'checks') = 'object' THEN e.metrics -> 'checks'"
    " ELSE '{}'::jsonb END) AS c(key, value)"
    " WHERE e.project_id = %s AND e.config ->> 'piece' = %s AND e.code_ref = %s AND e.run_status = 'ok'"
    " AND e.metrics ->> 'via' IS NOT NULL AND e.metrics -> 'worktree_dirty' IS DISTINCT FROM 'true'::jsonb"
    " AND c.value = 'ok' AND (c.key <> ALL(%s::text[]) OR e.metrics ->> 'via' IS DISTINCT FROM 'bash-hook')"
    " AND (e.author = ANY(%s::text[]) OR e.metrics ->> 'run_by' = ANY(%s::text[]))"
    " AND jsonb_typeof(e.metrics -> 'check_definitions') = 'object'"
    " AND e.metrics -> 'check_definitions' ->> c.key = %s::jsonb ->> c.key")


def passed_elsewhere(lg: Ledger, piece: str, commit: str, b: Base, implementers: Collection[str]) -> set[str]:
    """The checks of b that an implementer's own clean run of piece at commit passed, under b's definition."""
    if not implementers:
        return set()
    junit = [n for n, s in b.checks.items() if s.junit]
    who = sorted(implementers)
    defs = json.dumps({n: s.definition for n, s in b.checks.items()})
    return {str(r["key"]) for r in lg.sql_internal(_PASSED_ELSEWHERE, (lg.project_id, piece, commit, junit, who, who,
                                                                       defs))}


def classify_environment(repo: Path, b: Base, results: list[CheckResult], env: dict[str, str] | None,
                         elsewhere: set[str]) -> tuple[str | None, str | None, BaseException | None]:
    """T03: a failing check is an environment failure when the implementer's own clean run of this commit passed it
    (elsewhere) and it fails the same way at the merge-base, which is then re-run. A check that fails at the
    merge-base by design (the piece's goal) and was never seen passing stays the piece's failure. Returns (the
    reason when every failing check is an environment failure, the merge-base's signature of those checks, an
    interruption of the baseline run)."""
    failing = [r for r in results if r.status in ("fail", "timeout")]
    if not failing or not all(r.name in elsewhere for r in failing):
        return None, None, None
    runs, interrupted = baseline(repo, b, [b.checks[r.name] for r in failing], env)
    for r in failing:
        r.environment = r.name in runs and same_failure(r, runs[r.name])
    if interrupted is not None or not all(r.environment for r in failing):
        return None, None, interrupted
    names = ", ".join(r.name for r in failing)
    sig = signature.combine((r.name, runs[r.name].signature) for r in failing)
    return (f"{names} {'fails' if len(failing) == 1 else 'fail'} the same way at the merge-base "
            f"{b.merge_base[:12]}", sig, None)
