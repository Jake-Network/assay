"""`assay --version` and `assay doctor`: build, schema revisions and the Claude Code connection checks."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

import assay
from assay import hooks

BIN = str(Path(sys.executable).parent)


@pytest.fixture
def env(tmp_path: Path, db_url: str, project: str) -> dict[str, Any]:
    """A healthy setup: plugin installed and enabled, settings env matching the shell, empty hook log."""
    home = tmp_path / "home"
    claude = home / ".claude"
    (claude / "plugins").mkdir(parents=True)
    (claude / "plugins" / "installed_plugins.json").write_text(json.dumps(
        {"version": 2, "plugins": {"assay@assay": [{"scope": "user", "installPath": "/x"}]}}))
    (claude / "settings.json").write_text(json.dumps(
        {"enabledPlugins": {"assay@assay": True}, "env": {"ASSAY_PROJECT": project}}))
    repo = tmp_path / "repo"
    repo.mkdir()
    e = {**os.environ, "HOME": str(home), "CLAUDE_CONFIG_DIR": str(claude), "XDG_STATE_HOME": str(tmp_path / "state"),
         "ASSAY_DATABASE_URL": db_url, "ASSAY_PROJECT": project, "PATH": BIN + os.pathsep + "/usr/bin:/bin"}
    e.pop("ASSAY_DATA_DIR", None)
    return {"env": e, "repo": repo, "claude": claude}


def doctor(s: dict[str, Any]) -> tuple[int, str, dict[str, Any]]:
    p = subprocess.run([sys.executable, "-m", "assay.cli", "doctor"], capture_output=True, text=True,
                       cwd=s["repo"], env=s["env"], timeout=120)
    j = subprocess.run([sys.executable, "-m", "assay.cli", "--json", "doctor"], capture_output=True, text=True,
                       cwd=s["repo"], env=s["env"], timeout=120)
    checks = {c["check"]: c for c in json.loads(j.stdout)["checks"]}
    assert p.returncode == j.returncode
    return p.returncode, p.stdout, checks


def failing(out: str, name: str) -> str:
    """The FAIL line for this check and the fix line under it."""
    m = re.search(rf"^FAIL  {re.escape(name)} .*(?:\n {{6}}(?!fix:).*)*\n      fix: .+$", out, re.M)
    assert m, out
    return m.group(0)


def test_version_flag_names_the_build() -> None:
    for args in (["-m", "assay", "--version"], ["-m", "assay.cli", "--version"]):
        p = subprocess.run([sys.executable, *args], capture_output=True, text=True, timeout=60)
        assert p.returncode == 0 and p.stdout.strip() == f"assay {assay.__version__}"
    # from git: a release (0.1.0) or a dev build that names its commit
    assert re.fullmatch(r"\d+\.\d+(\.\d+)?(\.dev\d+\+g[0-9a-f]+(\.d\d{8})?)?", assay.__version__), assay.__version__


def test_healthy_setup_passes_and_prints_build_and_schema(env: dict[str, Any]) -> None:
    from assay.db.migrate import head

    code, out, checks = doctor(env)
    assert code == 0, out
    assert re.search(rf"^ok    assay build +{re.escape(assay.__version__)}$", out, re.M)
    assert re.search(rf"^ok    db schema +{head()}$", out, re.M)
    assert re.search(rf"^ok    build schema head +{head()}$", out, re.M)
    assert {"assay on PATH", "claude code plugin", "ASSAY_DATA_DIR", "ASSAY_PROJECT", "hook failures"} <= set(checks)


def test_assay_not_on_claude_codes_path(env: dict[str, Any]) -> None:
    env["env"]["PATH"] = "/usr/bin:/bin"
    if any(Path(d, "assay").exists() for d in ("/usr/bin", "/bin")):
        pytest.skip("a system-wide assay is installed")
    code, out, checks = doctor(env)
    assert code != 0
    assert "uv tool update-shell" in failing(out, "assay on PATH")
    assert checks["assay on PATH"]["ok"] is False


def test_plugin_not_installed_or_disabled(env: dict[str, Any]) -> None:
    settings = env["claude"] / "settings.json"
    s = json.loads(settings.read_text())
    s["enabledPlugins"] = {"assay@assay": False}
    settings.write_text(json.dumps(s))
    code, out, _ = doctor(env)
    assert code != 0 and "claude plugin enable assay@assay" in failing(out, "claude code plugin")
    (env["claude"] / "plugins" / "installed_plugins.json").unlink()
    code, out, _ = doctor(env)
    assert code != 0 and "claude plugin install assay@assay" in failing(out, "claude code plugin")
    # a manual install (.mcp.json with an assay server) is fine
    (env["repo"] / ".mcp.json").write_text(json.dumps({"mcpServers": {"assay": {"command": "assay"}}}))
    assert doctor(env)[2]["claude code plugin"]["ok"] is True


def test_settings_env_differs_from_shell(env: dict[str, Any], tmp_path: Path) -> None:
    local = env["repo"] / ".claude" / "settings.local.json"
    local.parent.mkdir()
    local.write_text(json.dumps({"env": {"ASSAY_PROJECT": "some-other-project"}}))
    code, out, _ = doctor(env)
    line = failing(out, "ASSAY_PROJECT")
    assert code != 0 and "some-other-project" in line and str(local) in line
    local.unlink()
    env["env"]["ASSAY_DATA_DIR"] = str(tmp_path / "data")  # shell only: Claude Code started elsewhere misses it
    code, out, _ = doctor(env)
    assert code != 0 and '"ASSAY_DATA_DIR"' in failing(out, "ASSAY_DATA_DIR")


def test_hook_failures_and_dead_reports(env: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    for k in ("XDG_STATE_HOME", "ASSAY_DATABASE_URL"):
        monkeypatch.setenv(k, env["env"][k])
    monkeypatch.delenv("ASSAY_DATA_DIR", raising=False)
    state = hooks._state_dir()
    state.mkdir(parents=True)
    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    other = env["repo"].parent / "elsewhere"
    (state / "hook.log").write_text(
        f"2020-01-01T00:00:00 hook processing failed for /old.json (queued to retry): Traceback\n"
        f"{now} subagent-stop: not captured: no assay project for {other} (no .assay/config.toml there)\n")
    assert doctor(env)[0] == 0  # old lines and other repositories do not count
    with (state / "hook.log").open("a") as f:
        f.write(f"{now} hook processing failed permanently for /s/x.json: Traceback\n")
    code, out, _ = doctor(env)
    assert code != 0 and "assay hook replay" in failing(out, "hook failures") and "1 failure line" in out
    with (state / "hook.log").open("a") as f:
        f.write(f"{now} replay: processed 1 (captured 1, cited 0, skipped 0); dead 0; still queued 0\n")
    assert doctor(env)[0] == 0
    dead = hooks._spool_dir() / "dead"
    dead.mkdir(parents=True)
    (dead / "a.json").write_text("{}")
    code, out, _ = doctor(env)
    assert code != 0 and "1 report(s) in" in out and "assay hook replay" in failing(out, "hook failures")


def test_unreachable_database_keeps_json_and_other_checks(env: dict[str, Any]) -> None:
    env["env"]["ASSAY_DATABASE_URL"] = "postgresql://nobody@127.0.0.1:1/none"
    p = subprocess.run([sys.executable, "-m", "assay.cli", "--json", "doctor"], capture_output=True, text=True,
                       cwd=env["repo"], env=env["env"], timeout=120)
    assert p.returncode == 1, p.stderr
    out = json.loads(p.stdout)
    checks = {c["check"]: c for c in out["checks"]}
    assert checks["connect"]["ok"] is False and checks["connect"]["fix"]
    assert checks["db schema"]["ok"] is False and checks["build schema head"]["ok"] is True
    assert {"assay build", "assay on PATH", "claude code plugin", "hook failures"} <= set(checks)


def test_path_check_does_not_run_the_binary_it_finds(env: dict[str, Any], tmp_path: Path) -> None:
    """A repository's settings can point PATH anywhere; doctor reports the path and never executes it."""
    bad = tmp_path / "evil"
    bad.mkdir()
    marker = tmp_path / "ran"
    (bad / "assay").write_text(f"#!/bin/sh\ntouch {marker}\n")
    (bad / "assay").chmod(0o755)
    settings = env["repo"] / ".claude" / "settings.json"
    settings.parent.mkdir()
    settings.write_text(json.dumps({"env": {"PATH": str(bad)}}))
    _, out, checks = doctor(env)
    assert checks["assay on PATH"]["ok"] is True and checks["assay on PATH"]["detail"] == str(bad / "assay")
    assert not marker.exists()


def test_fix_commands_quote_paths_with_spaces(env: dict[str, Any], tmp_path: Path) -> None:
    import shlex

    data = str(tmp_path / "Library" / "Application Support" / "assay" / "pg")
    s = json.loads((env["claude"] / "settings.json").read_text())
    s["env"]["ASSAY_DATA_DIR"] = data
    (env["claude"] / "settings.json").write_text(json.dumps(s))
    _, _, checks = doctor(env)
    fix = checks["ASSAY_DATA_DIR"]["fix"]
    cmd = fix.split("   (")[0]
    assert shlex.split(cmd) == ["export", f"ASSAY_DATA_DIR={data}"]
    # the other direction: a JSON snippet for the settings file
    s["env"].pop("ASSAY_DATA_DIR")
    (env["claude"] / "settings.json").write_text(json.dumps(s))
    env["env"]["ASSAY_DATA_DIR"] = data
    fix = doctor(env)[2]["ASSAY_DATA_DIR"]["fix"]
    snippet = fix[fix.index("{"):fix.index("}") + 1]
    assert json.loads(snippet) == {"ASSAY_DATA_DIR": data}
