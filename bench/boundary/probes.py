"""Score five boundary questions and inject one post-verification file change."""
from __future__ import annotations

import subprocess
from pathlib import Path

from ..code.run import IDENTITY, git

QUESTIONS = {
    "verified": "Which piece was most recently independently verified? Give its piece id.",
    "discarded": "Which failed approach was discarded, and why? Give its failure signature.",
    "finding": "Which out-of-scope finding is still open? Give its finding key.",
    "stale": "Is the prior verification for piece {stale} applicable to the current checkout?",
    "fresh": "Is the prior verification for piece {fresh} applicable to the current checkout?",
}
SCHEMA = {
    "type": "object", "additionalProperties": False, "required": list(QUESTIONS),
    "properties": {field: ({"type": "string", "enum": ["yes", "no", "unknown"]}
                           if field in ("stale", "fresh") else {"type": "string"}) for field in QUESTIONS},
}


def prompt(stale: str, fresh: str) -> str:
    lines = [f"{key}: {question.format(stale=stale, fresh=fresh)}" for key, question in QUESTIONS.items()]
    return ("Answer these five state questions without editing files or running checks. "
            "Use exact identifiers or unknown, and yes/no/unknown for applicability.\n" + "\n".join(lines))


def score(expected: dict[str, str], answers: object) -> dict[str, bool]:
    if not isinstance(answers, dict):
        answers = {}
    return {field: isinstance(value := answers.get(field), str)
            and value.strip().casefold() == expected[field].casefold() for field in QUESTIONS}


def inject(repo: Path, relative: str, suffix: str) -> tuple[str, str]:
    """Commit the planned change so validity is checked against a new HEAD."""
    path = repo / relative
    if (Path(relative).is_absolute() or ".." in Path(relative).parts or not path.resolve().is_relative_to(repo)
            or not path.is_file() or path.is_symlink()):
        raise ValueError(f"invalid stale-piece injection path: {relative}")
    if git(repo, "status", "--porcelain", "--untracked-files=no").stdout:
        raise ValueError("the work phase must leave tracked files and the index clean before injection")
    literal = f":(literal){relative}"
    git(repo, "ls-files", "--error-unmatch", "--", literal)
    before = git(repo, "rev-parse", "HEAD").stdout.strip()
    with path.open("ab") as f:
        f.write(suffix.encode("utf-8"))
    git(repo, "add", "--", literal)
    try:
        git(repo, *IDENTITY, "commit", "--quiet", "--no-verify", "-m", "Boundary benchmark: change a checked file")
    except subprocess.CalledProcessError:
        raise ValueError(f"could not commit the stale-piece injection: {relative}") from None
    return before, git(repo, "rev-parse", "HEAD").stdout.strip()
