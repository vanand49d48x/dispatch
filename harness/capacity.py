"""Decide how many agents can run at once on this machine (container-aware: reads cgroup limits)."""
from __future__ import annotations

import os
from pathlib import Path

GB = 1024 ** 3
PER_AGENT_BYTES = int(2.5 * GB)   # Claude Code process + Next.js build / Playwright headroom
MIN_FREE_TO_START = int(2 * GB)   # don't start another agent below this much free memory
HARD_CAP = 8                      # beyond this, shared-repo conflicts and API rate limits dominate


def _read(path: str) -> str | None:
    try:
        return Path(path).read_text().strip()
    except OSError:
        return None


def total_memory() -> int:
    host = 0
    meminfo = _read("/proc/meminfo")
    if meminfo:
        for line in meminfo.splitlines():
            if line.startswith("MemTotal:"):
                host = int(line.split()[1]) * 1024
    if not host:
        try:
            host = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
        except (ValueError, OSError, AttributeError):
            host = 8 * GB
    for p in ("/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory/memory.limit_in_bytes"):
        v = _read(p)
        if v and v.isdigit() and int(v) < host:
            return int(v)
    return host


def available_memory() -> int:
    avail = None
    meminfo = _read("/proc/meminfo")
    if meminfo:
        for line in meminfo.splitlines():
            if line.startswith("MemAvailable:"):
                avail = int(line.split()[1]) * 1024
    cur = _read("/sys/fs/cgroup/memory.current") or _read("/sys/fs/cgroup/memory/memory.usage_in_bytes")
    if cur and cur.isdigit():
        cg_free = total_memory() - int(cur)
        avail = cg_free if avail is None else min(avail, cg_free)
    return avail if avail is not None else total_memory() // 2


def cpu_count() -> int:
    v = _read("/sys/fs/cgroup/cpu.max")
    if v and not v.startswith("max"):
        try:
            quota, period = (int(x) for x in v.split()[:2])
            return max(1, quota // period)
        except ValueError:
            pass
    try:
        return len(os.sched_getaffinity(0))
    except (AttributeError, OSError):
        return os.cpu_count() or 2


def auto_limit() -> int:
    by_mem = max(1, total_memory() // PER_AGENT_BYTES)
    return max(1, min(by_mem, cpu_count(), HARD_CAP))


def can_start_another(running: int) -> bool:
    """Always allow the first agent; extra ones only while there's real free memory."""
    return running == 0 or available_memory() >= MIN_FREE_TO_START


def describe() -> str:
    return (f"{total_memory() / GB:.0f} GB RAM, {cpu_count()} CPUs → up to {auto_limit()} agents in parallel")
