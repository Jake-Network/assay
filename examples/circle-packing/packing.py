"""Configurable packing heuristics used by the scripted swarm (and handy for LLM agents to start from).

config keys: init (grid|hex|random|lattice_perturb), optimizer (slsqp|penalty), iters (int),
perturb (float, noise added to the initial centres). The seed drives every random choice.
"""
from __future__ import annotations

import math
from typing import Any

import numpy as np

N = 26


def initial(init: str, perturb: float, rng: np.random.Generator, n: int = N) -> np.ndarray:
    if init == "grid":
        cols = math.ceil(math.sqrt(n))
        rows = math.ceil(n / cols)
        pts = [((i % cols + 0.5) / cols, (i // cols + 0.5) / rows) for i in range(n)]
        r0 = 0.5 / max(cols, rows)
    elif init in ("hex", "lattice_perturb"):
        pts = []
        row_counts = [5, 6, 5, 5, 5] if n == 26 else [math.ceil(math.sqrt(n))] * math.ceil(math.sqrt(n))
        h = 1 / len(row_counts)
        for ri, cnt in enumerate(row_counts):
            off = 0.5 / cnt
            for c in range(cnt):
                pts.append((off + c / cnt, h * (ri + 0.5)))
        pts = pts[:n]
        r0 = 0.08
        if init == "lattice_perturb":
            perturb = max(perturb, 0.02)
    elif init == "random":
        pts = list(map(tuple, rng.uniform(0.1, 0.9, size=(n, 2))))
        r0 = 0.02
    else:
        raise ValueError(f"unknown init {init!r}")
    xy = np.array(pts, dtype=float) + rng.normal(0, perturb, size=(n, 2))
    xy = np.clip(xy, 0.02, 0.98)
    return np.concatenate([xy[:, 0], xy[:, 1], np.full(n, r0)])


def _pairs(n: int) -> tuple[np.ndarray, np.ndarray]:
    return np.triu_indices(n, 1)


def repair(z: np.ndarray, n: int = N) -> np.ndarray:
    """Shrink radii until the packing is strictly feasible."""
    x, y, r = z[:n].copy(), z[n:2 * n].copy(), np.maximum(z[2 * n:].copy(), 1e-6)
    x, y = np.clip(x, 1e-6, 1 - 1e-6), np.clip(y, 1e-6, 1 - 1e-6)
    r = np.minimum.reduce([r, x, 1 - x, y, 1 - y])
    i, j = _pairs(n)
    for _ in range(200):
        d = np.hypot(x[i] - x[j], y[i] - y[j])
        over = r[i] + r[j] - d
        bad = over > 0
        if not bad.any():
            break
        scale = np.where(bad, d / (r[i] + r[j]), 1.0)
        for a, b, s in zip(i[bad], j[bad], scale[bad], strict=True):
            r[a] *= s * (1 - 1e-12)
            r[b] *= s * (1 - 1e-12)
    return np.concatenate([x, y, np.maximum(r * (1 - 1e-12), 1e-9)])


def slsqp(z0: np.ndarray, iters: int, n: int = N) -> tuple[np.ndarray, bool, str]:
    from scipy.optimize import minimize

    i, j = _pairs(n)
    m = len(i)

    def cons(z: np.ndarray) -> np.ndarray:
        x, y, r = z[:n], z[n:2 * n], z[2 * n:]
        b = np.concatenate([x - r, 1 - x - r, y - r, 1 - y - r])
        p = (x[i] - x[j]) ** 2 + (y[i] - y[j]) ** 2 - (r[i] + r[j]) ** 2
        return np.concatenate([b, p])

    def jac(z: np.ndarray) -> np.ndarray:
        x, y, r = z[:n], z[n:2 * n], z[2 * n:]
        J = np.zeros((4 * n + m, 3 * n))
        e = np.arange(n)
        J[e, e], J[e, 2 * n + e] = 1, -1
        J[n + e, e], J[n + e, 2 * n + e] = -1, -1
        J[2 * n + e, n + e], J[2 * n + e, 2 * n + e] = 1, -1
        J[3 * n + e, n + e], J[3 * n + e, 2 * n + e] = -1, -1
        k = 4 * n + np.arange(m)
        dx, dy, sr = x[i] - x[j], y[i] - y[j], r[i] + r[j]
        J[k, i], J[k, j] = 2 * dx, -2 * dx
        J[k, n + i], J[k, n + j] = 2 * dy, -2 * dy
        J[k, 2 * n + i], J[k, 2 * n + j] = -2 * sr, -2 * sr
        return J

    res = minimize(lambda z: -z[2 * n:].sum(), z0, jac=lambda z: np.concatenate([np.zeros(2 * n), -np.ones(n)]),
                   constraints=[{"type": "ineq", "fun": cons, "jac": jac}], method="SLSQP",
                   bounds=[(0, 1)] * (2 * n) + [(0, 0.5)] * n, options={"maxiter": iters, "ftol": 1e-12})
    return res.x, bool(res.success), str(res.message)


def penalty(z0: np.ndarray, iters: int, rng: np.random.Generator, n: int = N) -> np.ndarray:
    """Plain gradient ascent on sum(r) minus a quadratic overlap penalty; crude on purpose."""
    z = z0.copy()
    i, j = _pairs(n)
    lr, mu = 0.002, 10.0
    for t in range(iters):
        x, y, r = z[:n], z[n:2 * n], z[2 * n:]
        g = np.zeros_like(z)
        g[2 * n:] -= 1
        for v, s in ((r - x, (1, 0)), (x + r - 1, (-1, 0)), (r - y, (0, 1)), (y + r - 1, (0, -1))):
            a = np.maximum(v, 0) * 2 * mu
            g[:n] += -s[0] * a
            g[n:2 * n] += -s[1] * a
            g[2 * n:] += a
        dx, dy = x[i] - x[j], y[i] - y[j]
        d = np.hypot(dx, dy) + 1e-12
        o = np.maximum(r[i] + r[j] - d, 0) * 2 * mu
        np.add.at(g, i, -o * dx / d)
        np.add.at(g, j, o * dx / d)
        np.add.at(g, n + i, -o * dy / d)
        np.add.at(g, n + j, o * dy / d)
        np.add.at(g, 2 * n + i, o)
        np.add.at(g, 2 * n + j, o)
        z = z - lr * g
        if t % 200 == 199:
            mu *= 1.5
    return z


def solve(config: dict[str, Any], seed: int) -> tuple[list[list[float]], dict[str, Any]]:
    rng = np.random.default_rng(seed)
    z = initial(config["init"], float(config.get("perturb", 0.0)), rng)
    info: dict[str, Any] = {}
    if config["optimizer"] == "slsqp":
        z, ok, msg = slsqp(z, int(config["iters"]))
        z = repair(z)
        info["converged"], info["message"] = ok, msg
    elif config["optimizer"] == "penalty":
        z = repair(penalty(z, int(config["iters"]), rng))
    else:
        raise ValueError(f"unknown optimizer {config['optimizer']!r}")
    circles = [[float(z[k]), float(z[N + k]), float(z[2 * N + k])] for k in range(N)]
    return circles, info
