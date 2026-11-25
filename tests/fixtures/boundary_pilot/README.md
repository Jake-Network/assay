# Guarded test diffs from the boundary pilot (2026-11-25)

`git diff 3ac227a <commit> -- tests/` in arm F's boltons checkout
(`~/benchbook-bench/boundary-pilot-boltons-F-try1/boltons/F-1/work`), copied as text: 3ac227a is the task's
`boundary-base`, and the three commits are the pieces whose re-run passed but was sent to a person as "tests
weakened" (bench/results/2026-11-25-boundary-pilot/). The checkout itself is not copied.
`tests/test_mutations.py::test_pilot_import_rewrites_are_appended` applies each diff to a skeleton of the base files
(the diff's context lines, nothing else of boltons) and expects every changed file to be added or appended.
