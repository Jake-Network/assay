"""Seed solution: a plain 6x5 grid with 26 equal circles. Prints {"circles": [...]} (sum_radii ~2.17)."""
import json

N = 26
cols, rows = 6, 5
r = 0.5 / cols  # limited by the 6 columns
circles = []
for k in range(N):
    i, j = k % cols, k // cols
    circles.append([(2 * i + 1) * r, (2 * j + 1) * (0.5 / rows), min(r, 0.5 / rows)])
print(json.dumps({"circles": circles}))
