"""The two declared checks of the SWE-bench measurement (bench/code/PREREGISTRATION.md): evaluate the working tree's
change to one SWE-bench Verified instance in the SWE-bench harness's Docker image.

    uv run --no-project --with swebench==5.0.2 python -I bench/code/swe_check.py --instance ID --mode visible
    uv run --no-project --with swebench==5.0.2 python -I bench/code/swe_check.py --instance ID --mode hidden --junit F

Run it from the repository under test. The change is everything in the working tree, committed or not, against the
instance's base_commit; .benchbook/ (the declarations) is left out.

visible  No test patch: the hidden check's test command on the PASS_TO_PASS tests that exist before the patch
         (instances.json: visible_eval_script, PASS_TO_PASS minus visible_excluded). The implementer may run it.
hidden   The instance's own eval script, test patch applied, graded on FAIL_TO_PASS and PASS_TO_PASS: the official
         evaluation of one instance (swebench.harness.run_evaluation.run_instance). Only the harness runs it: it
         refuses unless BENCH_SWE_HIDDEN=1, which bench/code/run.py sets for check verify alone.

Exit status: 0 every graded test passed; 1 a test failed, the change did not apply, the tests timed out or produced
no results; 3 the harness could not evaluate (no Docker, no image, swebench missing, an infrastructure failure);
2 usage. --junit writes one testcase per graded test, FAIL_TO_PASS first, so the failure signature of check verify
names the first failing test; a harness-level problem is one testcase of class "harness".

-I keeps the repository under test off sys.path (requests has a package named like one of the harness's
dependencies). Only the standard library is imported at module level: docker and swebench are imported by the
function that needs them, and --backend local (the stand-in of bench.code.run --dry) needs neither."""
from __future__ import annotations

import argparse
import io
import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
INSTANCES = HERE / "instances.json"
SWEBENCH = "swebench==5.0.2"  # the harness version whose interfaces this file uses
HIDDEN_ENV = "BENCH_SWE_HIDDEN"
EXCLUDE = ":(exclude).benchbook"
EXIT_PASS, EXIT_FAIL, EXIT_USAGE, EXIT_HARNESS = 0, 1, 2, 3
F2P, P2P = "FAIL_TO_PASS", "PASS_TO_PASS"
PASSING = {"PASSED", "XFAIL"}
MAX_LISTED = 20


@dataclass
class Graded:
    """What one evaluation found: per test (kind, id, status), or a problem that kept the tests from being graded."""
    mode: str
    tests: list[tuple[str, str, str]] = field(default_factory=list)
    problem: str | None = None  # PatchApplyFailed | TestsTimedOut | NoTestOutput: the change's fault, a failure
    harness_error: str | None = None  # the harness could not evaluate: not a result about the change
    detail: str = ""

    def failing(self) -> list[tuple[str, str, str]]:
        return [t for t in self.tests if not passes(*t)]

    @property
    def exit_code(self) -> int:
        if self.harness_error:
            return EXIT_HARNESS
        return EXIT_FAIL if self.problem or self.failing() or not self.tests else EXIT_PASS


def passes(kind: str, _test: str, status: str) -> bool:
    """The harness's grading: a FAIL_TO_PASS test must pass; a PASS_TO_PASS test may also be skipped."""
    return status in PASSING or (kind == P2P and status == "SKIPPED")


# ------------------------------------------------------------------------------------------- inputs

def load_instance(instance_id: str, path: Path = INSTANCES) -> dict[str, Any]:
    doc = json.loads(path.read_text(encoding="utf-8"))
    for inst in [*doc.get("instances", []), *doc.get("reserve", [])]:
        if inst["instance_id"] == instance_id:
            inst["_dataset"] = doc.get("dataset", {})
            return dict(inst)
    raise SystemExit(f"swe_check: {instance_id} is not in {path}")


def visible_tests(inst: dict[str, Any]) -> list[str]:
    excluded = set(inst.get("visible_excluded", []))
    return [t for t in inst[P2P] if t not in excluded]


def _git(repo: Path, *args: str, env: dict[str, str] | None = None) -> str:
    p = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, encoding="utf-8",
                       errors="surrogateescape", env=env, stdin=subprocess.DEVNULL)
    if p.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {p.stderr.strip() or p.returncode}")
    return p.stdout


def working_tree_diff(repo: Path, base: str) -> str:
    """Committed and uncommitted changes (new files included) against base, without .benchbook/. A throwaway index
    is used so the repository's own index is not touched."""
    top = Path(_git(repo, "rev-parse", "--show-toplevel").strip())
    with tempfile.TemporaryDirectory(prefix="swe-check-index-") as d:
        env = {**os.environ, "GIT_INDEX_FILE": str(Path(d) / "index")}
        _git(top, "read-tree", "HEAD", env=env)
        _git(top, "add", "-A", "--", ".", EXCLUDE, env=env)
        return _git(top, "diff", "--cached", "--binary", "--no-color", "--no-ext-diff", base, "--", ".", EXCLUDE,
                    env=env)


def gold_patch(inst: dict[str, Any]) -> str:
    """The reference fix, read from the dataset at the pinned revision (instances.json leaves it out; only the dry
    run's toy instances carry theirs)."""
    if "patch" in inst:
        return str(inst["patch"])
    from datasets import load_dataset  # comes with swebench

    ds = inst["_dataset"]
    for row in load_dataset(ds["name"], split="test", revision=ds["revision"]):
        if row["instance_id"] == inst["instance_id"]:
            return str(row["patch"])
    raise SystemExit(f"swe_check: no reference fix for {inst['instance_id']}")


# ------------------------------------------------------------------------------------------- the Docker harness

def harness_instance(inst: dict[str, Any], mode: str) -> dict[str, Any]:
    """The dataset row make_test_spec expects, for one mode."""
    row = {k: v for k, v in inst.items() if not k.startswith("_")}
    if mode == "visible":
        row.update(eval_script=inst["visible_eval_script"], FAIL_TO_PASS=[], PASS_TO_PASS=visible_tests(inst))
    return row


def evaluate_docker(inst: dict[str, Any], mode: str, diff: str, timeout: int, work: Path) -> Graded:
    try:
        import docker
        from swebench.harness.constants import (
            APPLY_PATCH_FAIL,
            LOG_INSTANCE,
            LOG_TEST_OUTPUT,
            RUN_EVALUATION_LOG_DIR,
        )
        from swebench.harness.grading import get_logs_eval
        from swebench.harness.run_evaluation import run_instance
        from swebench.harness.utils import make_test_spec
    except ImportError as e:
        return Graded(mode, harness_error=f"the SWE-bench harness is not installed ({e}): run under "
                                         f"uv run --no-project --with {SWEBENCH} python -I ...")
    spec = make_test_spec(harness_instance(inst, mode))
    try:
        client = docker.from_env(timeout=600)
        client.ping()
    except Exception as e:  # noqa: BLE001 - any failure to reach Docker is the same harness error
        return Graded(mode, harness_error=f"Docker is not available: {e}")
    try:
        client.images.get(spec.image)
    except docker.errors.ImageNotFound:
        return Graded(mode, harness_error=f"image {spec.image} is not on this machine: run the preflight "
                                         "(python -m bench.code.run --preflight --execute) to pull it")
    except Exception as e:  # noqa: BLE001
        return Graded(mode, harness_error=f"Docker could not look up {spec.image}: {e}")
    run_id = f"benchbook-{mode}-{uuid.uuid4().hex[:12]}"
    pred = {"instance_id": spec.instance_id, "model_name_or_path": "benchbook", "model_patch": diff}
    os.chdir(work)  # run_instance writes logs/run_evaluation/... under the current directory
    result = run_instance(spec, pred, client, run_id, timeout=timeout, skip_patch=not diff)
    log_dir = work / RUN_EVALUATION_LOG_DIR / run_id / "benchbook" / spec.instance_id
    log = (log_dir / LOG_INSTANCE).read_text(errors="replace") if (log_dir / LOG_INSTANCE).is_file() else ""
    if result is None:
        if APPLY_PATCH_FAIL in log:
            return Graded(mode, problem="PatchApplyFailed", detail=_tail(log))
        if "timed out" in log or "Timeout error" in log:
            return Graded(mode, problem="TestsTimedOut", detail=f"over {timeout} s")
        return Graded(mode, harness_error="the harness stopped before grading", detail=_tail(log))
    report = result[1][spec.instance_id]
    if "tests_status" not in report:
        if report.get("infra_failure"):
            return Graded(mode, harness_error=f"infrastructure: {report.get('infra_failure_reason')}")
        return Graded(mode, problem="NoTestOutput", detail="the test run produced no results the harness could parse")
    status_map, _ = get_logs_eval(spec, str(log_dir / LOG_TEST_OUTPUT))
    graded = Graded(mode)
    for kind in (F2P, P2P):
        lists = report["tests_status"][kind]
        for test in lists["success"]:
            graded.tests.append((kind, test, status_map.get(test, "PASSED")))
        for test in lists["failure"]:
            found = status_map.get(test)
            graded.tests.append((kind, test, found if found in ("FAILED", "ERROR", "SKIPPED") else "MISSING"))
    return graded


def _tail(text: str, n: int = 12) -> str:
    return "\n".join(text.strip().splitlines()[-n:])


# ------------------------------------------------------------------------------------------- the stand-in

SUMMARY = re.compile(r"^(PASSED|FAILED|ERROR|XFAIL|XPASS) (\S+)")


def evaluate_local(inst: dict[str, Any], mode: str, diff: str, repo: Path, timeout: int, work: Path) -> Graded:
    """The dry run's stand-in for the Docker evaluation, with the eval script's steps: the base commit's tree, the
    change applied, the test patch's files reset to the base, the test patch applied (hidden), the test command run,
    the output graded. pytest -rA plays the log parser."""
    tree = work / "tree"
    tree.mkdir()
    source = Path(inst.get("source") or repo)  # the toy repository, also when run outside a worktree (preflight)
    archive = subprocess.run(["git", "-C", str(source), "archive", "--format=tar", inst["base_commit"]],
                             capture_output=True, check=True).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        if hasattr(tarfile, "data_filter"):  # 3.11.4 and later
            tar.extractall(tree, filter="data")
        else:
            tar.extractall(tree)  # noqa: S202 - our own git archive
    pristine = {f: (tree / f).read_bytes() if (tree / f).is_file() else None for f in inst["test_files"]}

    def apply(patch: str) -> bool:
        return subprocess.run(["git", "apply", "--binary", "-"], cwd=tree, input=patch.encode(),
                              capture_output=True).returncode == 0

    if diff and not apply(diff):
        return Graded(mode, problem="PatchApplyFailed")
    for f, data in pristine.items():  # git checkout <base> <test files>
        if data is None:
            (tree / f).unlink(missing_ok=True)
        else:
            (tree / f).write_bytes(data)
    if mode == "hidden" and not apply(inst["test_patch"]):
        return Graded(mode, harness_error="the test patch did not apply")
    targets = [t for t in inst["test_targets"] if mode == "hidden" or t not in inst["new_test_files"]]
    try:
        p = subprocess.run([sys.executable, "-m", "pytest", "-rA", "-p", "no:cacheprovider", "-q", *targets],
                           cwd=tree, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return Graded(mode, problem="TestsTimedOut", detail=f"over {timeout} s")
    status = {m.group(2): m.group(1) for line in p.stdout.splitlines() if (m := SUMMARY.match(line))}
    if not status:
        return Graded(mode, problem="NoTestOutput", detail=_tail(p.stdout))
    expected = {F2P: inst[F2P], P2P: inst[P2P]} if mode == "hidden" else {F2P: [], P2P: visible_tests(inst)}
    return Graded(mode, [(kind, t, status.get(t, "MISSING")) for kind in (F2P, P2P) for t in expected[kind]])


# ------------------------------------------------------------------------------------------- output

def write_junit(graded: Graded, path: Path) -> None:
    suite = ET.Element("testsuite", name=f"swe_check.{graded.mode}")
    if graded.harness_error or graded.problem:
        case = ET.SubElement(suite, "testcase", classname="harness",
                             name="infrastructure" if graded.harness_error else "tests")
        ET.SubElement(case, "error" if graded.harness_error else "failure",
                      type="HarnessError" if graded.harness_error else str(graded.problem),
                      message=(graded.harness_error or graded.detail or "")[:500])
    for kind, test, status in graded.tests:
        case = ET.SubElement(suite, "testcase", classname=kind, name=test)
        if not passes(kind, test, status):
            ET.SubElement(case, "failure", type=status, message=f"{kind} test {status.lower()}")
    suite.set("tests", str(len(suite)))
    suite.set("failures", str(len(graded.failing()) + (1 if graded.problem else 0)))
    ET.ElementTree(suite).write(path, encoding="utf-8", xml_declaration=True)


def summary(graded: Graded, instance_id: str) -> str:
    lines = [f"swe_check {graded.mode} {instance_id}"]
    if graded.harness_error:
        lines.append(f"harness error: {graded.harness_error}")
    if graded.problem:
        lines.append(f"failed before grading: {graded.problem}")
    if graded.detail:
        lines += [f"  | {x}" for x in graded.detail.splitlines()]
    for kind in (F2P, P2P):
        mine = [t for t in graded.tests if t[0] == kind]
        if mine:
            ok = sum(passes(*t) for t in mine)
            lines.append(f"{kind}: {ok}/{len(mine)} passed")
    failing = graded.failing()
    lines += [f"  {status:<8} {test}" for kind, test, status in failing[:MAX_LISTED]]
    if len(failing) > MAX_LISTED:
        lines.append(f"  ... and {len(failing) - MAX_LISTED} more")
    lines.append({EXIT_PASS: "PASS", EXIT_FAIL: "FAIL", EXIT_HARNESS: "HARNESS ERROR"}[graded.exit_code])
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--instance", required=True)
    ap.add_argument("--mode", required=True, choices=("visible", "hidden"))
    ap.add_argument("--junit", type=Path, help="write the graded tests as JUnit XML here")
    ap.add_argument("--timeout", type=int, default=1200, help="seconds for the tests inside the container")
    ap.add_argument("--instances", type=Path, default=INSTANCES)
    ap.add_argument("--repo", type=Path, default=Path.cwd(), help="the repository under test (default: cwd)")
    ap.add_argument("--backend", choices=("docker", "local"), default="docker",
                    help="local: the dry run's stand-in, no Docker")
    change = ap.add_mutually_exclusive_group()
    change.add_argument("--empty", action="store_true", help="evaluate the unmodified base (preflight)")
    change.add_argument("--gold", action="store_true", help="evaluate the reference fix (preflight)")
    args = ap.parse_args(argv)
    if args.mode == "hidden" and os.environ.get(HIDDEN_ENV) != "1":
        print("swe_check: the hidden check is run by the benchmark harness, not by the implementer",
              file=sys.stderr)
        return EXIT_USAGE
    inst = load_instance(args.instance, args.instances)
    repo = args.repo.resolve()
    if args.empty:
        diff = ""
    elif args.gold:
        diff = gold_patch(inst)
    else:
        diff = working_tree_diff(repo, inst["base_commit"])
    with tempfile.TemporaryDirectory(prefix="swe-check-") as d:
        work = Path(d)
        if args.backend == "local":
            graded = evaluate_local(inst, args.mode, diff, repo, args.timeout, work)
        else:
            graded = evaluate_docker(inst, args.mode, diff, args.timeout, work)
        os.chdir(repo)  # leave the temporary directory before it is removed
    if args.junit:
        write_junit(graded, args.junit)
    print(summary(graded, args.instance))
    return graded.exit_code


if __name__ == "__main__":
    sys.exit(main())
