"""How busy the server really is, measured every second: processor, memory and disk.

Nothing here counts people. New heavy work (another video, another recording) starts only while the machine has
room, waits in line for a moment when it is nearly full, and is let through the second it frees up again. That is
what keeps a rush from slowing everyone down or crashing the server."""
import os
import shutil
import threading
import time
from pathlib import Path

from . import config

BUSY_CPU = float(config.env("PEAKCUT_BUSY_CPU", "82"))          # percent: above this, no extra videos are started
MIN_FREE_RAM_GB = float(config.env("PEAKCUT_MIN_FREE_RAM_GB", "1.5"))
MIN_FREE_DISK_GB = float(config.env("PEAKCUT_MIN_FREE_DISK_GB", "3"))
_CGROUP_ROOT = Path("/sys/fs/cgroup")
_CGROUP_MEMBERSHIP = Path("/proc/self/cgroup")

_state = {"cpu": 0.0, "ram_free": 99.0, "disk_free": 99.0, "at": 0.0}
_force = None            # tests only: pretend the server is busy (True) or free (False)
_started = False
_lock = threading.Lock()


def _cpu_times():
    """(busy, total) processor time so far, in any unit."""
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        idle, kernel, user = wintypes.FILETIME(), wintypes.FILETIME(), wintypes.FILETIME()
        ctypes.windll.kernel32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user))
        n = lambda f: (f.dwHighDateTime << 32) | f.dwLowDateTime          # noqa: E731
        total = n(kernel) + n(user)                                        # kernel time includes idle time
        return total - n(idle), total
    with open("/proc/stat", encoding="ascii") as f:
        v = [int(x) for x in f.readline().split()[1:9]]
    return sum(v) - v[3] - v[4], sum(v)                                    # everything except idle and waiting on disk


def _cgroup_cpu_reader():
    """Read CPU time for the group whose quota limits this process, if there is one."""
    path = next((line[3:] for line in _CGROUP_MEMBERSHIP.read_text().splitlines()
                 if line.startswith("0::/")), None)
    if path is None or ".." in Path(path).parts:
        return None

    group = _CGROUP_ROOT / path.lstrip("/")
    limit = os.cpu_count() or float("inf")
    stats = None
    for directory in (group, *group.parents):
        try:
            quota, period = (directory / "cpu.max").read_text().split()
            if quota != "max":
                cores = int(quota) / int(period)
                if 0 < cores <= limit:
                    limit, stats = cores, directory / "cpu.stat"
        except (OSError, ValueError, ZeroDivisionError):
            pass
        if directory == _CGROUP_ROOT:
            break

    if stats is None:
        return None

    def read():
        for line in stats.read_text().splitlines():
            key, _, value = line.partition(" ")
            if key == "usage_usec":
                return int(value), time.monotonic_ns() * limit / 1000
        raise ValueError("No CPU usage in cpu.stat")

    return read


def _ram_free_gb() -> float:
    if os.name == "nt":
        import ctypes

        class M(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong)] + [
                (n, ctypes.c_ulonglong) for n in ("total", "avail", "tpf", "apf", "tv", "av", "aev")]
        m = M()
        m.dwLength = ctypes.sizeof(M)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
        return m.avail / 1e9
    with open("/proc/meminfo", encoding="ascii") as f:
        for line in f:
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) / 1e6
    return 99.0


def _watch():
    read_cpu = _cpu_times
    last = None
    try:
        limited_cpu = _cgroup_cpu_reader()
        if limited_cpu is not None:
            last = limited_cpu()
            read_cpu = limited_cpu
    except (OSError, ValueError):
        pass
    if last is None:
        try:
            last = read_cpu()
        except Exception:  # noqa: BLE001 - keep measuring memory and disk when CPU time is unavailable
            pass
    while True:
        time.sleep(1)
        if last is not None:
            try:
                now = read_cpu()
                used, total = now[0] - last[0], now[1] - last[1]
                last = now
                if total > 0 and used >= 0:
                    # smoothed over a few seconds, so one short spike neither holds people up nor lets too much in
                    _state["cpu"] = 0.55 * _state["cpu"] + 0.45 * min(100.0, 100.0 * used / total)
            except Exception:  # noqa: BLE001
                pass
        try:
            _state["ram_free"] = _ram_free_gb()
        except Exception:  # noqa: BLE001
            pass
        try:
            if time.time() - _state["at"] > 15:
                _state["disk_free"] = shutil.disk_usage(config.DATA_DIR).free / 1e9
                _state["at"] = time.time()
        except Exception:  # noqa: BLE001
            pass


def start():
    global _started
    with _lock:
        if not _started:
            _started = True
            threading.Thread(target=_watch, daemon=True, name="load").start()


def busy() -> bool:
    """True while the machine is close to full: nothing extra should start right now."""
    if _force is not None:
        return _force
    start()
    return _state["cpu"] >= BUSY_CPU or _state["ram_free"] < MIN_FREE_RAM_GB


def disk_full() -> bool:
    start()
    return _state["disk_free"] < MIN_FREE_DISK_GB


def state() -> dict:
    start()
    return {"cpu": round(_state["cpu"]), "ram_free_gb": round(_state["ram_free"], 1), "disk_free_gb": round(_state["disk_free"], 1),
            "busy": busy(), "busy_cpu": BUSY_CPU}
