#!/usr/bin/env bash
# benchbook's GitHub Action: re-run the declared checks of each piece a pull request completes, record them in the
# shared ledger, and keep one summary comment on the pull request. action.yml passes GitHub's context in as BB_*
# variables; the work is done by benchbook (pr pieces, check verify, pr summary, pr comment). See README.md here.
#
# Never print BB_DATABASE_URL or BB_TOKEN and never `set -x`: GitHub masks secrets it knows, not values derived from
# them. Exit codes: 0 every re-run passed (or nothing to do, as on a fork's pull request), 1 a check failed or a step
# could not run.
set -euo pipefail

notice() { echo "::notice title=benchbook::$*"; }
warn() { echo "::warning title=benchbook::$*"; }
fail() { echo "::error title=benchbook::$*"; exit 1; }

# check verify runs the pull request's code: nothing it starts may inherit the GitHub token. A plain (unexported)
# shell variable is not passed to child processes.
gh_token=${BB_GITHUB_TOKEN:-}
unset BB_GITHUB_TOKEN

case "${GITHUB_EVENT_NAME:-}" in
  pull_request) ;;
  pull_request_target)
    fail "runs on pull_request only: under pull_request_target the pull request's code would run with this repository's secrets" ;;
  *) fail "runs on pull_request events (this is ${GITHUB_EVENT_NAME:-unknown})" ;;
esac
[[ "${BB_PR:-}" =~ ^[0-9]+$ ]] || fail "no pull request number in the event"

# A fork's (and Dependabot's) pull request gets no secrets and a read-only GITHUB_TOKEN: say so and pass.
if [ "${BB_HEAD_REPO:-}" != "${GITHUB_REPOSITORY:-}" ]; then
  notice "pull request from a fork (${BB_HEAD_REPO:-unknown}): GitHub gives it no secrets and a read-only token, so nothing is re-run, recorded or commented. Re-run the pieces from a branch of this repository."
  exit 0
fi
url=${BB_DATABASE_URL:-${BENCHBOOK_DATABASE_URL:-}}
token=${BB_TOKEN:-${BENCHBOOK_TOKEN:-}}
unset BB_DATABASE_URL BB_TOKEN
if [ -z "$url" ]; then
  if [ "${GITHUB_ACTOR:-}" = "dependabot[bot]" ]; then
    notice "Dependabot's pull requests get no secrets: nothing is re-run, recorded or commented."
    exit 0
  fi
  fail "no ledger: pass database-url (from a secret such as secrets.BENCHBOOK_DATABASE_URL)"
fi

tmp=${RUNNER_TEMP:-}
[ -n "$tmp" ] || tmp=$(mktemp -d)

# --- benchbook itself ------------------------------------------------------------------------------------------
case "${BB_INSTALL:-}" in
  skip) ;;  # already on PATH
  *)
    python=${BB_PYTHON:-python3}
    "$python" -c 'import sys; sys.exit(sys.version_info < (3, 11))' \
      || fail "benchbook needs Python 3.11 or later: add actions/setup-python before this action"
    "$python" -m venv "$tmp/benchbook-venv"
    pip=("$tmp/benchbook-venv/bin/python" -m pip --quiet --disable-pip-version-check)
    spec=${BB_INSTALL:-}
    if [ -z "$spec" ]; then
      # The action's own tree, so the commands match this action. A downloaded action has no .git for hatch-vcs to
      # read a version from; the pretend version names this one build only (no dependency is built with it).
      SETUPTOOLS_SCM_PRETEND_VERSION=0.0.0+action "${pip[@]}" wheel --no-deps -w "$tmp/benchbook-wheel" \
        "${GITHUB_ACTION_PATH:?}/../.."
      spec=$(ls "$tmp"/benchbook-wheel/benchbook-*.whl)
    fi
    "${pip[@]}" install "$spec"
    PATH="$tmp/benchbook-venv/bin:$PATH"
    ;;
esac
command -v benchbook >/dev/null || fail "benchbook is not on PATH"

# --- who writes ------------------------------------------------------------------------------------------------
export BENCHBOOK_DATABASE_URL="$url"
unset url
if [ -n "$token" ]; then
  # a token is one agent: it authors every record (BENCHBOOK_AGENT would have to name the same agent)
  export BENCHBOOK_TOKEN="$token"
  unset BENCHBOOK_AGENT
else
  # owner connection: the name is self-declared, one per workflow run
  export BENCHBOOK_AGENT="ci:${GITHUB_RUN_ID:?}"
  unset BENCHBOOK_TOKEN
fi
unset token
if [ -n "${BB_PROJECT:-}" ]; then export BENCHBOOK_PROJECT="$BB_PROJECT"; fi

decide=(--no-decide)
if [ "${BB_DECIDE:-false}" = true ]; then
  if [ -n "${BENCHBOOK_TOKEN:-}" ]; then
    decide=()  # check verify decides only when the token's agent is an orchestrator or a human
  else
    warn "decide: true needs a per-agent token of a lead (orchestrator or human); on an owner connection the CI's name is self-declared, so this run records and does not decide"
  fi
fi

run_url="${GITHUB_SERVER_URL:-https://github.com}/${GITHUB_REPOSITORY}/actions/runs/${GITHUB_RUN_ID}"
trace=$(printf '{"ci": "github-actions", "repository": "%s", "run_id": "%s", "run_attempt": "%s", "run_url": "%s", "pull_request": %s}' \
  "$GITHUB_REPOSITORY" "$GITHUB_RUN_ID" "${GITHUB_RUN_ATTEMPT:-1}" "$run_url" "$BB_PR")

git cat-file -e "${BB_BASE_SHA:?}^{commit}" 2>/dev/null && git cat-file -e "${BB_HEAD_SHA:?}^{commit}" 2>/dev/null \
  || fail "the pull request's base or head commit is not in the checkout: use actions/checkout with fetch-depth: 0"

# --- re-run each piece -----------------------------------------------------------------------------------------
failed=0
benchbook pr pieces --base "$BB_BASE_SHA" --head "$BB_HEAD_SHA" --claims > "$tmp/benchbook-pieces.tsv" \
  || fail "could not list the pieces of this pull request"
while IFS=$'\t' read -r piece sha reporter claim; do
  [ -n "$piece" ] || continue
  args=(check verify "$piece" "$sha" --base "$BB_BASE_SHA" --trace-ref "$trace")
  # The run is evidence for the claim the summary shows: check verify checks that it is about this piece and commit
  # and takes its reporter from it. Without a claim nothing is linked.
  if [ -n "${claim:-}" ] && [ "$claim" != "-" ]; then
    args+=(--claim "$claim")
  elif [ "$reporter" != "-" ]; then
    args+=(--reported-by "$reporter")
  fi
  echo "::group::benchbook check verify ${sha:0:12} (piece $piece)"
  benchbook "${args[@]}" ${decide[@]+"${decide[@]}"} < /dev/null || failed=1
  echo "::endgroup::"
done < "$tmp/benchbook-pieces.tsv"

# --- the summary -----------------------------------------------------------------------------------------------
if ! benchbook pr summary --base "$BB_BASE_SHA" --head "$BB_HEAD_SHA" --run-url "$run_url" > "$tmp/benchbook-summary.md"
then
  fail "could not write the summary"
fi
if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then cat "$tmp/benchbook-summary.md" >> "$GITHUB_STEP_SUMMARY"; fi
if [ "${BB_COMMENT:-true}" = true ]; then
  comment=(pr comment --repo "$GITHUB_REPOSITORY" --pr "$BB_PR" --body-file "$tmp/benchbook-summary.md")
  if [ -n "${BB_COMMENT_AUTHOR:-}" ]; then comment+=(--author "$BB_COMMENT_AUTHOR"); fi
  # no piece: keep an earlier comment current, but do not start one on every pull request
  if [ ! -s "$tmp/benchbook-pieces.tsv" ]; then comment+=(--update-only); fi
  # the comment needs only the GitHub token, and gh is not given the ledger's credentials
  env -u BENCHBOOK_DATABASE_URL -u BENCHBOOK_TOKEN GH_TOKEN="$gh_token" benchbook "${comment[@]}" \
    || fail "could not post the summary comment: the workflow needs permissions: pull-requests: write"
fi
exit "$failed"
