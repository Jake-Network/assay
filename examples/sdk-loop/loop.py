"""A minimal autoresearch-style loop on the SDK: claim a task, avoid repeats, record everything.

    benchbook init --project lr-sweep --goal "Minimise val loss" --metric val_loss --direction min
    benchbook task add "Sweep learning rate" --tags lr --spec '{"config_space": {"lr": [1e-4, 3e-4, 1e-3, 3e-3]}}'
    BENCHBOOK_PROJECT=lr-sweep python examples/sdk-loop/loop.py
"""
from __future__ import annotations

import itertools
import math
import random

from benchbook import Ledger


def train(cfg: dict[str, float], seed: int) -> float:
    """Stand-in for a real training run: returns a validation loss."""
    rng = random.Random(seed)
    return 2.5 + 0.3 * abs(math.log10(cfg["lr"]) + 3.3) + rng.gauss(0, 0.01)


def main() -> None:
    lg = Ledger.open(agent="loop-1")
    while (task := lg.next_task(lease_seconds=600)) is not None:
        print(lg.brief(task.id, budget_tokens=800))
        space = task.spec.get("config_space", {"lr": [3e-4]})
        keys = sorted(space)
        for values in itertools.product(*(space[k] for k in keys)):
            cfg = dict(zip(keys, values, strict=True))
            for seed in (0, 1):
                if any(e.seed == seed for e in lg.check_tried(cfg).exact):
                    continue  # someone already ran exactly this
                try:
                    loss = train(cfg, seed)
                    r = lg.record_experiment(cfg, {"val_loss": loss}, seed=seed, task_id=task.id)
                except Exception as e:  # record failures too
                    r = lg.record_experiment(cfg, {}, run_status="error", seed=seed, notes=str(e)[:500],
                                             task_id=task.id)
                print(cfg, seed, r.outcome, "new best" if r.is_new_best else "")
            lg.heartbeat(task.id, 600)
        lg.complete_task(task.id, "done", f"swept {', '.join(keys)}")


if __name__ == "__main__":
    main()
