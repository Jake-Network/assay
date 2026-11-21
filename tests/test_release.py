"""The release pipeline (docs/releasing.md). It only runs on GitHub, so these checks read the workflow files: a later
edit must not store a token, widen the publishing job, drop a system from the smoke run, or drift from the names
PyPI's trusted publisher is registered with."""
from __future__ import annotations

import re
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
SMOKE = ROOT / "scripts" / "smoke_wheel.sh"


def _code(text: str) -> str:
    """The YAML without its comments, so that a comment mentioning a keyword does not satisfy a check."""
    return "\n".join(re.sub(r"(^|\s+)#.*$", "", line) for line in text.splitlines())


def _jobs(workflow: str) -> dict[str, str]:
    """Job name -> its text. Jobs sit two spaces under `jobs:`; the project has no YAML parser to read them with."""
    body = _code((WORKFLOWS / workflow).read_text(encoding="utf-8")).split("\njobs:\n", 1)[1]
    parts = re.split(r"^  ([\w-]+):[ \t]*\n", body, flags=re.M)
    return dict(zip(parts[1::2], parts[2::2], strict=True))


def test_release_builds_and_publishes_on_a_version_tag_without_a_stored_token() -> None:
    text = _code((WORKFLOWS / "release.yml").read_text(encoding="utf-8"))
    assert re.search(r'^on:\n  push:\n    tags: \["v\*"\]$', text, re.M)
    # Trusted Publishing: nothing to store, so nothing may be read from the repository's secrets
    assert not re.search(r"secrets\.|UV_PUBLISH_TOKEN|UV_PUBLISH_PASSWORD|password:", text)
    jobs = _jobs("release.yml")
    assert list(jobs) == ["build", "smoke", "publish"]
    assert "run: uv build" in jobs["build"]
    assert "run: uv publish" in jobs["publish"]
    # the tag must be the version of the files (PyPI never takes a version twice)
    assert 'dist/benchbook-$version-py3-none-any.whl' in jobs["build"] and "GITHUB_REF_NAME#v" in jobs["build"]


def test_only_the_publish_job_can_publish_and_it_runs_nothing_from_the_repository() -> None:
    jobs = _jobs("release.yml")
    assert [name for name, job in jobs.items() if "id-token" in job] == ["publish"]
    publish = jobs["publish"]
    assert re.search(r"permissions:\n\s+id-token: write\n\s+steps:", publish)  # that permission and no other
    assert re.search(r"needs: \[build, smoke\]", publish)  # only files that were built and run on both systems
    assert "actions/checkout" not in publish and "scripts/" not in publish
    assert re.findall(r"run: (.+)", publish) == ["uv publish"]


def test_the_wheel_is_smoke_tested_on_linux_and_macos_on_every_push_and_at_release() -> None:
    for workflow in ("ci.yml", "release.yml"):
        smoke = _jobs(workflow)["smoke"]
        assert re.search(r"os: \[ubuntu-latest, macos-latest\]", smoke), workflow
        assert "run: bash scripts/smoke_wheel.sh" in smoke, workflow
    ci = _jobs("ci.yml")["smoke"]
    assert ci.index("run: uv build") < ci.index("run: bash scripts/smoke_wheel.sh")
    # the release run takes the wheel the build job made, not one built again on the smoke runner
    release = _jobs("release.yml")["smoke"]
    assert "needs: build" in release and "actions/download-artifact" in release and "uv build" not in release


def test_smoke_script_runs_doctor_init_doctor_from_the_wheel() -> None:
    script = SMOKE.read_text(encoding="utf-8")
    assert 'uvx --from "$WHEEL" benchbook "$@"' in script
    steps = re.findall(r"^(?:out=\"\$\()?bb (doctor|init)\b", script, re.M)
    assert steps == ["doctor", "init", "doctor"]
    # a run must not reach a real ledger: HOME moves, and no inherited variable names a database or a project
    assert 'export HOME="$WORK/home"' in script and "(BENCHBOOK|ASSAY)_" in script


def test_smoke_script_is_valid_bash_and_refuses_a_missing_wheel(tmp_path: Path) -> None:
    if shutil.which("bash") is None:
        pytest.skip("bash is not on PATH")
    assert subprocess.run(["bash", "-n", str(SMOKE)], capture_output=True, text=True).returncode == 0
    p = subprocess.run(["bash", str(SMOKE), str(tmp_path / "benchbook-0.1.0-py3-none-any.whl")], capture_output=True,
                       text=True, timeout=60)
    assert p.returncode == 2 and "no such wheel" in p.stderr


def test_release_doc_names_what_the_trusted_publisher_must_match() -> None:
    """PyPI matches the project, the workflow file and the environment exactly; a rename on one side only stops
    the next release at the publish step."""
    doc = (ROOT / "docs" / "releasing.md").read_text(encoding="utf-8")
    m = re.search(r"environment:\n\s+name: (\S+)", _jobs("release.yml")["publish"])
    assert m and f"| Environment name | `{m.group(1)}` |" in doc
    assert "| Workflow name | `release.yml` |" in doc
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert f"| PyPI Project Name | `{project['name']}` |" in doc
    assert project["urls"]["Repository"].endswith("/Jake-Network/benchbook")
    assert "| Owner | `Jake-Network` |" in doc and "| Repository name | `benchbook`" in doc


def test_release_doc_lists_the_steps_that_wait_for_the_owner_and_the_exit_condition() -> None:
    doc = (ROOT / "docs" / "releasing.md").read_text(encoding="utf-8")
    for step in ("Trademark search", "gh repo rename benchbook", "GitHub organisation", "npm", "crates.io",
                 "pending publisher", "git push origin v0.1.0", "## Phase 0 exit condition", "uvx benchbook doctor"):
        assert step in doc, step
    assert "The owner confirms each one right before it is done." in " ".join(doc.split())
