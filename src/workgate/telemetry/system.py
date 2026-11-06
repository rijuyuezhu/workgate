"""Portable host and process telemetry collection."""

import contextlib
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

_PROCESS_STARTED_AT = time.time()
_SYSTEM_SAMPLE_LOCK = threading.Lock()
_CPU_SAMPLE: tuple[int, int] | None = None
_NETWORK_SAMPLE: tuple[float, int, int] | None = None


def _read_linux_cpu_times() -> tuple[int, int] | None:
    """Return cumulative Linux CPU total and idle ticks when available."""
    try:
        fields = (
            Path("/proc/stat")
            .read_text(encoding="utf-8")
            .splitlines()[0]
            .split()
        )
        if not fields or fields[0] != "cpu":
            return None
        values = [int(value) for value in fields[1:]]
    except OSError, ValueError, IndexError:
        return None
    if len(values) < 4:
        return None
    # Guest ticks are already included in user/nice time.
    total = sum(
        value for index, value in enumerate(values) if index not in {8, 9}
    )
    idle = values[3] + (values[4] if len(values) > 4 else 0)
    return total, idle


def _read_linux_memory() -> tuple[int, int] | None:
    """Return Linux memory total and used bytes when available."""
    try:
        values: dict[str, int] = {}
        for line in (
            Path("/proc/meminfo").read_text(encoding="utf-8").splitlines()
        ):
            name, separator, raw = line.partition(":")
            if not separator:
                continue
            token = raw.strip().split()[0]
            values[name] = int(token) * 1024
        total = values["MemTotal"]
        available = values.get("MemAvailable", values.get("MemFree", 0))
    except OSError, ValueError, KeyError, IndexError:
        return None
    return total, max(0, total - available)


def _read_windows_cpu_times() -> tuple[int, int] | None:
    import ctypes
    from ctypes import wintypes

    idle = wintypes.FILETIME()
    kernel = wintypes.FILETIME()
    user = wintypes.FILETIME()
    try:
        if not ctypes.windll.kernel32.GetSystemTimes(  # type: ignore[attr-defined]
            ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)
        ):
            return None
    except AttributeError, OSError, ValueError:
        return None

    def ticks(value: wintypes.FILETIME) -> int:
        return (int(value.dwHighDateTime) << 32) | int(value.dwLowDateTime)

    return ticks(kernel) + ticks(user), ticks(idle)


def _read_macos_cpu_times() -> tuple[int, int] | None:
    import ctypes

    class HostCpuLoadInfo(ctypes.Structure):
        _fields_ = [("cpu_ticks", ctypes.c_uint32 * 4)]

    try:
        library = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
        library.mach_host_self.restype = ctypes.c_uint32
        library.host_statistics.argtypes = [
            ctypes.c_uint32,
            ctypes.c_int,
            ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_uint32),
        ]
        library.host_statistics.restype = ctypes.c_int
        info = HostCpuLoadInfo()
        count = ctypes.c_uint32(4)
        status = library.host_statistics(
            library.mach_host_self(),
            3,  # HOST_CPU_LOAD_INFO
            ctypes.cast(ctypes.byref(info), ctypes.POINTER(ctypes.c_int)),
            ctypes.byref(count),
        )
        if status != 0 or count.value != 4:
            return None
        ticks = [int(value) for value in info.cpu_ticks]
        return sum(ticks), ticks[2]  # CPU_STATE_IDLE
    except OSError, AttributeError, TypeError, ValueError:
        return None


def _read_cpu_times() -> tuple[int, int] | None:
    if sys.platform.startswith("linux"):
        return _read_linux_cpu_times()
    if sys.platform == "win32":
        return _read_windows_cpu_times()
    if sys.platform == "darwin":
        return _read_macos_cpu_times()
    return None


def _read_windows_memory() -> tuple[int, int] | None:
    import ctypes
    from ctypes import wintypes

    class MemoryStatusEx(ctypes.Structure):
        _fields_ = [
            ("dwLength", wintypes.DWORD),
            ("dwMemoryLoad", wintypes.DWORD),
            ("ullTotalPhys", ctypes.c_ulonglong),
            ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong),
            ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong),
            ("ullAvailVirtual", ctypes.c_ulonglong),
            ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]

    status = MemoryStatusEx()
    status.dwLength = ctypes.sizeof(status)
    try:
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(  # type: ignore[attr-defined]
            ctypes.byref(status)
        ):
            return None
    except AttributeError, OSError, ValueError:
        return None
    total, available = int(status.ullTotalPhys), int(status.ullAvailPhys)
    if total <= 0 or not 0 <= available <= total:
        return None
    return total, total - available


def _read_macos_memory() -> tuple[int, int] | None:
    try:
        total_result = subprocess.run(
            ["sysctl", "-n", "hw.memsize"],
            capture_output=True,
            text=True,
            timeout=1,
            check=True,
        )
        vm_result = subprocess.run(
            ["vm_stat"],
            capture_output=True,
            text=True,
            timeout=1,
            check=True,
        )
        total = int(total_result.stdout.strip())
        lines = vm_result.stdout.splitlines()
        match = re.search(
            r"page size of (\d+) bytes", lines[0] if lines else ""
        )
        if match is None:
            return None
        page_size = int(match.group(1))
        pages: dict[str, int] = {}
        for line in lines[1:]:
            match = re.fullmatch(r"\s*([^:]+):\s*(\d+)\.?(?:\s*)", line)
            if match:
                pages[match.group(1)] = int(match.group(2))
        free_keys = ("Pages free", "Pages inactive", "Pages speculative")
        if (
            total <= 0
            or page_size <= 0
            or not any(k in pages for k in free_keys)
        ):
            return None
        available_pages = sum(pages.get(key, 0) for key in free_keys)
        return total, total - min(total, available_pages * page_size)
    except OSError, ValueError, subprocess.SubprocessError:
        return None


def _read_memory() -> tuple[int, int] | None:
    if sys.platform.startswith("linux"):
        return _read_linux_memory()
    if sys.platform == "win32":
        return _read_windows_memory()
    if sys.platform == "darwin":
        return _read_macos_memory()
    return None


def _read_linux_network() -> tuple[int, int] | None:
    """Return aggregate non-loopback Linux network RX/TX bytes."""
    try:
        lines = (
            Path("/proc/net/dev").read_text(encoding="utf-8").splitlines()[2:]
        )
    except OSError, IndexError:
        return None
    received = transmitted = 0
    found = False
    for line in lines:
        interface, separator, raw = line.partition(":")
        if not separator or interface.strip() == "lo":
            continue
        fields = raw.split()
        if len(fields) < 9:
            continue
        try:
            received += int(fields[0])
            transmitted += int(fields[8])
        except ValueError:
            continue
        found = True
    return (received, transmitted) if found else None


def _percent(used: int | float, total: int | float) -> float | None:
    """Return one bounded percentage, or None for an unusable total."""
    if total <= 0:
        return None
    return round(max(0.0, min(100.0, float(used) * 100.0 / float(total))), 1)


def local_system_snapshot(disk_path: Path) -> dict[str, Any]:
    """Collect a portable, best-effort process and host resource snapshot."""
    global _CPU_SAMPLE, _NETWORK_SAMPLE

    now = time.time()
    monotonic_now = time.monotonic()
    load_1m: float | None = None
    with contextlib.suppress(OSError, AttributeError):
        load_1m = round(float(os.getloadavg()[0]), 2)

    cpu_times = _read_cpu_times()
    cpu_percent: float | None = None
    network = _read_linux_network()
    network_rx_bps: float | None = None
    network_tx_bps: float | None = None
    with _SYSTEM_SAMPLE_LOCK:
        if cpu_times is not None:
            if _CPU_SAMPLE is not None:
                total_delta = cpu_times[0] - _CPU_SAMPLE[0]
                idle_delta = cpu_times[1] - _CPU_SAMPLE[1]
                if total_delta > 0 and 0 <= idle_delta <= total_delta:
                    cpu_percent = round(
                        max(
                            0.0,
                            min(
                                100.0,
                                (total_delta - idle_delta)
                                * 100.0
                                / total_delta,
                            ),
                        ),
                        1,
                    )
            _CPU_SAMPLE = cpu_times
        if network is not None:
            if _NETWORK_SAMPLE is not None:
                elapsed = monotonic_now - _NETWORK_SAMPLE[0]
                if elapsed > 0:
                    network_rx_bps = max(
                        0.0, (network[0] - _NETWORK_SAMPLE[1]) / elapsed
                    )
                    network_tx_bps = max(
                        0.0, (network[1] - _NETWORK_SAMPLE[2]) / elapsed
                    )
            _NETWORK_SAMPLE = (monotonic_now, network[0], network[1])

    cpu_count = max(1, os.cpu_count() or 1)
    if cpu_percent is None and load_1m is not None:
        cpu_percent = round(
            max(0.0, min(100.0, load_1m * 100.0 / cpu_count)), 1
        )

    memory = _read_memory()
    memory_total = memory[0] if memory else None
    memory_used = memory[1] if memory else None
    try:
        disk = shutil.disk_usage(disk_path)
    except OSError:
        disk = None

    uptime_s = max(0.0, now - _PROCESS_STARTED_AT)
    with contextlib.suppress(OSError, ValueError, IndexError):
        uptime_s = float(
            Path("/proc/uptime").read_text(encoding="utf-8").split()[0]
        )

    return {
        "timestamp": now,
        "cpu_percent": cpu_percent,
        "cpu_count": cpu_count,
        "memory_percent": (
            _percent(memory_used, memory_total)
            if memory_used is not None and memory_total is not None
            else None
        ),
        "memory_used_bytes": memory_used,
        "memory_total_bytes": memory_total,
        "disk_percent": _percent(disk.used, disk.total) if disk else None,
        "disk_used_bytes": disk.used if disk else None,
        "disk_total_bytes": disk.total if disk else None,
        "load_1m": load_1m,
        "network_rx_bps": (
            round(network_rx_bps, 1) if network_rx_bps is not None else None
        ),
        "network_tx_bps": (
            round(network_tx_bps, 1) if network_tx_bps is not None else None
        ),
        "uptime_s": round(max(0.0, uptime_s)),
    }
