"""`benchbook --version` and `benchbook doctor`: build, schema revisions and the Claude Code connection checks."""
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

import benchbook
from benchbook.hooks import spooler

BIN = str(Path(sys.executable).parent)


@pytest.fixture
def env(tmp_path: Path, db_url: str, project: str) -> dict[str, Any]:
    """A healthy setup: plugin installed and enabled, settings env matching the shell, empty hook log."""
    home = tmp_path / "home"
    claude = home / ".claude"
    (claude / "plugins").mkdir(parents=True)
    (claude / "plugins" / "installed_plugins.json").write_text(json.dumps(
        {"version": 2, "plugins": {"benchbook@benchbook": [{"scope": "user", "installPath": "/x"}]}}))
    (claude / "settings.json").write_text(json.dumps(
        {"enabledPlugins": {"benchbook@benchbook": True},
         "env": {"BENCHBOOK_PROJECT": project, "BENCHBOOK_DATABASE_URL": db_url}}))
    repo = tmp_path / "repo"
    repo.mkdir()
    e = {**os.environ, "HOME": str(home), "CLAUDE_CONFIG_DIR": str(claude), "XDG_STATE_HOME": str(tmp_path / "state"),
         "BENCHBOOK_DATABASE_URL": db_url, "BENCHBOOK_PROJECT": project, "PATH": BIN + os.pathsep + "/usr/bin:/bin"}
    e.pop("BENCHBOOK_DATA_DIR", None)
    return {"env": e, "repo": repo, "claude": claude}


def doctor(s: dict[str, Any]) -> tuple[int, str, dict[str, Any]]:
    p = subprocess.run([sys.executable, "-m", "benchbook.cli", "doctor"], capture_output=True, text=True,
                       cwd=s["repo"], env=s["env"], timeout=120)
    j = subprocess.run([sys.executable, "-m", "benchbook.cli", "--json", "doctor"], capture_output=True, text=True,
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
    for args in (["-m", "benchbook", "--version"], ["-m", "benchbook.cli", "--version"]):
        p = subprocess.run([sys.executable, *args], capture_output=True, text=True, timeout=60)
        assert p.returncode == 0 and p.stdout.strip() == f"benchbook {benchbook.__version__}"
    # from git: a release (0.1.0) or a dev build that names its commit
    assert re.fullmatch(r"\d+\.\d+(\.\d+)?(\.dev\d+\+g[0-9a-f]+(\.d\d{8})?)?",
                        benchbook.__version__), benchbook.__version__


def test_healthy_setup_passes_and_prints_build_and_schema(env: dict[str, Any]) -> None:
    from benchbook.db.migrate import head

    code, out, checks = doctor(env)
    assert code == 0, out
    assert re.search(rf"^ok    benchbook build +{re.escape(benchbook.__version__)}$", out, re.M)
    assert re.search(rf"^ok    db schema +{head()}$", out, re.M)
    assert re.search(rf"^ok    build schema head +{head()}$", out, re.M)
    assert {"benchbook on PATH", "claude code plugin", "BENCHBOOK_DATA_DIR", "BENCHBOOK_PROJECT",
            "hook failures"} <= set(checks)


def test_benchbook_not_on_claude_codes_path(env: dict[str, Any]) -> None:
    env["env"]["PATH"] = "/usr/bin:/bin"
    if any(Path(d, "benchbook").exists() for d in ("/usr/bin", "/bin")):
        pytest.skip("a system-wide benchbook is installed")
    code, out, checks = doctor(env)
    assert code != 0
    assert "uv tool update-shell" in failing(out, "benchbook on PATH")
    assert checks["benchbook on PATH"]["ok"] is False


def test_plugin_not_installed_or_disabled(env: dict[str, Any]) -> None:
    settings = env["claude"] / "settings.json"
    s = json.loads(settings.read_text())
    s["enabledPlugins"] = {"benchbook@benchbook": False}
    settings.write_text(json.dumps(s))
    code, out, _ = doctor(env)
    assert code != 0 and "claude plugin enable benchbook@benchbook" in failing(out, "claude code plugin")
    (env["claude"] / "plugins" / "installed_plugins.json").unlink()
    code, out, _ = doctor(env)
    assert code != 0 and "claude plugin install benchbook@benchbook" in failing(out, "claude code plugin")
    # a manual install (.mcp.json with a benchbook server) is fine
    (env["repo"] / ".mcp.json").write_text(json.dumps({"mcpServers": {"benchbook": {"command": "benchbook"}}}))
    assert doctor(env)[2]["claude code plugin"]["ok"] is True


def test_settings_env_differs_from_shell(env: dict[str, Any], tmp_path: Path) -> None:
    local = env["repo"] / ".claude" / "settings.local.json"
    local.parent.mkdir()
    local.write_text(json.dumps({"env": {"BENCHBOOK_PROJECT": "some-other-project"}}))
    code, out, _ = doctor(env)
    line = failing(out, "BENCHBOOK_PROJECT")
    assert code != 0 and "some-other-project" in line and str(local) in line
    local.unlink()
    env["env"]["BENCHBOOK_DATA_DIR"] = str(tmp_path / "data")  # shell only: Claude Code started elsewhere misses it
    # with BENCHBOOK_DATABASE_URL on both sides the data directory is not used
    assert doctor(env)[2]["BENCHBOOK_DATA_DIR"]["ok"] is True
    path, s = _settings(env)
    del s["env"]["BENCHBOOK_DATABASE_URL"]
    path.write_text(json.dumps(s))
    code, out, _ = doctor(env)
    assert code != 0 and '"BENCHBOOK_DATA_DIR"' in failing(out, "BENCHBOOK_DATA_DIR")


def test_hook_failures_and_dead_reports(env: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    for k in ("XDG_STATE_HOME", "BENCHBOOK_DATABASE_URL"):
        monkeypatch.setenv(k, env["env"][k])
    monkeypatch.delenv("BENCHBOOK_DATA_DIR", raising=False)
    state = spooler._state_dir()
    state.mkdir(parents=True)
    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    other = env["repo"].parent / "elsewhere"
    (state / "hook.log").write_text(
        f"2020-01-01T00:00:00 hook processing failed for /old.json (queued to retry): Traceback\n"
        f"{now} subagent-stop: not captured: no benchbook project for {other} (no .benchbook/config.toml there)\n")
    assert doctor(env)[0] == 0  # old lines and other repositories do not count
    with (state / "hook.log").open("a") as f:
        f.write(f"{now} hook processing failed permanently for /s/x.json: Traceback\n")
    code, out, _ = doctor(env)
    assert code != 0 and "benchbook hook replay" in failing(out, "hook failures") and "1 failure line" in out
    with (state / "hook.log").open("a") as f:
        f.write(f"{now} replay: processed 1 (captured 1, cited 0, skipped 0); dead 0; still queued 0\n")
    assert doctor(env)[0] == 0
    dead = spooler._spool_dir() / "dead"
    dead.mkdir(parents=True)
    (dead / "a.json").write_text("{}")
    code, out, _ = doctor(env)
    assert code != 0 and "1 report(s) in" in out and "benchbook hook replay" in failing(out, "hook failures")


def test_unreachable_database_keeps_json_and_other_checks(env: dict[str, Any]) -> None:
    env["env"]["BENCHBOOK_DATABASE_URL"] = "postgresql://nobody@127.0.0.1:1/none"
    p = subprocess.run([sys.executable, "-m", "benchbook.cli", "--json", "doctor"], capture_output=True, text=True,
                       cwd=env["repo"], env=env["env"], timeout=120)
    assert p.returncode == 1, p.stderr
    out = json.loads(p.stdout)
    checks = {c["check"]: c for c in out["checks"]}
    assert checks["connect"]["ok"] is False and checks["connect"]["fix"]
    assert checks["db schema"]["ok"] is False and checks["build schema head"]["ok"] is True
    assert {"benchbook build", "benchbook on PATH", "claude code plugin", "hook failures"} <= set(checks)


def test_path_check_does_not_run_the_binary_it_finds(env: dict[str, Any], tmp_path: Path) -> None:
    """A repository's settings can point PATH anywhere; doctor reports the path and never executes it."""
    bad = tmp_path / "evil"
    bad.mkdir()
    marker = tmp_path / "ran"
    (bad / "benchbook").write_text(f"#!/bin/sh\ntouch {marker}\n")
    (bad / "benchbook").chmod(0o755)
    settings = env["repo"] / ".claude" / "settings.json"
    settings.parent.mkdir()
    settings.write_text(json.dumps({"env": {"PATH": str(bad)}}))
    _, out, checks = doctor(env)
    assert checks["benchbook on PATH"]["ok"] is True and checks["benchbook on PATH"]["detail"] == str(bad / "benchbook")
    assert not marker.exists()


def test_fix_commands_quote_paths_with_spaces(env: dict[str, Any], tmp_path: Path) -> None:
    import shlex

    data = str(tmp_path / "Library" / "Application Support" / "benchbook" / "pg")
    s = json.loads((env["claude"] / "settings.json").read_text())
    del s["env"]["BENCHBOOK_DATABASE_URL"]  # so the data directory is the database target on the Claude Code side
    s["env"]["BENCHBOOK_DATA_DIR"] = data
    (env["claude"] / "settings.json").write_text(json.dumps(s))
    _, _, checks = doctor(env)
    fix = checks["BENCHBOOK_DATA_DIR"]["fix"]
    cmd = fix.split("   (")[0]
    assert shlex.split(cmd) == ["export", f"BENCHBOOK_DATA_DIR={data}"]
    # the other direction: a JSON snippet for the settings file
    s["env"].pop("BENCHBOOK_DATA_DIR")
    (env["claude"] / "settings.json").write_text(json.dumps(s))
    env["env"]["BENCHBOOK_DATA_DIR"] = data
    fix = doctor(env)[2]["BENCHBOOK_DATA_DIR"]["fix"]
    snippet = fix[fix.index("{"):fix.index("}") + 1]
    assert json.loads(snippet) == {"BENCHBOOK_DATA_DIR": data}


def _settings(s: dict[str, Any]) -> tuple[Path, dict[str, Any]]:
    path = s["claude"] / "settings.json"
    return path, json.loads(path.read_text())


def test_claude_code_on_another_database(env: dict[str, Any]) -> None:
    """Claude Code's MCP server and hook use the settings URL; a different database there is a failure, and the URL
    (which can hold a password) is never printed."""
    from psycopg.conninfo import make_conninfo

    path, s = _settings(env)
    shell_url = env["env"]["BENCHBOOK_DATABASE_URL"]
    s["env"]["BENCHBOOK_DATABASE_URL"] = make_conninfo(shell_url, dbname="some_other_db", password="s3cret-pw")
    path.write_text(json.dumps(s))
    code, out, checks = doctor(env)
    line = failing(out, "BENCHBOOK_DATABASE_URL")
    assert code != 0 and "different database" in line and "restart Claude Code" in line
    assert "s3cret-pw" not in out and "some_other_db" not in out and shell_url not in out
    assert "s3cret-pw" not in json.dumps(checks)
    # the same server and database with another password is the same target
    s["env"]["BENCHBOOK_DATABASE_URL"] = make_conninfo(shell_url, password="s3cret-pw")
    path.write_text(json.dumps(s))
    assert doctor(env)[2]["BENCHBOOK_DATABASE_URL"]["ok"] is True


def test_bad_home_path_in_settings_keeps_json(env: dict[str, Any]) -> None:
    path, s = _settings(env)
    s["env"]["BENCHBOOK_DATA_DIR"] = "~no-such-user-benchbook-test/pg"
    path.write_text(json.dumps(s))
    p = subprocess.run([sys.executable, "-m", "benchbook.cli", "--json", "doctor"], capture_output=True, text=True,
                       cwd=env["repo"], env=env["env"], timeout=120)
    assert p.returncode == 1, p.stderr
    checks = {c["check"]: c for c in json.loads(p.stdout)["checks"]}
    assert checks["BENCHBOOK_DATA_DIR"]["ok"] is False and "cannot expand" in checks["BENCHBOOK_DATA_DIR"]["detail"]
    assert checks["BENCHBOOK_DATA_DIR"]["fix"]
    assert {"connect", "benchbook on PATH", "BENCHBOOK_PROJECT", "hook failures"} <= set(checks)


def test_hook_failures_are_read_per_database(env: dict[str, Any], tmp_path: Path,
                                             monkeypatch: pytest.MonkeyPatch) -> None:
    """hook.log is shared, spools are per database: replaying database B's empty spool does not settle database A's
    failure, and a report still queued in A's spool counts."""
    monkeypatch.setenv("XDG_STATE_HOME", env["env"]["XDG_STATE_HOME"])
    monkeypatch.delenv("BENCHBOOK_DATA_DIR", raising=False)
    monkeypatch.setenv("BENCHBOOK_DATABASE_URL", env["env"]["BENCHBOOK_DATABASE_URL"])
    spool_a = spooler._spool_dir()
    monkeypatch.setenv("BENCHBOOK_DATABASE_URL", "postgresql://u@127.0.0.1:1/b")
    spool_b = spooler._spool_dir()
    spool_b.mkdir(parents=True)
    spooler.replay_now(spool_b)  # clean: nothing queued in B
    log = spooler._state_dir() / "hook.log"
    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    text = log.read_text()
    log.write_text(f"{now} hook processing failed for {spool_a}/r.json (queued to retry): Traceback\n" + text)
    assert f"replay {spool_b}:" in text
    code, out, _ = doctor(env)  # run against A
    assert code != 0 and "1 failure line" in failing(out, "hook failures")
    # a failure in B's spool does not count for A, and a clean replay of A settles A's
    spool_a.mkdir(parents=True)
    monkeypatch.setenv("BENCHBOOK_DATABASE_URL", env["env"]["BENCHBOOK_DATABASE_URL"])
    spooler.replay_now(spool_a)
    with log.open("a") as f:
        f.write(f"{now} hook entry x.json moved to {spool_b}/dead: BenchbookError: unknown project\n")
    assert doctor(env)[0] == 0
    # a report the background processor never drained
    stuck = spool_a / "stuck.json"
    stuck.write_text("{}")
    old = time.time() - 3600
    os.utime(stuck, (old, old))
    code, out, _ = doctor(env)
    assert code != 0 and "1 report(s) queued for over 5 min" in failing(out, "hook failures")


def test_installed_plugin_with_the_old_matcher(env: dict[str, Any], tmp_path: Path) -> None:
    install = tmp_path / "cache" / "benchbook" / "0.1.0"
    (install / "hooks").mkdir(parents=True)
    (install / "hooks" / "hooks.json").write_text(json.dumps({"hooks": {"SubagentStop": [
        {"matcher": "^benchbook:benchbook-worker$",
         "hooks": [{"type": "command", "command": "benchbook hook subagent-stop"}]}]}}))
    (env["claude"] / "plugins" / "installed_plugins.json").write_text(json.dumps(
        {"version": 2, "plugins": {"benchbook@benchbook": [{"scope": "user", "installPath": str(install)}]}}))
    code, out, _ = doctor(env)
    assert code != 0 and "claude plugin update benchbook@benchbook" in failing(out, "plugin hooks")
    current = Path(__file__).parents[1] / "integrations" / "claude-code"
    (env["claude"] / "plugins" / "installed_plugins.json").write_text(json.dumps(
        {"version": 2, "plugins": {"benchbook@benchbook": [{"scope": "user", "installPath": str(current)}]}}))
    code, out, checks = doctor(env)
    assert code == 0, out and "plugin hooks" not in checks


def test_installed_plugin_without_the_check_hooks(env: dict[str, Any], tmp_path: Path) -> None:
    """0.1.2's hooks (capture only): doctor says Bash results and task completions are not seen."""
    install = tmp_path / "cache" / "benchbook" / "0.1.2"
    (install / "hooks").mkdir(parents=True)
    (install / "hooks" / "hooks.json").write_text(json.dumps({"hooks": {
        "SubagentStop": [{"hooks": [{"type": "command", "command": "benchbook hook subagent-stop"}]}],
        "PostToolUse": [{"matcher": "SubagentHandback",
                         "hooks": [{"type": "command", "command": "benchbook hook subagent-handback"}]}]}}))
    (env["claude"] / "plugins" / "installed_plugins.json").write_text(json.dumps(
        {"version": 2, "plugins": {"benchbook@benchbook": [{"scope": "user", "installPath": str(install)}]}}))
    code, out, checks = doctor(env)
    line = failing(out, "plugin hooks")
    assert code != 0 and "predates 0.1.3" in line and "benchbook hook bash-result" in line
    assert "benchbook hook task-completed" in line and "claude plugin update benchbook@benchbook" in line
