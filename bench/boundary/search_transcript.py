"""Search raw event lines from arm E's own previous sessions."""
from __future__ import annotations

import argparse
import os
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("query")
    query = parser.parse_args().query.casefold()
    if not query:
        parser.error("query must be nonempty")
    root = Path(os.environ["BENCH_BOUNDARY_PAST_EVENTS"])
    matches = 0
    for path in sorted(root.glob("**/*.jsonl")):
        with path.open(encoding="utf-8") as events:
            for lineno, line in enumerate(events, 1):
                if query not in line.casefold():
                    continue
                print(f"{path.relative_to(root)}:{lineno}: {line.rstrip()}")
                matches += 1
                if matches == 20:
                    return


if __name__ == "__main__":
    main()
