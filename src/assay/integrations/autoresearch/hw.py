"""Hardware class for partitions (§6.2): 'NVIDIA H100 80GB HBM3' -> 'h100-80gb'."""
from __future__ import annotations

import os
import platform
import re
import socket
import subprocess
import time

from ...errors import AssayError

VENDOR = {"nvidia", "geforce", "tesla", "amd", "radeon", "intel", "corporation", "graphics", "apple", "gpu"}
MEMORY_KIND = {"hbm", "hbm2", "hbm2e", "hbm3", "hbm3e", "gddr5", "gddr5x", "gddr6", "gddr6x", "gddr7", "ddr4", "ddr5"}
_SLUG = re.compile(r"[^a-z0-9]+")


def slug(s: str) -> str:
    return _SLUG.sub("-", s.lower()).strip("-")


def gpu_class(name: str, memory_mib: float | None = None) -> str:
    """Lower-case, drop vendor and memory-type words, keep model and form factor, end with '<N>gb'."""
    tokens = [t for t in _SLUG.sub(" ", name.lower()).split() if t]
    mem = None
    kept: list[str] = []
    for t in tokens:
        m = re.fullmatch(r"(\d+)gb", t)
        if m:
            mem = int(m.group(1))
        elif t not in VENDOR and t not in MEMORY_KIND:
            kept.append(t)
    if mem is None and memory_mib:
        mem = round(memory_mib / 1024)
    if mem:
        kept.append(f"{mem}gb")
    return "-".join(kept) or "unknown-gpu"


def _first_visible_device(env: dict[str, str]) -> str | None:
    cvd = env.get("CUDA_VISIBLE_DEVICES")
    if cvd is None:
        return None
    first = cvd.split(",")[0].strip()
    return first or None


def detect_nvidia(env: dict[str, str] | None = None) -> str | None:
    env = dict(os.environ if env is None else env)
    cmd = ["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"]
    dev = _first_visible_device(env)
    if dev is not None:
        cmd += ["-i", dev]
    out, err = None, ""
    for attempt in range(3):
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=15, check=True).stdout
            break
        except FileNotFoundError:
            return None  # no NVIDIA tooling on this machine
        except subprocess.CalledProcessError as e:
            msg = (e.stderr or e.stdout or "").strip()
            err = f"exit {e.returncode}: {msg.splitlines()[-1] if msg else ''}"[:300]
        except (OSError, subprocess.SubprocessError) as e:
            err = str(e)[:300]
        if attempt < 2:
            time.sleep(1 + attempt)
    if out is None:
        # A transient failure must not put runs into an unknown-<host> partition (A9).
        raise AssayError(f"nvidia-smi failed ({err}); fix it or pass --hw / set ASSAY_AR_HW to the GPU class")
    line = next((ln for ln in out.splitlines() if ln.strip()), None)
    if line is None:
        return None
    name, _, mem = line.rpartition(",") if "," in line else (line, "", "")
    try:
        mib = float(mem.strip().split()[0]) if mem.strip() else None
    except ValueError:
        mib = None
    return gpu_class(name.strip(), mib)


def detect_macos() -> str | None:
    if platform.system() != "Darwin":
        return None
    try:
        out = subprocess.run(["sysctl", "-n", "machdep.cpu.brand_string"], capture_output=True, text=True,
                             timeout=5, check=True).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    return slug(out) or None


def detect(override: str | None = None, env: dict[str, str] | None = None) -> str:
    """--hw, then ASSAY_AR_HW, then nvidia-smi, then macOS sysctl, then unknown-<hostname>. An installed
    nvidia-smi that fails raises AssayError instead of falling through."""
    env = dict(os.environ if env is None else env)
    value = override or env.get("ASSAY_AR_HW")
    if value:
        return slug(value)
    return detect_nvidia(env) or detect_macos() or "unknown-" + (slug(socket.gethostname()) or "host")
