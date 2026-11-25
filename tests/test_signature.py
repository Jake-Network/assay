"""Failure signatures (PR 2-P0c, signature.py): the same failure signs the same across runs, and different failures
sign differently, with realistic outputs in both directions. Never the output text itself."""
from __future__ import annotations

from pathlib import Path

import pytest

from benchbook.checks import signature
from benchbook.checks.rerun import CheckResult, failure_signature
from benchbook.hooks import checks as check_hooks

# ------------------------------------------------------------------------------------------- realistic outputs
# Each case: a failure, the same failure in another run (other timestamps, durations, line numbers, addresses,
# temporary paths, counts, pids), and a different failure of the same check.

PYTEST_1 = """============================= test session starts ==============================
platform linux -- Python 3.12.3, pytest-8.3.2, pluggy-1.5.0
rootdir: /home/u/repo
collected 14 items

tests/media/test_upload.py ..F.........                                  [100%]

=================================== FAILURES ===================================
_________________________ test_reupload_same_file __________________________

tmp_path = PosixPath('/tmp/pytest-of-u/pytest-41/test_reupload_same_file0')

    def test_reupload_same_file(tmp_path):
>       assert upload(tmp_path / "a.png") == upload(tmp_path / "a.png")
E       AssertionError: assert <Blob object at 0x7f3a2c1d9e50> == <Blob object at 0x7f3a2c1d9f10>

tests/media/test_upload.py:41: AssertionError
=========================== short test summary info ============================
FAILED tests/media/test_upload.py::test_reupload_same_file - AssertionError: assert {left} == {right}
========================= 1 failed, 13 passed in 2.31s =========================
""".format(left="<Blob object at 0x7f3a2c1d9e50>", right="<Blob object at 0x7f3a2c1d9f10>")
PYTEST_1_AGAIN = (PYTEST_1.replace("pytest-41", "pytest-57").replace("0x7f3a2c1d9e50", "0x7fb81200aa10")
                  .replace("0x7f3a2c1d9f10", "0x7fb81200ab90").replace(":41:", ":44:")
                  .replace("1 failed, 13 passed in 2.31s", "1 failed, 15 passed in 3.08s")
                  .replace("collected 14 items", "collected 16 items"))
PYTEST_2 = PYTEST_1.replace("test_reupload_same_file", "test_upload_rejects_empty_file").replace(
    "AssertionError: assert <Blob object at 0x7f3a2c1d9e50> == <Blob object at 0x7f3a2c1d9f10>",
    "ValueError: empty upload")

GO_1 = """=== RUN   TestAdd
    add_test.go:12: Add(1, 2) = 4, want 3
--- FAIL: TestAdd (0.00s)
=== RUN   TestSub
--- PASS: TestSub (0.00s)
FAIL
exit status 1
FAIL	example.com/calc	0.004s
"""
GO_1_AGAIN = GO_1.replace("0.004s", "0.011s").replace("add_test.go:12", "add_test.go:14")
GO_2 = GO_1.replace("TestAdd", "TestMul").replace("add_test.go:12: Add(1, 2) = 4, want 3",
                                                   "mul_test.go:9: Mul(2, 3) = 5, want 6")

TRACEBACK_1 = """2026-11-21T09:12:44.123Z INFO starting migration run pid=48213
Traceback (most recent call last):
  File "/home/u/repo/scripts/migrate.py", line 88, in <module>
    main()
  File "/home/u/repo/scripts/migrate.py", line 71, in main
    conn = connect(settings.DATABASE_URL)
psycopg.OperationalError: connection to server at "127.0.0.1", port 5432 failed: Connection refused
"""
TRACEBACK_1_AGAIN = (TRACEBACK_1.replace("2026-11-21T09:12:44.123Z", "2026-11-22T17:03:09.871Z")
                     .replace("pid=48213", "pid=51002").replace("line 88", "line 90").replace("line 71", "line 73"))
TRACEBACK_2 = TRACEBACK_1.replace(
    'psycopg.OperationalError: connection to server at "127.0.0.1", port 5432 failed: Connection refused',
    'KeyError: \'DATABASE_URL\'')

JEST_1 = """FAIL src/media/upload.test.ts (5.123 s)
  upload
    ✕ keeps one blob for the same file (12 ms)
    ✓ rejects an empty file (3 ms)

  ● upload › keeps one blob for the same file

    expect(received).toBe(expected) // Object.is equality

    Expected: 1
    Received: 2

      40 |   await upload(file);
      41 |   await upload(file);
    > 42 |   expect(store.count()).toBe(1);
         |                         ^

      at Object.<anonymous> (src/media/upload.test.ts:42:25)

Test Suites: 1 failed, 3 passed, 4 total
Tests:       1 failed, 21 passed, 22 total
Snapshots:   0 total
Time:        6.812 s
Ran all test suites.
"""
JEST_1_AGAIN = JEST_1.replace("5.123 s", "4.020 s").replace("12 ms", "9 ms").replace("6.812 s", "5.115 s").replace(
    "21 passed, 22 total", "23 passed, 24 total")
JEST_2 = JEST_1.replace("keeps one blob for the same file", "rejects a file over the size limit")

MYPY_1 = ('src/app/media/upload.py:57: error: Incompatible return value type (got "int", expected "str")'
          '  [return-value]\nFound 1 error in 1 file (checked 212 source files)\n')
MYPY_1_AGAIN = MYPY_1.replace(":57:", ":61:").replace("212 source", "215 source")
MYPY_2 = MYPY_1.replace('Incompatible return value type (got "int", expected "str")  [return-value]',
                        'Name "blob_id" is not defined  [name-defined]')

MAKE_1 = """cc -O2 -c src/store.c -o build/store.o
src/store.c:118:9: error: use of undeclared identifier 'blob_count'
  118 |         blob_count++;
      |         ^
1 error generated.
make: *** [Makefile:23: build/store.o] Error 1
"""
MAKE_1_AGAIN = MAKE_1.replace("118", "121").replace("Makefile:23", "Makefile:25")
MAKE_2 = MAKE_1.replace("use of undeclared identifier 'blob_count'", "incompatible pointer to integer conversion")

NPM_1 = """> media@1.4.0 test
> node scripts/smoke.js

[09:12:44.120] fetching http://127.0.0.1:41873/health
Error: expected status 200 from /health, got 503
npm ERR! Lifecycle script `test` failed with error:
npm ERR! Error: command failed
npm ERR!   in workspace: media@1.4.0
npm ERR!   at location: /tmp/ci-run-8812/media
"""
NPM_1_AGAIN = NPM_1.replace("09:12:44.120", "17:40:02.004").replace("41873", "39211").replace("8812", "9013")
NPM_2 = NPM_1.replace("Error: expected status 200 from /health, got 503",
                      "TypeError: Cannot read properties of undefined (reading 'blobs')")

GENERIC = [(PYTEST_1, PYTEST_1_AGAIN, PYTEST_2), (GO_1, GO_1_AGAIN, GO_2),
           (TRACEBACK_1, TRACEBACK_1_AGAIN, TRACEBACK_2), (JEST_1, JEST_1_AGAIN, JEST_2),
           (MYPY_1, MYPY_1_AGAIN, MYPY_2), (MAKE_1, MAKE_1_AGAIN, MAKE_2), (NPM_1, NPM_1_AGAIN, NPM_2)]
IDS = ["pytest", "go", "traceback", "jest", "mypy", "make", "npm"]


@pytest.mark.parametrize(("first", "again", "other"), GENERIC, ids=IDS)
def test_one_failure_signs_the_same_and_another_signs_differently(first: str, again: str, other: str) -> None:
    a, b, c = (signature.check_part("fail", 1, (), signature.detail(x)) for x in (first, again, other))
    assert a is not None and a.startswith("exit 1:")
    assert a == b, (signature.last_error_line(first), signature.last_error_line(again))
    assert a != c, (signature.last_error_line(first), signature.last_error_line(other))
    assert signature.check_part("fail", 2, (), signature.detail(first)) != a  # the exit code is part of it


@pytest.mark.parametrize(("output", "line"), [
    (PYTEST_1, "FAILED tests/media/test_upload.py::test_reupload_same_file"),  # not the result count after it
    (GO_1, "--- FAIL: TestAdd"),  # not go's package summary, "FAIL" or "exit status 1"
    (JEST_1, "● upload › keeps one blob"),  # not the suite totals
    (MAKE_1, "src/store.c:118:9: error:"),  # not make's "*** ... Error 1" nor clang's count
    (NPM_1, "Error: expected status 200"),  # not npm's own lines
    (MYPY_1, "src/app/media/upload.py:57: error:"),
])
def test_the_last_error_line_skips_wrappers_counts_and_headers(output: str, line: str) -> None:
    found = signature.last_error_line(output)
    assert found is not None and found.startswith(line), found


def test_normalisation() -> None:
    n = signature.normalise
    assert n("done at 2026-11-21T09:12:44.123+09:00 in 1.25s (pid 4411)") == "done at <time> in <dur> (pid 0)"
    assert n("wrote /tmp/pytest-of-u/pytest-3/x0/out.json, 0x7ffd3a2b") == "wrote <tmp>, <addr>"
    assert n("job 3f2a91c0-1b2c-4d5e-8f90-a1b2c3d4e5f6 at commit 1a2b3c4d5e6f") == "job <uuid> at commit <hex>"
    assert n("x.py:12:5: E501 line too long (130 > 120)") == "x.py:0:0: E501 line too long (0 > 0)"  # E501 stays
    assert n("TS2322 in test_case_3 on x86_64") == "TS2322 in test_case_3 on x86_64"  # numbers inside words stay
    assert n("/home/u/repo/src/a.py:3: boom", "/home/u/repo") == "src/a.py:0: boom"  # the checkout's own path
    assert n("\x1b[31mFAILED\x1b[0m   x") == "FAILED x"


def test_a_summary_alone_still_signs() -> None:
    """With nothing but a count to go on, the count line is the line (better than no signature at all)."""
    assert signature.last_error_line("1 failed, 2 passed in 0.12s") == "1 failed, 2 passed in 0.12s"
    assert signature.line_hash("") is None and signature.check_part("fail", 3, (), None) == "exit 3:-"


# ------------------------------------------------------------------------------------------- JUnit and tsc

def test_junit_part_names_the_first_failing_test_and_the_set() -> None:
    a = [("tests.media.test_upload::test_reupload", "AssertionError")]
    assert signature.junit_part(a) == "tests.media.test_upload::test_reupload:AssertionError"
    two = [("tests.b::test_y", None), ("tests.a::test_x", "KeyError")]
    part = signature.junit_part(two)
    assert part is not None and part.startswith("tests.a::test_x:KeyError +1 more #")
    assert signature.junit_part(list(reversed(two))) == part  # the order a parallel runner wrote does not count
    assert signature.junit_part([*two, ("tests.c::test_z", "KeyError")]) != part  # A+B is not A+B+C
    assert signature.junit_part([two[1], ("tests.c::test_z", None)]) != part  # nor A+C
    assert signature.junit_part([("tests.a::test_x", "ValueError")]) != signature.junit_part([two[1]])  # nor A:other
    obj = [("t::test_x[<Blob object at 0x7f3a2c1d9e50>]", "AssertionError")]
    assert signature.junit_part(obj) == signature.junit_part([("t::test_x[<Blob object at 0x7fffff000010>]",
                                                                "AssertionError")])


def test_junit_files_are_read_whole_and_sorted(tmp_path: Path) -> None:
    cases = "".join(f'<testcase classname="tests.t{i % 7}" name="test_{i}"><failure message="x">t.py:{i}: '
                    f'AssertionError</failure></testcase>' for i in range(120))
    (tmp_path / "j.xml").write_text(f'<testsuites><testsuite>{cases}<testcase classname="ok" name="test_ok"/>'
                                    '</testsuite></testsuites>')
    failed = signature.junit_failures(tmp_path / "j.xml")
    assert len(failed) == 120 and failed == sorted(failed) and failed[0] == ("tests.t0::test_0", "AssertionError")
    part = signature.check_part("fail", 1, failed, "0" * 16)
    assert part is not None and part.startswith("tests.t0::test_0:AssertionError +119 more #")


TSC_1 = """src/media/upload.ts(42,7): error TS2322: Type 'string' is not assignable to type 'number'.
src/media/store.ts(9,3): error TS2304: Cannot find name 'blobCount'.

Found 2 errors in 2 files.

Errors  Files
     1  src/media/upload.ts:42
     1  src/media/store.ts:9
"""
TSC_PRETTY = (
    "\x1b[96msrc/media/upload.ts\x1b[0m:\x1b[93m44\x1b[0m:\x1b[93m7\x1b[0m - "
    "\x1b[91merror\x1b[0m\x1b[90m TS2322: \x1b[0m"
    "Type 'string' is not assignable to type 'number'.\n\n44   count = name;\n     ~~~~~\n\n"
    "\x1b[96msrc/media/store.ts\x1b[0m:\x1b[93m11\x1b[0m:\x1b[93m3\x1b[0m - "
    "\x1b[91merror\x1b[0m\x1b[90m TS2304: \x1b[0m"
    "Cannot find name 'blobCount'.\n\nFound 2 errors in 2 files.\n")


def test_tsc_signs_by_error_code_and_file() -> None:
    plain = signature.detail(TSC_1)
    assert plain is not None and plain.startswith("tsc TS2304 src/media/store.ts +1 more #")
    assert signature.detail(TSC_PRETTY) == plain  # --pretty, colours and other line numbers: the same failure
    assert signature.check_part("fail", 2, (), plain) == plain  # no exit code: the codes and files are the failure
    one = signature.detail("src/media/upload.ts(42,7): error TS2322: Type 'string' is not assignable.\n")
    assert one == "tsc TS2322 src/media/upload.ts"
    assert signature.detail("src/media/upload.ts(42,7): error TS2345: Argument of type 'x'.\n") != one  # other code
    assert signature.detail("src/media/other.ts(42,7): error TS2322: Type 'string'.\n") != one  # other file
    assert signature.detail("error TS5023: Unknown compiler option 'strictest'.\n") == "tsc TS5023"
    root = "/tmp/benchbook-verify-x1/worktree"
    assert signature.detail(f"{root}/src/media/upload.ts(42,7): error TS2322: x\n", root) == one


# ------------------------------------------------------------------------------------------- the attempt's signature

def test_attempt_signature_joins_the_failing_checks_and_splits_back() -> None:
    rs = [CheckResult("unit", "fail", 1, failed_tests=[("t::test_a", "AssertionError")]),
          CheckResult("lint", "ok", 0), CheckResult("types", "fail", 2, last_line_hash="tsc TS2322 a.ts"),
          CheckResult("slow", "timeout"), CheckResult("e2e", "fail", 3, last_line_hash="ab" * 8),
          CheckResult("late", "not_run")]
    sig = failure_signature(rs)
    assert sig == f"unit:t::test_a:AssertionError; types:tsc TS2322 a.ts; slow:timeout; e2e:exit 3:{'ab' * 8}"
    assert signature.elements(sig) == ["unit:t::test_a:AssertionError", "types:tsc TS2322 a.ts", "slow:timeout",
                                       f"e2e:exit 3:{'ab' * 8}"]
    # a part made where the output was wins over rebuilding it
    assert failure_signature([CheckResult("unit", "fail", 1, signature="x:y")]) == "unit:x:y"
    assert failure_signature([CheckResult("unit", "ok", 0)]) is None
    assert signature.combine([("a", "p; q")]) == "a:p, q"  # the separator never appears inside a part


def test_the_output_text_is_never_in_a_signature() -> None:
    secret = "SECRET-TOKEN-ghp_abcdefghijklmnop leaked in an error: boom"
    sig = signature.combine([("unit", signature.check_part("fail", 1, (), signature.detail(f"ok\n{secret}\n")))])
    assert sig is not None and "SECRET" not in sig and "boom" not in sig


def test_the_hook_signs_through_the_same_module() -> None:
    assert check_hooks.rough_signature(PYTEST_1) == signature.detail(PYTEST_1)
    assert check_hooks.rough_signature(TSC_1) == signature.detail(TSC_1)
    assert check_hooks.rough_signature("") is None
