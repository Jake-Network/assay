"""§18.3 scenario 3: a worker dies mid-task; its lease expires and another worker takes over."""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time

from benchbook import Ledger

WORKER = """
import sys, time
from benchbook import Ledger
lg = Ledger.open(project=sys.argv[1], agent="doomed")
t = lg.next_task(lease_seconds=2)
lg.record_experiment({"partial": True}, {"score": 1.0}, task_id=t.id)
print("claimed", flush=True)
time.sleep(60)
"""


def test_killed_worker_lease_is_taken_over(lead: Ledger, db_url: str) -> None:
    [tid] = lead.create_tasks([{"title": "long job"}])
    p = subprocess.Popen([sys.executable, "-c", WORKER, lead.project_id], stdout=subprocess.PIPE, text=True,
                         env={**os.environ, "BENCHBOOK_DATABASE_URL": db_url})
    assert p.stdout is not None and p.stdout.readline().strip() == "claimed"
    p.send_signal(signal.SIGKILL)
    p.wait()
    other = lead.as_agent("rescuer", "worker")
    assert other.next_task() is None  # lease still valid
    time.sleep(2.5)
    t = other.next_task()
    assert t is not None and t.id == tid and t.attempts == 2
    events = [e["event"] for e in lead.get(tid)["events"]]
    assert events[-2:] == ["expired", "claimed"]
    # the dead worker's result survived
    assert lead.status()["experiments"]["total"] == 1
