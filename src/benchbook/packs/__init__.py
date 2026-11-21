"""Packs: claim kinds and JSON Schemas validated at write time (P3)."""
from __future__ import annotations

import json
import tomllib
from dataclasses import dataclass, field
from functools import cache
from importlib import resources
from importlib.resources.abc import Traversable
from typing import Any

from jsonschema import Draft202012Validator

from ..errors import BenchbookError, ValidationFailed

# What record_experiment does with an ok run whose config, code_ref and seed were already recorded:
# "refuse" stores nothing and points to the earlier run; "notify" stores it and reports the earlier run.
DEDUP_MODES = ("refuse", "notify")
# [verify] rule in pack.toml: what decide(claim, "verified", oracle="rerun") checks (docs/concepts.md, docs/code.md).
# "replication" (default; core and ml-research): >= rerun_min_seeds seeds and rerun_min_authors authors of the original
# config and code (project settings), and for an improvement both means beat the baseline. "all_pass" (code): a
# completion is verified by clean, passing re-runs of its piece at its commit by agents other than its implementer.
VERIFY_RULES = ("replication", "all_pass")


@dataclass(frozen=True)
class VerifyPolicy:
    rule: str = "replication"
    # all_pass only (replication takes its thresholds from the project settings): the passing independent re-runs
    # needed, and the distinct agents needed counting the implementer (2 = the implementer and one other).
    min_runs: int = 1
    min_authors: int = 2


@dataclass
class Pack:
    name: str
    claim_kinds: list[str]
    dedup_ignore: list[str]
    brief_limits: dict[str, int]
    config_max_depth: int
    config_max_bytes: int
    dedup_mode: str = "refuse"
    ok_metrics_required: list[str] = field(default_factory=list)
    brief_compiler: str | None = None  # "module:function", replaces the default brief (§6.7)
    # [project] in pack.toml: what `benchbook init --pack` uses when the command line leaves it out
    # (metric, direction, settings).
    project_defaults: dict[str, Any] = field(default_factory=dict)
    verify: VerifyPolicy = field(default_factory=VerifyPolicy)
    validators: dict[str, Draft202012Validator] = field(default_factory=dict)

    def validate(self, schema_name: str, value: Any, label: str) -> None:
        v = self.validators.get(schema_name)
        if v is not None:
            check(v, value, label)


def check(validator: Draft202012Validator, value: Any, label: str) -> None:
    problems = []
    for err in sorted(validator.iter_errors(value), key=lambda e: list(e.absolute_path)):
        path = "".join(f"[{p}]" if isinstance(p, int) else f".{p}" for p in err.absolute_path)
        problems.append(f"{label}{path}: {err.message}")
    if problems:
        raise ValidationFailed(problems)


@cache
def load_pack(name: str) -> Pack:
    if not isinstance(name, str) or not name or "/" in name or "\\" in name or name.startswith("."):
        raise BenchbookError(f"unknown pack: {name}")
    root = resources.files("benchbook.packs").joinpath(name)
    if not root.joinpath("pack.toml").is_file():
        raise BenchbookError(f"unknown pack: {name}")
    return read_pack(name, root)


def read_pack(name: str, root: Traversable) -> Pack:
    """Parse the pack in directory `root` (pack.toml and schemas/)."""
    meta = tomllib.loads(root.joinpath("pack.toml").read_text(encoding="utf-8"))
    dedup_mode = meta.get("dedup_mode", "refuse")
    if dedup_mode not in DEDUP_MODES:
        raise BenchbookError(f"pack {name}: dedup_mode must be one of {', '.join(DEDUP_MODES)}; got {dedup_mode!r}")
    pack = Pack(
        name=name,
        claim_kinds=list(meta["claim_kinds"]),
        dedup_ignore=list(meta.get("dedup_ignore", ["notes", "seed", "run_id", "timestamp"])),
        dedup_mode=dedup_mode,
        brief_limits=dict(meta.get("brief", {})),
        config_max_depth=int(meta.get("config", {}).get("max_depth", 6)),
        config_max_bytes=int(meta.get("config", {}).get("max_bytes", 65536)),
        ok_metrics_required=list(meta.get("ok_metrics_required", [])),
        brief_compiler=meta.get("brief_compiler"),
        project_defaults=dict(meta.get("project", {})),
        verify=read_verify(name, meta.get("verify", {})),
    )
    schemas = root.joinpath("schemas")
    for f in schemas.iterdir():
        if f.name.endswith(".json"):
            schema = json.loads(f.read_text(encoding="utf-8"))
            pack.validators[f.name[:-5]] = Draft202012Validator(schema)
    return pack


def read_verify(name: str, table: Any) -> VerifyPolicy:
    """[verify] of pack.toml. Strict: a misspelt key or a threshold that would let an implementer verify its own
    completion is an error, not a default."""
    if not isinstance(table, dict):
        raise BenchbookError(f"pack {name}: [verify] must be a table")
    rule = table.get("rule", "replication")
    if rule not in VERIFY_RULES:
        raise BenchbookError(f"pack {name}: [verify] rule must be one of {', '.join(VERIFY_RULES)}; got {rule!r}")
    allowed = {"rule"} | ({"min_runs", "min_authors"} if rule == "all_pass" else set())
    if unknown := sorted(set(table) - allowed):
        raise BenchbookError(f"pack {name}: [verify] unknown key {', '.join(unknown)} for rule {rule}"
                             + (" (its thresholds are the project settings rerun_min_seeds and rerun_min_authors)"
                                if rule == "replication" else ""))
    policy = VerifyPolicy(rule, table.get("min_runs", 1), table.get("min_authors", 2))
    for key, least in (("min_runs", 1), ("min_authors", 2)):
        v = getattr(policy, key)
        if isinstance(v, bool) or not isinstance(v, int) or v < least:
            raise BenchbookError(f"pack {name}: [verify] {key} must be an integer >= {least}; got {v!r}")
    return policy


def depth(value: Any) -> int:
    if isinstance(value, dict):
        return 1 + max((depth(v) for v in value.values()), default=0)
    if isinstance(value, list):
        return 1 + max((depth(v) for v in value), default=0)
    return 0
