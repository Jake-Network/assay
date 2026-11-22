"""Install, uninstall and the plugin split (T07): the two plugins of the marketplace, the hook shim, the commands
install and uninstall run (against fake `claude` and `uv` programs), the Codex manifest merge, and the checks
`init` proposes per language."""
from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

from benchbook import doctor, install, proposals

ROOT = Path(__file__).resolve().parent.parent
SHIM = ROOT / "integrations" / "claude-code" / "bin" / "benchbook-hook"


def fake(bin_dir: Path, name: str, log: Path) -> None:
    """A program that appends its argv to log and succeeds."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    p = bin_dir / name
    p.write_text(f'#!/bin/sh\necho "{name} $*" >> "{log}"\ncat >/dev/null 2>&1 || true\n')
    p.chmod(p.stat().st_mode | stat.S_IEXEC)


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


# ------------------------------------------------------------------------------------------- the plugins

def test_the_marketplace_has_a_hook_plugin_and_an_mcp_plugin() -> None:
    market = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text())
    plugins = {p["name"]: ROOT / p["source"] for p in market["plugins"]}
    assert set(plugins) == {"benchbook", "benchbook-mcp"}
    hooks_only, mcp = plugins["benchbook"], plugins["benchbook-mcp"]
    for d, name in ((hooks_only, "benchbook"), (mcp, "benchbook-mcp")):
        assert json.loads((d / ".claude-plugin" / "plugin.json").read_text())["name"] == name
    assert not (hooks_only / ".mcp.json").exists() and not (hooks_only / "agents").exists()
    assert not (hooks_only / "skills").exists()
    assert (mcp / ".mcp.json").is_file() and (mcp / "agents" / "benchbook-worker.md").is_file()
    commands = [h["command"] for es in json.loads((hooks_only / "hooks" / "hooks.json").read_text())["hooks"].values()
                for e in es for h in e["hooks"]]
    assert commands and all(c.startswith('"${CLAUDE_PLUGIN_ROOT}/bin/benchbook-hook" ') for c in commands)
    assert set(doctor.CHECK_HOOKS) <= doctor._hook_commands(
        json.loads((hooks_only / "hooks" / "hooks.json").read_text())["hooks"])
    mode = git(ROOT, "ls-files", "-s", str(SHIM.relative_to(ROOT))).split()[0]
    assert mode == "100755"


def test_the_shim_runs_benchbook_or_notes_that_it_is_missing(tmp_path: Path) -> None:
    sh = ["sh", str(SHIM), "bash-result"]
    data = tmp_path / "data"
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "CLAUDE_PLUGIN_DATA": str(data)}
    p = subprocess.run(sh, input="{}", env=env, capture_output=True, text=True)
    assert p.returncode == 0 and p.stdout == "" and p.stderr == ""
    assert "hook bash-result skipped" in (data / "missing-cli").read_text()
    log = tmp_path / "calls"
    fake(tmp_path / "bin", "benchbook", log)
    p = subprocess.run(sh, input="{}", env={**env, "PATH": f"{tmp_path / 'bin'}:/usr/bin:/bin"},
                       capture_output=True, text=True)
    assert p.returncode == 0 and log.read_text().strip() == "benchbook hook bash-result"


# ------------------------------------------------------------------------------------------- install and uninstall

@pytest.fixture
def path_with_fakes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    log = tmp_path / "calls"
    for name in ("claude", "uv", "benchbook"):
        fake(tmp_path / "bin", name, log)
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}:{os.environ['PATH']}")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    return log


def test_install_runs_uv_and_the_claude_plugin_commands(path_with_fakes: Path) -> None:
    plan = install.install()
    calls = path_with_fakes.read_text().splitlines()
    assert calls == [f"uv tool install benchbook=={install.version()}",
                     "claude plugin marketplace add Jake-Network/benchbook",
                     "claude plugin install benchbook@benchbook"]
    assert any("benchbook init" in s for s in plan.done)
    install.install(mcp=True, cli=False)
    assert path_with_fakes.read_text().splitlines()[-1] == "claude plugin install benchbook-mcp@benchbook"


def test_dry_run_runs_nothing(path_with_fakes: Path) -> None:
    plan = install.install(dry_run=True)
    assert not path_with_fakes.exists()
    assert "would run: claude plugin install benchbook@benchbook" in plan.done


def test_codex_manifest_is_merged_and_stripped(path_with_fakes: Path, tmp_path: Path) -> None:
    target = tmp_path / "codex" / "hooks.json"
    target.parent.mkdir()
    mine = {"type": "command", "command": "my-linter"}
    target.write_text(json.dumps({"hooks": {"PostToolUse": [{"matcher": "^Bash$", "hooks": [mine]}]}}))
    install.install("codex", cli=False)
    install.install("codex", cli=False)  # twice: still one benchbook entry per event
    merged = json.loads(target.read_text())["hooks"]
    assert [h["command"] for e in merged["PostToolUse"] for h in e["hooks"]] == [
        "my-linter", "benchbook hook bash-result --harness codex", "benchbook hook notes --harness codex"]
    assert len(merged["SubagentStop"]) == 1
    install.uninstall(None)
    assert json.loads(target.read_text())["hooks"] == {"PostToolUse": [{"matcher": "^Bash$", "hooks": [mine]}]}


def test_gemini_install_preserves_other_settings_and_grouped_hooks(path_with_fakes: Path, tmp_path: Path,
                                                                  monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GEMINI_CLI_HOME", str(tmp_path / "gemini"))
    target = tmp_path / "gemini" / ".gemini" / "settings.json"
    target.parent.mkdir(parents=True)
    mine = {"type": "command", "command": "my-linter"}
    ours = {"type": "command", "command": "benchbook hook notes --harness gemini"}
    group = {"matcher": "read_file", "sequential": True, "hooks": [mine]}
    existing = {"general": {"vimMode": True}, "hooks": {"AfterTool": [group]}}
    target.write_text(json.dumps({**existing, "hooks": {"AfterTool": [{**group, "hooks": [mine, ours]}]}}))

    install.install("gemini", cli=False)
    merged = json.loads(target.read_text())
    install.install("gemini", cli=False)
    assert json.loads(target.read_text()) == merged
    assert merged["general"] == existing["general"]
    assert merged["hooks"]["AfterTool"][0] == group
    assert set(merged["hooks"]) == {"AfterTool", "SessionStart"}

    install.uninstall(None)
    assert json.loads(target.read_text()) == existing


def test_uninstall_removes_plugins_state_and_the_local_folder(path_with_fakes: Path, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    (repo / ".benchbook").mkdir()
    (repo / ".benchbook" / "config.toml").write_text('project = "x"\n')
    (repo / ".git" / "info" / "exclude").write_text("*.log\n/.benchbook/\n")
    state = tmp_path / "state" / "benchbook"
    (state / "spool").mkdir(parents=True)
    plan = install.uninstall(repo, dry_run=True)
    assert state.exists() and (repo / ".benchbook").exists() and any("would remove" in s for s in plan.done)
    plan = install.uninstall(repo)
    assert not state.exists() and not (repo / ".benchbook").exists()
    assert (repo / ".git" / "info" / "exclude").read_text() == "*.log\n"
    calls = path_with_fakes.read_text().splitlines()
    assert "claude plugin uninstall benchbook@benchbook" in calls
    assert any("kept the embedded database" in s for s in plan.done)


def test_uninstall_keeps_the_folder_when_git_does_not_answer(path_with_fakes: Path, tmp_path: Path) -> None:
    """A corrupt index makes git ls-files fail with no output; that is no answer, not "untracked"."""
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    (repo / ".benchbook").mkdir()
    (repo / ".benchbook" / "config.toml").write_text('project = "x"\n')
    git(repo, "add", ".benchbook")
    git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "tracked")
    (repo / ".git" / "index").write_bytes(b"not an index")
    for dry in (True, False):
        plan = install.uninstall(repo, dry_run=dry)
        assert (repo / ".benchbook" / "config.toml").exists()
        assert not any("benchbook folder" in s for s in plan.done)
        assert any(s.startswith(f"kept {repo / '.benchbook'}: git ls-files failed") for s in plan.done)
    plain = tmp_path / "plain"  # outside any repository (cli's _repo_root falls back to the cwd)
    (plain / ".benchbook").mkdir(parents=True)
    plan = install.uninstall(plain)
    assert (plain / ".benchbook").is_dir() and any("git ls-files failed" in s for s in plan.done)


def test_purge_keeps_a_directory_that_is_not_a_cluster(path_with_fakes: Path, tmp_path: Path,
                                                       monkeypatch: pytest.MonkeyPatch) -> None:
    data = tmp_path / "data"
    (data / "keep.txt").parent.mkdir()
    (data / "keep.txt").write_text("mine\n")
    monkeypatch.setenv("BENCHBOOK_DATA_DIR", str(data))
    for dry in (True, False):
        plan = install.uninstall(None, dry_run=dry, purge=True)
        assert (data / "keep.txt").read_text() == "mine\n"
        assert any("not a PostgreSQL data directory" in s for s in plan.done)
        assert not any("the embedded database:" in s for s in plan.done)


def test_purge_removes_a_cluster_only_after_its_server_is_confirmed_stopped(
        path_with_fakes: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from benchbook.db import conn
    from benchbook.errors import BenchbookError

    data = tmp_path / "pg"
    data.mkdir()
    (data / "PG_VERSION").write_text("16\n")
    monkeypatch.setenv("BENCHBOOK_DATA_DIR", str(data))
    plan = install.uninstall(None, dry_run=True, purge=True)
    assert data.is_dir() and f"would remove the embedded database: {data}" in plan.done

    def still_running(d: Path | None = None) -> bool:
        raise BenchbookError("not confirmed stopped")

    monkeypatch.setattr(conn, "stop_embedded", still_running)
    plan = install.uninstall(None, purge=True)
    assert (data / "PG_VERSION").is_file()
    assert any(s.startswith(f"kept the embedded database {data}") for s in plan.done)

    stopped: list[Path | None] = []
    monkeypatch.setattr(conn, "stop_embedded", lambda d=None: stopped.append(d) or True)
    plan = install.uninstall(None, purge=True)
    assert stopped == [data] and not data.exists()
    assert f"removed the embedded database: {data}" in plan.done


# ------------------------------------------------------------------------------------------- init's proposals

@pytest.mark.parametrize(("files", "want"), [
    ({"pyproject.toml": "[project]\nname = 'x'\n[dependency-groups]\ndev = ['pytest', 'ruff']\n", "uv.lock": ""},
     {"unit": "uv run pytest -q --junitxml={junit}", "lint": "uv run ruff check"}),
    ({"pyproject.toml": "[project]\nname = 'x'\n", "tests/test_a.py": ""},
     {"unit": "python -m pytest -q --junitxml={junit}"}),
    ({"package.json": json.dumps({"scripts": {"test": "vitest run", "lint": "eslint ."}}), "pnpm-lock.yaml": ""},
     {"unit": "pnpm test", "lint": "pnpm run lint"}),
    ({"package.json": json.dumps({"scripts": {"test": 'echo "Error: no test specified" && exit 1'}})}, {}),
    ({"go.mod": "module x\n"}, {"unit": "go test ./...", "lint": "go vet ./..."}),
    ({"Cargo.toml": "[package]\nname = 'x'\n"}, {"unit": "cargo test"}),
    ({"Makefile": "test:\n\tpytest\nlint:\n\truff check\n"}, {"unit": "make test", "lint": "make lint"}),
])
def test_init_proposes_the_repositorys_own_checks(tmp_path: Path, files: dict[str, str],
                                                  want: dict[str, str]) -> None:
    for name, text in files.items():
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text(text)
    git(tmp_path, "init", "-q", "-b", "main")
    prop = proposals.propose(tmp_path)
    assert prop.checks == want
    if want:
        import tomllib

        from benchbook.config import parse_checks, parse_guard

        table = tomllib.loads(prop.toml())
        assert set(parse_checks(table["checks"])) == set(want)
        assert ".benchbook/config.toml" in parse_guard(table["guard"])[0]
