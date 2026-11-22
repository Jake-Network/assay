#!/usr/bin/env bash
# A new user's first minute, run from a built wheel: `uvx benchbook doctor` on a machine that has never had benchbook,
# `benchbook init` in an empty directory, then `doctor` again. It fails when the wheel misses a file the commands
# need, or when the embedded Postgres dependency does not install or start on this OS. CI runs it on fresh Linux and
# macOS runners for every push (ci.yml) and on the release artifacts before they are published (release.yml).
#
# HOME is a temp directory for the run, so the default data and state directories are the ones exercised (on macOS
# the data directory has a space) and your real ledger is never read or written.
# Usage: uv build && bash scripts/smoke_wheel.sh [wheel, default: the only one in dist/]
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
if [[ $# -ge 1 ]]; then
    WHEEL="$1"
else
    wheels=("$ROOT"/dist/*.whl)
    if [[ ${#wheels[@]} -ne 1 || ! -f "${wheels[0]}" ]]; then
        echo "smoke: expected exactly one wheel in $ROOT/dist (run: uv build), found: ${wheels[*]}" >&2
        exit 2
    fi
    WHEEL="${wheels[0]}"
fi
if [[ ! -f "$WHEEL" ]]; then
    echo "smoke: no such wheel: $WHEEL" >&2
    exit 2
fi
# benchbook-<version>-py3-none-any.whl
VERSION="$(basename "$WHEEL")"
VERSION="${VERSION#benchbook-}"
VERSION="${VERSION%%-*}"

WORK="$(mktemp -d)"
# uvx keeps a cached environment per wheel path, and a rebuilt wheel usually has the same file name. Run a copy at a
# path no earlier run used, so that what is checked is this file and not the previous build.
cp "$WHEEL" "$WORK/"
WHEEL="$WORK/$(basename "$WHEEL")"
# uv keeps its cache and its Pythons under HOME. Keep using the real ones, so that moving HOME moves only what
# benchbook itself writes and a local run does not download every dependency again.
UV_CACHE_DIR="$(uv cache dir)"
UV_PYTHON_INSTALL_DIR="$(uv python dir)"
export UV_CACHE_DIR UV_PYTHON_INSTALL_DIR
export HOME="$WORK/home"
# Nothing inherited may point this run at an existing ledger, project or Claude Code setup (ASSAY_ is the prefix
# from before the rename, still read as a fallback).
while IFS= read -r name; do
    unset "$name"
done < <(compgen -e | grep -E '^(BENCHBOOK|ASSAY)_' || true)
unset XDG_DATA_HOME XDG_STATE_HOME XDG_CONFIG_HOME CLAUDE_CONFIG_DIR
# `init` writes its config at the top of the enclosing git repository: stop that search at the temp directory, so a
# TMPDIR inside a checkout still counts as an empty directory.
export GIT_CEILING_DIRECTORIES="$WORK"

bb() {
    uvx --from "$WHEEL" benchbook "$@"
}
cleanup() {
    # the embedded server outlives the command that started it (`benchbook db stop` is how a user stops it)
    bb db stop >/dev/null 2>&1 || true
    rm -rf -- "$WORK"
}
trap cleanup EXIT
mkdir -p "$HOME" "$WORK/empty"
cd "$WORK/empty"

echo "== benchbook --version"
got="$(bb --version)"
echo "$got"
if [[ "$got" != "benchbook $VERSION" ]]; then
    echo "smoke: the wheel is $VERSION but the installed command reports: $got" >&2
    exit 1
fi

echo "== benchbook doctor (nothing set up yet)"
# Exit 1 is expected here: the schema and the project do not exist before `init`, and doctor says so. What must
# already hold is that the package imports and that the embedded Postgres installed and started.
out="$(bb doctor || true)"
printf '%s\n' "$out"
for check in "benchbook build" "connect" "postgres >= 15" "python"; do
    if ! grep -Eq "^ok +$check " <<<"$out"; then
        echo "smoke: doctor did not report ok for: $check" >&2
        exit 1
    fi
done

echo "== benchbook init (empty directory)"
bb init --goal "Smoke test" --metric score --direction max
if [[ ! -f .benchbook/config.toml ]]; then
    echo "smoke: init did not write .benchbook/config.toml in the empty directory" >&2
    exit 1
fi

echo "== benchbook doctor (after init)"
bb doctor

echo "== benchbook install --dry-run"
out="$(bb install --dry-run)"
printf '%s\n' "$out"
if ! grep -q "would run: claude plugin install benchbook@benchbook" <<<"$out"; then
    echo "smoke: install --dry-run does not install the hook plugin" >&2
    exit 1
fi

echo "== benchbook init --yes (a git repository: code pack, proposed checks, nothing to commit)"
mkdir -p "$WORK/repo"
cd "$WORK/repo"
git init -q
printf '[project]\nname = "smoke"\n\n[dependency-groups]\ndev = ["pytest"]\n' >pyproject.toml
bb init --yes
grep -q '^\[checks.unit\]' .benchbook/config.toml || { echo "smoke: init --yes proposed no unit check" >&2; exit 1; }
grep -qx '/.benchbook/' .git/info/exclude || { echo "smoke: init did not exclude .benchbook/" >&2; exit 1; }
if [[ -n "$(git status --porcelain --untracked-files=all -- .benchbook)" ]]; then
    echo "smoke: .benchbook/ shows up in git status" >&2
    exit 1
fi

echo "== benchbook uninstall"
bb uninstall
if [[ -e .benchbook ]] || grep -q benchbook .git/info/exclude; then
    echo "smoke: uninstall left .benchbook/ or its exclude line" >&2
    exit 1
fi
echo "smoke ok: benchbook $VERSION"
