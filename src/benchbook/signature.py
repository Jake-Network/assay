"""Failure signatures (spec §8 P0): the one place that turns a finished check into a short string that is the same
when the same failure happens again and different when a different one does.

Per check, from the most specific source the run has:

1. JUnit XML (`junit = true`): the failing test ids and exception types. The signature names the first failing test
   in sorted order and its type; when more than one test failed it adds how many more and a hash of the whole set,
   so "A fails" and "A and B fail" are different failures and the order a parallel runner wrote them in is not.
2. TypeScript compiler output (`error TS2322` lines, plain or --pretty): the error codes and the files, the same way
   (the first (code, file) pair in sorted order, and the count and hash of the set when there are several).
3. Anything else: the exit code and a hash of the normalised last error line (`last_error_line`, `normalise`).
4. A check that timed out: `timeout`.

The attempt's signature joins the failing checks' parts in run order: `check:part; check:part`. Only that string is
stored (spec §15, §16): never the output, and not the line the hash was taken from.

The output text only exists where the check ran: `check run` and `check verify` read their log, the Bash hook reads
the tool result's error text (check_hooks.rough_signature). Those pass the output-derived part (`detail`) on; the
spool processor, which has no output, combines it with the status and exit code (`check_part`).

Standard library only, and no heavy import at module level: the Bash hook and check run load this on their fast path.
"""
from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from xml.etree.ElementTree import Element

HASH_LEN = 16  # hex characters of the last-error-line hash
SET_HASH_LEN = 8  # hex characters of the hash over a set of failing tests or compiler errors
MAX_SIGNATURE = 1000  # the attempt's whole signature (metrics schema maxLength)
MAX_PART = 400  # one check's part: a test id can be long
SEPARATOR = "; "  # between the checks of one attempt
MAX_JUNIT_BYTES = 50_000_000
MAX_JUNIT_CASES = 100_000
MAX_LOG_BYTES = 2_000_000  # the end of a log the detail is read from
WINDOW = 200  # non-empty lines, from the end, searched for the last error line

LINE_HASH = re.compile(rf"[0-9a-f]{{{HASH_LEN}}}")

# ------------------------------------------------------------------------------------------- normalisation

_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
_TIMESTAMP = re.compile(r"\b\d{4}-\d{2}-\d{2}(?:[T ]\d{1,2}:\d{2}(?::\d{2})?(?:[.,]\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?\b")
_CLOCK = re.compile(r"\b\d{1,2}:\d{2}:\d{2}(?:[.,]\d+)?\b")
_TEMP = re.compile(r"(?:/private)?/(?:tmp|var/folders|var/tmp|dev/shm)/[^\s'\"`),;\]]*"
                   r"|[A-Za-z]:\\(?:[^\\\s]+\\)*?(?:Temp|tmp)\\[^\s'\"`),;\]]*")
_UUID = re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")
_ADDRESS = re.compile(r"\b0x[0-9a-fA-F]+\b")
_HEX = re.compile(r"\b(?=[0-9a-f]*\d)(?=[0-9a-f]*[a-f])[0-9a-f]{7,}\b")
_DURATION = re.compile(r"(?<![\w.])\d+(?:\.\d+)?\s?(?:ns|µs|us|ms|s|secs?|seconds?|m|mins?|minutes?|h|hrs?|hours?)\b")
_NUMBER = re.compile(r"(?<![A-Za-z_\d])\d+(?:[.,]\d+)*(?![A-Za-z_\d])")


def strip_ansi(text: str) -> str:
    return _ANSI.sub("", text)


def normalise(line: str, root: str | Path | None = None) -> str:
    """The parts of a line that change from run to run of the same failure, replaced by placeholders: the checkout's
    own path, timestamps and clock times, temporary paths, uuids, memory addresses, long hex ids, durations, and
    then every free-standing number (line numbers, counts, ports, pids). Numbers that are part of a word stay
    (E501, TS2322, x86_64, test_case_3), and so do the words: two failures with different messages stay apart."""
    s = " ".join(strip_ansi(line).split())
    if root:
        r = str(root).rstrip("/\\")
        if r:
            s = s.replace(r + "/", "").replace(r + "\\", "").replace(r, ".")
    s = _TIMESTAMP.sub("<time>", s)
    s = _CLOCK.sub("<time>", s)
    s = _TEMP.sub("<tmp>", s)
    s = _UUID.sub("<uuid>", s)
    s = _ADDRESS.sub("<addr>", s)
    s = _HEX.sub("<hex>", s)
    s = _DURATION.sub("<dur>", s)
    return _NUMBER.sub("0", s)


# ------------------------------------------------------------------------------------------- the last error line

# A line that names a failure: an error word, an exception class, a lint or compiler rule code, a test runner's
# failure bullet. Checked case-insensitively for the words, case-sensitively for the class and rule names.
_ERROR_WORDS = re.compile(r"(?i)\b(?:errors?|fail(?:s|ed|ure|ures)?|fatal|panic\w*|exception|traceback|assert\w*|"
                          r"abort(?:ed)?|segmentation fault|denied|refused|not found|cannot|can't|could not|"
                          r"unable to|undefined|invalid|missing|mismatch\w*|unexpected)\b")
_ERROR_NAMES = re.compile(r"\w(?:Error|Exception|Failure|Exit)\b|\b[A-Z]{1,4}\d{2,5}\b|^\s*(?:[●✕✗×]|E\s{2,})")
# A location (file:line); a weaker sign, used when no line names a failure.
_LOCATION = re.compile(r"[\w./\\-]+\.[A-Za-z]\w{0,7}(?::\d+|\(\d+)")
# Lines a wrapper prints around the real failure (make, npm, yarn, pnpm, cargo, go, CI runners): they are the same for
# every failure of the check and would make every failure one.
_WRAPPER = re.compile(
    r"^(?:make(?:\[\d+\])?: \*\*\*|npm (?:ERR!|error)|error Command failed with exit code|info Visit https?://|"
    r"\s*ELIFECYCLE|ERR_PNPM_|error: test failed, to rerun pass|error: could not compile|"
    r"error: process didn't exit successfully|exit status -?\d+$|(?:Error: )?Process completed with exit code|"
    r"Command exited with non-zero status|(?:FAIL|ok)\s+\S+\s+[\d.]+s$|FAIL\s+\S+\s+\[(?:build|setup) failed\]$|"
    r"Error: Command failed:|ERROR: InvocationError|##\[error\]Process completed)",
    re.I)
# Words of a results summary ("1 failed, 2 passed in 0.12s", "Found 3 errors in 2 files", "Tests: 1 failed, 4 total"):
# a line made only of these and numbers says how many, not what.
_SUMMARY_WORDS = frozenset("""
    failed passed error errors warning warnings skipped deselected xfailed xpassed rerun reruns tests test suites suite
    snapshots snapshot total in and of found files file checked source sources ran run ok fail failure failures result
    results ignored measured filtered out time seconds s ms todo pending passing failing problems problem assertions
    examples example specs spec cases case testcases executed success successes done finished duration with took
    broken flaky errored crashed pass dur generated emitted
""".split())
# Words of a section header ("FAILURES", "Traceback (most recent call last):", "short test summary info", "error:").
_HEADER_WORDS = frozenset("""
    fail failed failure failures error errors traceback most recent call last exception exceptions short test tests
    summary info details detail output stdout stderr captured log call setup teardown warnings warning the
""".split())
_WORD = re.compile(r"[A-Za-z]+")


def _words(line: str) -> list[str]:
    return [w.lower() for w in _WORD.findall(line)]


def _is_summary(norm: str) -> bool:
    ws = _words(norm.replace("<dur>", " dur "))
    return bool(ws) and "0" in norm and all(w in _SUMMARY_WORDS for w in ws)


def _is_header(line: str) -> bool:
    ws = _words(line)
    return not ws or all(w in _HEADER_WORDS for w in ws)


def last_error_line(output: str) -> str | None:
    """The line of the output that best names the failure, searched in the last WINDOW non-empty lines from the
    end: the last line with an error word, an exception or rule name, or a failure bullet; else the last one with a
    file:line location; else the last line. Wrapper lines (make, npm, cargo, go summaries), result counts and
    section headers are skipped: they are the same whichever test failed."""
    lines = [x.strip() for x in strip_ansi(output).replace("\r", "\n").splitlines() if x.strip()][-WINDOW:]
    if not lines:
        return None
    useful = [x for x in lines if not _WRAPPER.match(x) and not _is_summary(normalise(x)) and not _is_header(x)]
    for x in reversed(useful):
        if _ERROR_WORDS.search(x) or _ERROR_NAMES.search(x):
            return x
    for x in reversed(useful):
        if _LOCATION.search(x):
            return x
    return useful[-1] if useful else lines[-1]


def line_hash(output: str, root: str | Path | None = None) -> str | None:
    """sha256 (HASH_LEN hex) of the normalised last error line; None for an empty output."""
    line = last_error_line(output)
    if line is None:
        return None
    return hashlib.sha256(normalise(line, root).encode("utf-8")).hexdigest()[:HASH_LEN]


# ------------------------------------------------------------------------------------------- sets of failures

def _set_part(items: Sequence[tuple[str, str]], fmt: str) -> str:
    """The first item in sorted order, and for several the count of the others and a hash of all of them."""
    unique = sorted(set(items))
    first = fmt.format(*unique[0])
    if len(unique) == 1:
        return first
    digest = hashlib.sha256("\n".join(f"{a}\t{b}" for a, b in unique).encode("utf-8")).hexdigest()[:SET_HASH_LEN]
    return f"{first} +{len(unique) - 1} more #{digest}"


def junit_part(failed: Sequence[tuple[str, str | None]]) -> str | None:
    """From (test id, exception type) pairs: `id:type`, or `id:type +N more #hash`."""
    if not failed:
        return None
    return _set_part([(_ADDRESS.sub("<addr>", t), k or "failed") for t, k in failed], "{}:{}")


# tsc's two formats: "src/a.ts(12,5): error TS2322: ..." and, with --pretty, "src/a.ts:12:5 - error TS2322: ...";
# an error without a file ("error TS5023: Unknown compiler option") has no location.
_TSC = re.compile(r"^(?:(?P<file>[^\s(:][^(]*?)\(\d+,\d+\)\s*:\s*|(?P<pfile>\S[^:]*?):\d+:\d+\s+-\s+)?"
                  r"error\s+(?P<code>TS\d+)\s*:")


def tsc_errors(output: str, root: str | Path | None = None) -> list[tuple[str, str]]:
    """(code, file) of every TypeScript compiler error in the output; the file relative to root, "" for none."""
    out = []
    r = str(root).rstrip("/\\") + "/" if root else None
    for line in strip_ansi(output).replace("\r", "\n").splitlines():
        m = _TSC.match(line.strip())
        if m is None:
            continue
        f = (m.group("file") or m.group("pfile") or "").strip().replace("\\", "/")
        if r and f.startswith(r):
            f = f[len(r):]
        f = _TEMP.sub("<tmp>", f.removeprefix("./"))
        out.append((m.group("code"), f))
    return out


def tsc_part(output: str, root: str | Path | None = None) -> str | None:
    """`tsc TS2322 src/a.ts`, or with several errors `tsc TS2322 src/a.ts +N more #hash`; None without tsc errors."""
    errors = tsc_errors(output, root)
    if not errors:
        return None
    return "tsc " + _set_part(errors, "{} {}").rstrip()


# ------------------------------------------------------------------------------------------- one check, one attempt

def detail(output: str, root: str | Path | None = None) -> str | None:
    """What the output alone says about a failure: the TypeScript errors when there are any, else the hash of the
    normalised last error line. The Bash hook and check run pass this on with the attempt (spool key
    last_line_hash, its name from before tsc parts existed)."""
    return tsc_part(output, root) or line_hash(output, root)


def check_part(status: str, exit_code: int | None, failed_tests: Sequence[tuple[str, str | None]] = (),
               output_detail: str | None = None) -> str | None:
    """One check's part of the signature, without its name; None when the check did not fail."""
    if status == "timeout":
        return "timeout"
    if status != "fail":
        return None
    if part := junit_part(failed_tests):
        return part
    if output_detail and not LINE_HASH.fullmatch(output_detail):
        return output_detail  # a structured part (tsc) made where the output was
    return f"exit {exit_code}:{output_detail or '-'}"


def combine(parts: Iterable[tuple[str, str | None]]) -> str | None:
    """The attempt's signature: `check:part` per failing check, in run order. None when nothing failed."""
    out = [f"{name}:{_clip(part)}" for name, part in parts if part]
    return SEPARATOR.join(out)[:MAX_SIGNATURE] or None


def _clip(part: str) -> str:
    part = part.replace(SEPARATOR, ", ")  # the separator splits a signature back into its checks
    return part if len(part) <= MAX_PART else part[:MAX_PART - 1] + "…"


def elements(signature: str | None) -> list[str]:
    """A signature split back into its `check:part` elements: two attempts fail the same way on a check when they
    share an element."""
    return [x for x in (signature or "").split(SEPARATOR) if x]


# ------------------------------------------------------------------------------------------- reading the files

def read_log(path: Path) -> str:
    """The end of a check's log, decoded leniently; "" when it cannot be read."""
    try:
        with path.open("rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - MAX_LOG_BYTES))
            return f.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


def junit_failures(path: Path) -> list[tuple[str, str | None]]:
    """(test id, exception type) of every failing or erroring testcase of a JUnit XML file, sorted by test id.
    pytest gives no type attribute; its failure text ends with "file.py:12: AssertionError"."""
    import xml.etree.ElementTree as ET

    try:
        if path.stat().st_size > MAX_JUNIT_BYTES:
            return []
        root = ET.parse(path).getroot()
    except (ET.ParseError, OSError):
        return []
    out: list[tuple[str, str | None]] = []
    for case in root.iter("testcase"):
        bad = case.find("failure")
        if bad is None:
            bad = case.find("error")
        if bad is None:
            continue
        name, cls = case.get("name") or "?", case.get("classname")
        out.append((f"{cls}::{name}" if cls else name, bad.get("type") or _exception_type(bad)))
        if len(out) >= MAX_JUNIT_CASES:
            break
    return sorted(set(out), key=lambda x: (x[0], x[1] or ""))


_TRAILING_TYPE = re.compile(r":\s*([A-Za-z_][\w.]*)\s*$")
_LEADING_TYPE = re.compile(r"^([A-Za-z_][\w.]*(?:Error|Exception|Failure|Exit))\b")


def _exception_type(el: Element) -> str | None:
    lines = [x for x in (el.text or "").splitlines() if x.strip()]
    if lines and (m := _TRAILING_TYPE.search(lines[-1])):
        return m.group(1)
    if m := _LEADING_TYPE.match(el.get("message") or ""):
        return m.group(1)
    return None
