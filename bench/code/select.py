"""Pick the SWE-bench Verified instances of the false completion measurement (bench/code/PREREGISTRATION.md) and
write bench/code/instances.json, which is committed: the instance list is fixed before anything runs.

    uv run --with pyarrow python -m bench.code.select                 # downloads the pinned revisions
    uv run --with pyarrow python -m bench.code.select --cache DIR     # keeps the parquet files in DIR

pyarrow is only needed here and only to read the dataset's parquet file, so it is added for this one command
(`uv run --with`) instead of becoming a dependency of the package.

The selection is a pure function of the two dataset files and the constants below:

1. Source: the harness's own copy of the dataset (SWE-bench/SWE-bench_Verified), which carries what the swebench 5
   harness needs to evaluate an instance (image, eval_script, log_parser, eval_type). The plan names
   princeton-nlp/SWE-bench_Verified; its instance ids, difficulty labels and base commits are checked to be the same,
   and both revisions are recorded.
2. Eligible: difficulty "<15 min fix", a repository with a light environment (LIGHT_REPOS: pure-Python projects
   whose images build no C extensions and pull no scientific stack), a visible check that runs at least one existing
   test, and every file of the test patch inside the repository's guard globs.
3. Allocation: N instances over the eligible repositories in proportion to their eligible counts (largest
   remainder, ties by repository name), so the sample keeps the eligible pool's mix.
4. Order: within a repository by sha256(SEED + instance id); the run order is the same hash over all instances, so a
   run that stops early has still sampled every repository.
5. Reserve: the next RESERVES_PER_REPO eligible instances of each repository in the same order. An instance that
   fails the preflight (its unmodified base must pass the visible check and fail the hidden one, the reference fix
   must pass the hidden one) is replaced by the next reserve of its repository.

The visible check runs the hidden check's test command without the test patch, on the PASS_TO_PASS tests that exist
before the patch: tests in files the patch creates, test functions the patch adds, and parametrized cases named
after a file the patch creates are left out (visible_excluded), and so are the test command's targets that the patch
creates (visible_eval_script)."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import urllib.request
from collections import Counter
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

from benchbook.core.globs import matching

HERE = Path(__file__).resolve().parent
OUT = HERE / "instances.json"
HARNESS_DATASET = "SWE-bench/SWE-bench_Verified"
HARNESS_REVISION = "78f471bf655a3137b2e8a75af1501690ec009ec3"  # 2026-08-16, the copy swebench 5.0.2 reads
LABEL_DATASET = "princeton-nlp/SWE-bench_Verified"
LABEL_REVISION = "c104f840cc67f8b6eec6f759ebc8b2693d585d4a"  # 2025-02-18
PARQUET = "data/test-00000-of-00001.parquet"
DIFFICULTY = "<15 min fix"
N = 40  # sized for at least 30 completion reports (plan, PR 1-2)
RESERVES_PER_REPO = 3
SEED = "benchbook-swe-verified-1"
# Every guard also holds these: a conftest.py can change what any test does, and the declarations themselves.
COMMON_GUARD = ["**/conftest.py", ".benchbook/config.toml"]
# Repositories with light environments, and their test folders (the guard of check verify).
LIGHT_REPOS: dict[str, list[str]] = {
    "django/django": ["tests/**"],
    "pallets/flask": ["tests/**"],
    "psf/requests": ["tests/**", "test_requests.py"],
    "pylint-dev/pylint": ["tests/**"],
    "pytest-dev/pytest": ["testing/**"],
    "sphinx-doc/sphinx": ["tests/**"],
    "sympy/sympy": ["sympy/**/tests/**"],
}
# Kept from the dataset row. Left out: the reference fix (patch) and hints_text, which the implementer must not see;
# the preflight reads the reference fix from the dataset itself.
FIELDS = ("instance_id", "repo", "base_commit", "version", "environment_setup_commit", "created_at", "difficulty",
          "image", "log_parser", "eval_type", "problem_statement", "FAIL_TO_PASS", "PASS_TO_PASS", "test_patch",
          "eval_script")
START, END = ": '>>>>> Start Test Output'", ": '>>>>> End Test Output'"
HEREDOC = re.compile(r"^git apply -v - <<'(EOF_\d+)'$")
ADDED_TEST = re.compile(r"^\+\s*(?:async\s+)?def\s+(test\w*)\s*\(")


# ------------------------------------------------------------------------------------------- the test patch

def patch_files(test_patch: str) -> tuple[list[str], list[str], set[str]]:
    """(files the patch touches, files it creates, test function names it adds)."""
    files: list[str] = []
    new: list[str] = []
    added: set[str] = set()
    old_path: str | None = None
    for line in test_patch.splitlines():
        if line.startswith("--- "):
            old_path = line[4:].strip()
        elif line.startswith("+++ "):
            path = line[4:].strip()
            if path.startswith("b/"):
                path = path[2:]
            if path != "/dev/null":
                files.append(path)
                if old_path == "/dev/null":
                    new.append(path)
        elif m := ADDED_TEST.match(line):
            added.add(m.group(1))
    return files, new, added


def django_label(path: str) -> str | None:
    """tests/a/b.py -> a.b, the label the harness puts on Django's runtests.py command line."""
    if path.startswith("tests/") and path.endswith(".py"):
        return path[len("tests/"):-len(".py")].replace("/", ".")
    return None


def visible_eval_script(eval_script: str, base_commit: str, files: Sequence[str], new: Sequence[str]) -> str | None:
    """The hidden check's script without the test patch, or None when no target of its test command exists before
    the patch (or the script does not have the expected shape)."""
    lines = eval_script.splitlines()
    out: list[str] = []
    skip_until: str | None = None
    removed_heredoc = False
    gone = set(new) | {label for p in new if (label := django_label(p))}
    known = set(files) | {label for p in files if (label := django_label(p))}
    in_test = False
    targets_left: int | None = None
    for line in lines:
        if skip_until is not None:
            if line == skip_until:
                skip_until = None
            continue
        if m := HEREDOC.match(line):
            skip_until, removed_heredoc = m.group(1), True
            continue
        if line.startswith(f"git checkout {base_commit} "):
            kept = [t for t in line.split()[3:] if t not in gone]
            if kept:
                out.append(" ".join(["git", "checkout", base_commit, *kept]))
            continue
        if line == START:
            in_test = True
            out.append(line)
            continue
        if line == END:
            in_test = False
            out.append(line)
            continue
        if in_test:
            tokens = line.split(" ")
            targets = [t for t in tokens if t in known]
            kept_tokens = [t for t in tokens if t not in gone]
            targets_left = len([t for t in targets if t not in gone])
            out.append(" ".join(kept_tokens))
            continue
        out.append(line)
    if not removed_heredoc or skip_until is not None or not targets_left:
        return None
    return "\n".join(out) + "\n"


def _test_name(test_id: str) -> tuple[str, str | None, str | None, str | None]:
    """(function name, file path, Django module, parameters) of a test id in the harness's three shapes: pytest
    'path::Class::name[params]', Django 'name (module.Class)', sympy 'name'."""
    if test_id.endswith(")") and " (" in test_id:
        name, module = test_id[:-1].split(" (", 1)
        return name.strip(), None, module, None
    params = None
    head = test_id
    if "[" in test_id:
        head, params = test_id.split("[", 1)
        params = params.rsplit("]", 1)[0]
    if "::" in head:
        parts = head.split("::")
        return parts[-1], parts[0], None, params
    return head.strip(), None, None, params


def visible_tests(pass_to_pass: Iterable[str], new: Sequence[str], added: set[str]) -> list[str]:
    """PASS_TO_PASS without the tests that only exist once the test patch is applied."""
    labels = [label for p in new if (label := django_label(p))]
    stems = [Path(p).stem for p in new if len(Path(p).stem) >= 3]
    kept = []
    for test_id in pass_to_pass:
        name, path, module, params = _test_name(test_id)
        if name in added or (path is not None and path in new):
            continue
        if module is not None and any(module == lb or module.startswith(lb + ".") for lb in labels):
            continue
        if params is not None and any(s in params for s in stems):
            continue
        kept.append(test_id)
    return kept


# ------------------------------------------------------------------------------------------- selection

def rank(instance_id: str, seed: str = SEED) -> str:
    return hashlib.sha256(f"{seed}:{instance_id}".encode()).hexdigest()


def _tests(value: Any) -> list[str]:
    """FAIL_TO_PASS / PASS_TO_PASS: a list in the harness's copy, a JSON string in princeton-nlp's."""
    return [str(x) for x in (json.loads(value) if isinstance(value, str) else value)]


def prepare(row: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    """The instance as instances.json keeps it, or (None, the reason it is not eligible)."""
    if row["difficulty"] != DIFFICULTY:
        return None, "difficulty"
    if row["repo"] not in LIGHT_REPOS:
        return None, "environment"
    inst = {k: row[k] for k in FIELDS}
    inst["FAIL_TO_PASS"], inst["PASS_TO_PASS"] = _tests(row["FAIL_TO_PASS"]), _tests(row["PASS_TO_PASS"])
    files, new, added = patch_files(row["test_patch"])
    guard = LIGHT_REPOS[row["repo"]] + COMMON_GUARD
    if not files or set(matching(files, guard)) != set(files):
        return None, "test patch outside the guard"
    script = visible_eval_script(row["eval_script"], row["base_commit"], files, new)
    if script is None:
        return None, "no visible check"
    visible = visible_tests(inst["PASS_TO_PASS"], new, added)
    if not visible:
        return None, "no visible tests"
    # The visible tests are PASS_TO_PASS minus these (stored as the difference: the lists run to hundreds of ids).
    inst.update(test_files=files, new_test_files=new, guard_paths=guard, visible_eval_script=script,
                visible_excluded=[t for t in inst["PASS_TO_PASS"] if t not in set(visible)])
    return inst, None


def allocate(counts: dict[str, int], n: int) -> dict[str, int]:
    """n over the repositories in proportion to counts: largest remainder, ties by name."""
    total = sum(counts.values())
    if total < n:
        raise ValueError(f"only {total} eligible instances for {n}")
    quotas = {repo: n * c / total for repo, c in counts.items()}
    out = {repo: int(q) for repo, q in quotas.items()}
    for repo in sorted(counts, key=lambda r: (-(quotas[r] - out[r]), r))[:n - sum(out.values())]:
        out[repo] += 1
    return out


def check_labels(rows: Sequence[dict[str, Any]], labels: Sequence[dict[str, Any]]) -> None:
    """The plan's dataset (princeton-nlp) must agree with the harness's copy on what selection reads."""
    a = {r["instance_id"]: r for r in rows}
    b = {r["instance_id"]: r for r in labels}
    if set(a) != set(b):
        raise ValueError(f"the two datasets list different instances ({len(set(a) ^ set(b))} differ)")
    for key in ("difficulty", "base_commit", "repo"):
        if bad := [i for i in a if a[i][key] != b[i][key]]:
            raise ValueError(f"{key} differs between the datasets for {len(bad)} instances, e.g. {bad[0]}")


def select(rows: Sequence[dict[str, Any]], n: int = N, seed: str = SEED,
           reserves: int = RESERVES_PER_REPO) -> dict[str, Any]:
    eligible: dict[str, list[dict[str, Any]]] = {}
    excluded: Counter[str] = Counter()
    for row in rows:
        inst, reason = prepare(row)
        if inst is None:
            excluded[str(reason)] += 1
            continue
        eligible.setdefault(inst["repo"], []).append(inst)
    for insts in eligible.values():
        insts.sort(key=lambda i: rank(i["instance_id"], seed))
    quota = allocate({repo: len(v) for repo, v in eligible.items()}, n)
    chosen = [i for repo in sorted(eligible) for i in eligible[repo][:quota[repo]]]
    reserve = [i for repo in sorted(eligible) if quota[repo]
               for i in eligible[repo][quota[repo]:quota[repo] + reserves]]
    chosen.sort(key=lambda i: rank(i["instance_id"], seed))
    return {
        "pool": {repo: len(v) for repo, v in sorted(eligible.items())},
        "allocation": {repo: quota[repo] for repo in sorted(quota)},
        "excluded": dict(sorted(excluded.items())),
        "instances": chosen,
        "reserve": reserve,
    }


# ------------------------------------------------------------------------------------------- the dataset

def download(dataset: str, revision: str, cache: Path) -> Path:
    target = cache / dataset.replace("/", "__") / revision / PARQUET.rsplit("/", 1)[-1]
    if not target.is_file():
        target.parent.mkdir(parents=True, exist_ok=True)
        url = f"https://huggingface.co/datasets/{dataset}/resolve/{revision}/{PARQUET}"
        with urllib.request.urlopen(url, timeout=120) as resp:  # noqa: S310 - a fixed https URL
            data = resp.read()
        tmp = target.with_suffix(".part")
        tmp.write_bytes(data)
        tmp.replace(target)
    return target


def read_parquet(path: Path) -> list[dict[str, Any]]:
    try:
        import pyarrow.parquet as pq  # only here: uv run --with pyarrow
    except ImportError as e:
        raise SystemExit("reading the dataset needs pyarrow: uv run --with pyarrow python -m bench.code.select") from e
    rows: list[dict[str, Any]] = pq.read_table(path).to_pylist()
    return rows


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cache", type=Path, default=Path.home() / ".cache" / "benchbook-swebench")
    ap.add_argument("--out", type=Path, default=OUT)
    args = ap.parse_args(argv)
    harness = download(HARNESS_DATASET, HARNESS_REVISION, args.cache)
    labels = download(LABEL_DATASET, LABEL_REVISION, args.cache)
    rows = read_parquet(harness)
    check_labels(rows, read_parquet(labels))
    picked = select(rows)
    doc = {
        "about": "Fixed before the run; see bench/code/PREREGISTRATION.md. Written by python -m bench.code.select.",
        "dataset": {"name": HARNESS_DATASET, "revision": HARNESS_REVISION, "file": PARQUET, "sha256": sha256(harness),
                    "rows": len(rows)},
        "labels": {"name": LABEL_DATASET, "revision": LABEL_REVISION, "file": PARQUET, "sha256": sha256(labels),
                   "checked": ["instance_id", "difficulty", "base_commit", "repo"]},
        "selection": {"difficulty": DIFFICULTY, "repos": LIGHT_REPOS, "common_guard": COMMON_GUARD, "n": N,
                      "seed": SEED, "reserves_per_repo": RESERVES_PER_REPO},
        **picked,
    }
    args.out.write_text(json.dumps(doc, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"{len(picked['instances'])} instances and {len(picked['reserve'])} reserves -> {args.out}\n"
          f"allocation {picked['allocation']}\nexcluded {picked['excluded']}", file=sys.stderr)


if __name__ == "__main__":
    main()
