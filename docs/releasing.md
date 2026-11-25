# Releasing

benchbook is published to PyPI by `.github/workflows/release.yml` when a `v*` tag is pushed. No maintainer machine
uploads anything and the repository stores no PyPI token: PyPI trusts the workflow itself (Trusted Publishing, OIDC).

## What a tag does

| job | what it runs | it stops the release when |
|---|---|---|
| `build` | `uv build --no-sources` on the tagged tree: the sdist, then the wheel from that sdist | the files are not exactly `benchbook-<tag without v>` (the tag is not this commit's version) |
| `smoke` | `scripts/smoke_wheel.sh` on Ubuntu and macOS with the wheel from `build` | a file is missing from the wheel, or the embedded Postgres does not install or start |
| `publish` | `uv publish` in the `pypi` environment, with `id-token: write` and nothing else | PyPI has no trusted publisher for this repository, workflow file and environment |

The version is not written in any file. hatch-vcs derives it from git: the commit tagged `v0.1.0` builds `0.1.0`, any
other commit builds `0.1.devN+g<commit>`. `benchbook --version` and the first line of `benchbook doctor` print it.

These checks run long before a tag:

- `ci.yml` has the same `smoke` job on every pull request and push to main. It builds the wheel and runs
  `uvx --from dist/*.whl benchbook doctor`, `benchbook init` in an empty directory and `doctor` again, on runners that
  have never had benchbook. This is what shows that the embedded Postgres wheel (`pixeltable-pgserver`) installs on
  both systems.
- `tests/test_packaging.py` builds both distributions and compares them with git: every tracked file of the package
  is in the wheel (SQL, packs, templates, static files, `py.typed`, the generated `_version.py`), the sdist is the
  tracked tree, and a file that only a local or global git exclude hides is reported instead of shipped.
  `tests/test_release.py` reads the two workflows: no stored token, the publishing permission in the `publish` job
  alone, both systems in the smoke run, and the names in this document equal to the ones in `release.yml`.

To run the smoke check yourself (it moves `HOME` to a temp directory, so it never touches your ledger):

```bash
uv build --no-sources && bash scripts/smoke_wheel.sh
```

## Before the first release

Every step in this section acts outside the repository and no commit undoes it. **The owner confirms each one right
before it is done.** Neither CI nor an agent working in this repository runs them. The order matters: the names in
steps 4 and 5 depend on the rename in step 3.

1. **Trademark search (once).** Search `benchbook` in the USPTO trademark search, the WIPO Global Brand Database and
   KIPRIS, for software and software services (Nice classes 9 and 42). The name was chosen from registry
   availability alone; no trademark search has been done. Write the date and the result in `DECISIONS.md`. A live
   registration for developer software under the same name is a reason to stop before step 3.

2. **Check that the names are still free.** They were on 2026-11-21. `404` means free:

   ```bash
   for url in https://pypi.org/pypi/benchbook/json https://registry.npmjs.org/benchbook \
              https://crates.io/api/v1/crates/benchbook https://api.github.com/users/benchbook; do
       curl -s -o /dev/null -A name-check -w "%{http_code} $url\n" "$url"
   done
   ```

3. **Rename the repository.**

   ```bash
   gh repo rename benchbook --repo Jake-Network/assay
   git remote set-url origin git@github.com:Jake-Network/benchbook.git
   ```

   GitHub redirects the old web and git URLs for as long as no new repository takes the old name, so existing
   clones keep working. This comes first because PyPI identifies the publisher by the repository's current name
   (step 5), and because the package's project URLs, the README install lines and
   `claude plugin marketplace add Jake-Network/benchbook` already use the new name.

4. **Reserve the name where it is not used yet.**
   - GitHub organisation `benchbook`: created in the browser (github.com/account/organizations/new, free plan).
   - npm and crates.io: a name is held by publishing a package, so reserving means publishing a placeholder that
     points at this repository (`npm publish`, `cargo publish`). Both registries may remove or reassign names that
     are held without use; read their current policy before publishing one.
   - PyPI has no reservation. The name becomes ours with the first upload, which is the release itself (below).

5. **Register the PyPI pending publisher.** On pypi.org, in *Your account → Publishing*, add a pending publisher on
   the GitHub tab:

   | field | value |
   |---|---|
   | PyPI Project Name | `benchbook` |
   | Owner | `Jake-Network` |
   | Repository name | `benchbook` (the name after step 3) |
   | Workflow name | `release.yml` |
   | Environment name | `pypi` |

   A pending publisher does not create the project or hold its name until it is used to publish, and it is
   invalidated if someone else registers the name first
   ([PyPI docs](https://docs.pypi.org/trusted-publishers/creating-a-project-through-oidc/)). Do this step and the
   first tag close together.

6. **Create the `pypi` environment** in the repository's *Settings → Environments*, and add the owner as a required
   reviewer. The `publish` job then waits for an approval click in the Actions run, after `build` and `smoke` have
   passed: that click is the confirmation right before the upload. Without a required reviewer a pushed `v*` tag
   publishes with no further question. A tag ruleset that limits who may create `v*` tags is the second guard.

## Every release

- [ ] `main` is green in CI, `smoke` included on both systems.
- [ ] `CHANGELOG.md`: the `Unreleased` headings carry the version and the date.
- [ ] `uv build --no-sources && bash scripts/smoke_wheel.sh` passes locally.
- [ ] **Owner confirms**, then tag the commit CI ran on and push only that tag:

  ```bash
  git tag -a v0.1.0 -m v0.1.0 <commit>
  git push origin v0.1.0
  ```

- [ ] Watch the `release` run; approve the `pypi` environment when `publish` asks.
- [ ] https://pypi.org/project/benchbook/ shows the version and renders the README.
- [ ] The check under "Phase 0 exit condition" passes on a machine that installs from PyPI.

A release candidate (`v0.1.0rc1`) goes through the same three jobs and is a way to rehearse the pipeline. It is a
real upload: it creates the PyPI project, and that version number is used for good. uv and pip install a
pre-release only when asked for one or when the project has no final release.

After the first release, the install lines that still say `uv tool install --from git+https://...` can use the
PyPI name (`uv tool install benchbook`, `uvx benchbook`): `README.md`, `README.ko.md`, `docs/autoresearch.md`,
`integrations/autoresearch/template/README.md` and the fix line of the PATH check in `src/benchbook/cli/doctor.py`.
Change them only once the package is on PyPI, or they point at nothing.

## Phase 0 exit condition

On a macOS machine and a Linux machine that have never had benchbook (no data directory, nothing in uv's cache),
with only uv installed:

```bash
uvx benchbook doctor      # ok: benchbook build, connect, postgres >= 15, python
mkdir bb-empty && cd bb-empty
uvx benchbook init --goal "Smoke test" --metric score --direction max
uvx benchbook doctor      # every line ok, exit code 0
uvx benchbook db stop     # the embedded server keeps running otherwise
```

The first `doctor` exits 1 on such a machine, by design of `doctor`: the database has no schema and the directory
has no project yet, and it prints the fix (`benchbook migrate`, `benchbook init`) for each. What it already proves
is that the package installed and that the embedded Postgres started. The second `doctor` is the one that passes.

CI runs these commands from the built wheel on `ubuntu-latest` and `macos-latest` for every pull request and push to main, which covers the
packaging and the Postgres dependency. It does not cover installing by name from PyPI, an Intel Mac (the `test` job
runs there, from a checkout and not from the wheel) or a Linux that is not glibc-based. The exit condition is
checked by hand on real machines after the first upload.

## When a release goes wrong

| what happened | what to do |
|---|---|
| `build` fails at "Check that the build is the tagged version" | The tag does not spell the version the way the build does (`v0.1.0-rc1` builds `0.1.0rc1`), is not a version (`vnext`), or shares its commit with another version tag. Nothing was uploaded: delete the tag and push a correct one. |
| `smoke` fails | Nothing was uploaded. Fix on `main`, delete the tag, tag the fixed commit. |
| `publish` fails with an invalid-publisher error | A field of the trusted publisher does not match (repository renamed, workflow file renamed, environment name). Correct it on PyPI and re-run the failed job: the built files are kept as the run's artifact. |
| `publish` stops halfway | Re-run the job. PyPI ignores files that are already there and identical. |
| a bad version is on PyPI | A version number is never reusable. Yank it on PyPI and release the next patch version. Do not move a tag that was published. |

## Not done yet

- PEP 740 attestations. The uv guide adds `astral-sh/attest-action` before `uv publish`; it is left out until it has
  a stable release, because it would run in the one job that holds the publishing permission.
- The workflows pin actions by major version tag, like `ci.yml`. Pinning the `publish` job's actions by commit SHA is
  the stricter choice.
