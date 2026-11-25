"""`python -m benchbook.hooks SPOOL [LANE]`: the spool processor a hook starts in the background
(subagent.start_processor). A directory drains that lane of the spool; a file is processed on its own."""
from __future__ import annotations

import sys
from pathlib import Path

from .subagent import LANES, RECORDS, process_file, replay

path = Path(sys.argv[1])
if path.is_dir():
    replay(path, sys.argv[2] if len(sys.argv) > 2 and sys.argv[2] in LANES else RECORDS)
else:
    print(process_file(path))
