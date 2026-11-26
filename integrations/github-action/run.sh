#!/usr/bin/env bash
# benchbook's GitHub Action: re-run the declared checks of each piece a pull request completes, record them in the
# shared ledger (or, without database-url, in a ledger of this run only), and keep one summary comment on the pull
# request. action.yml passes GitHub's context in as BB_* variables; the work is done by benchbook (pr pieces, check
# verify, pr summary, pr comment). See README.md here.
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
# Without database-url the run keeps a ledger of its own (README.md here, "Without a ledger").
ephemeral=false
if [ -z "$url" ]; then
  if [ "${GITHUB_ACTOR:-}" = "dependabot[bot]" ]; then
    notice "Dependabot's pull requests get no secrets: nothing is re-run, recorded or commented."
    exit 0
  fi
  ephemeral=true
  notice "no database-url: the re-runs are recorded in a ledger of this workflow run only (README.md of the action, Without a ledger)"
fi

# One folder per invocation (a second run of the action in the same job must not reuse the first one's ledger),
# removed at the end; the run's own ledger, if it started one, is stopped first.
tmp=$(mktemp -d "${RUNNER_TEMP:-${TMPDIR:-/tmp}}/benchbook.XXXXXX")
ledger_dir=
cleanup() {
  if [ -n "$ledger_dir" ]; then BENCHBOOK_DATA_DIR="$ledger_dir" benchbook db stop >/dev/null 2>&1 || true; fi
  rm -rf "$tmp"
}
trap cleanup EXIT

# The summary to the job summary and, unless comment: false, to the pull request's one comment ($2: update-only
# keeps an earlier comment current without starting one). gh gets only the GitHub token.
post_summary() {
  if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then cat "$1" >> "$GITHUB_STEP_SUMMARY"; fi
  if [ "${BB_COMMENT:-true}" = true ]; then
    local comment=(pr comment --repo "$GITHUB_REPOSITORY" --pr "$BB_PR" --body-file "$1")
    if [ -n "${BB_COMMENT_AUTHOR:-}" ]; then comment+=(--author "$BB_COMMENT_AUTHOR"); fi
    if [ "${2:-}" = update-only ]; then comment+=(--update-only); fi
    env -u BENCHBOOK_DATABASE_URL -u BENCHBOOK_TOKEN GH_TOKEN="$gh_token" benchbook "${comment[@]}" \
      || fail "could not post the summary comment: the workflow needs permissions: pull-requests: write"
  fi
}

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
decide=(--no-decide)
if [ "$ephemeral" = true ]; then
  # A ledger of this run only, created below once the checks are known to exist; BENCHBOOK_AGENT is set after init.
  # The pre-rename names too: benchbook still reads them, and one would lead back to a shared ledger.
  unset BENCHBOOK_DATABASE_URL BENCHBOOK_TOKEN BENCHBOOK_AGENT ASSAY_DATABASE_URL ASSAY_TOKEN ASSAY_AGENT ASSAY_DATA_DIR
  if [ "${BB_DECIDE:-false}" = true ] || [ -n "$token" ]; then
    warn "decide and token are used only with a shared ledger (database-url); this run records in a ledger of its own and decides nothing"
  fi
else
  export BENCHBOOK_DATABASE_URL="$url"
  if [ -n "$token" ]; then
    # a token is one agent: it authors every record (BENCHBOOK_AGENT would have to name the same agent)
    export BENCHBOOK_TOKEN="$token"
    unset BENCHBOOK_AGENT
  else
    # owner connection: the name is self-declared, one per workflow run
    export BENCHBOOK_AGENT="ci:${GITHUB_RUN_ID:?}"
    unset BENCHBOOK_TOKEN
  fi
fi
unset url token
if [ -n "${BB_PROJECT:-}" ]; then export BENCHBOOK_PROJECT="$BB_PROJECT"; fi

if [ "$ephemeral" != true ] && [ "${BB_DECIDE:-false}" = true ]; then
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

# --- a ledger of this run only ---------------------------------------------------------------------------------
whole=()
reported_by=
if [ "$ephemeral" = true ]; then
  # The checks come from the config committed at the merge-base only (check verify reads them there), so a pull
  # request cannot choose its own checks; without one there is nothing to re-run.
  merge_base=$(git merge-base "$BB_BASE_SHA" "$BB_HEAD_SHA") || fail "the pull request's base and head have no merge-base"
  if ! git cat-file -e "$merge_base:.benchbook/config.toml" 2>/dev/null \
      && ! git cat-file -e "$merge_base:.assay/config.toml" 2>/dev/null; then
    notice "no database-url and no config committed at the merge-base, so there are no checks to re-run. Commit the config on the base branch: git add -f .benchbook/config.toml (docs/code.md, Set up)."
    # an earlier comment (from before the base changed) must not stand as this head's result
    printf '%s\n### benchbook: completions in this pull request\n\n%s\n' '<!-- benchbook-summary -->' \
      "This run did not re-run anything: there is no \`database-url\` and no \`.benchbook/config.toml\` committed at the merge-base of ${BB_BASE_SHA:0:12} and ${BB_HEAD_SHA:0:12}, so no checks are declared there. An earlier result in this comment is not about this head; it is not re-run." \
      > "$tmp/benchbook-summary.md"
    post_summary "$tmp/benchbook-summary.md" update-only
    exit 0
  fi
  ledger_dir="$tmp/benchbook-ledger"
  export BENCHBOOK_DATA_DIR="$ledger_dir"
  # init in an empty repository: in the checkout it would rewrite config.toml and pin the pull request's own
  # checks, which check verify falls back to for a piece whose merge-base has no config.
  project=${BB_PROJECT:-pr-$BB_PR}
  mkdir -p "$tmp/benchbook-init" && git -C "$tmp/benchbook-init" init -q \
    || fail "could not create this run's ledger"
  (cd "$tmp/benchbook-init" && env -u BENCHBOOK_AGENT benchbook init --project "$project" --pack code --yes \
    < /dev/null > /dev/null) || fail "could not create this run's ledger"
  export BENCHBOOK_PROJECT="$project" BENCHBOOK_AGENT="ci:${GITHUB_RUN_ID:?}"
  whole=(--whole-as "pr-$BB_PR")
  reported_by=${BB_PR_AUTHOR:-pr-author}
fi

# --- re-run each piece -----------------------------------------------------------------------------------------
failed=0
benchbook pr pieces --base "$BB_BASE_SHA" --head "$BB_HEAD_SHA" --claims ${whole[@]+"${whole[@]}"} \
  > "$tmp/benchbook-pieces.tsv" \
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
  elif [ -n "$reported_by" ]; then
    args+=(--reported-by "$reported_by")  # this run's ledger: the pull request's author reported it
  fi
  echo "::group::benchbook check verify ${sha:0:12} (piece $piece)"
  benchbook "${args[@]}" ${decide[@]+"${decide[@]}"} < /dev/null || failed=1
  echo "::endgroup::"
done < "$tmp/benchbook-pieces.tsv"

# --- the summary -----------------------------------------------------------------------------------------------
if ! benchbook pr summary --base "$BB_BASE_SHA" --head "$BB_HEAD_SHA" --run-url "$run_url" ${whole[@]+"${whole[@]}"} \
  > "$tmp/benchbook-summary.md"
then
  fail "could not write the summary"
fi
if [ "$ephemeral" = true ]; then
  printf '\n%s\n' "This run had no shared ledger (no \`database-url\`), so the re-runs were recorded in a ledger of this workflow run only. Discarded approaches and findings from agent sessions do not show here." \
    >> "$tmp/benchbook-summary.md"
fi
# no piece: keep an earlier comment current, but do not start one on every pull request
if [ -s "$tmp/benchbook-pieces.tsv" ]; then post_summary "$tmp/benchbook-summary.md"
else post_summary "$tmp/benchbook-summary.md" update-only; fi
exit "$failed"
