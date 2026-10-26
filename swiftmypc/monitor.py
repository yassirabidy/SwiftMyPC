"""Live CPU / memory sampling of running processes."""

import ctypes
import os
import time
from ctypes import wintypes
from dataclasses import dataclass, field

import psutil

from .explain import describe, is_protected


@dataclass
class AppUsage:
    key: str            # tree id: lowercase name when grouped, pid when not
    name: str
    pids: list = field(default_factory=list)
    cpu: float = 0.0    # percent of the whole machine
    memory: int = 0     # bytes (working set)

    @property
    def count(self) -> int:
        return len(self.pids)

    @property
    def description(self) -> str:
        return describe(self.name)

    @property
    def protected(self) -> bool:
        return is_protected(self.name)


class _SYSTEM_PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("NextEntryOffset", wintypes.ULONG),
        ("NumberOfThreads", wintypes.ULONG),
        ("WorkingSetPrivateSize", ctypes.c_longlong),
        ("HardFaultCount", wintypes.ULONG),
        ("NumberOfThreadsHighWatermark", wintypes.ULONG),
        ("CycleTime", ctypes.c_ulonglong),
        ("CreateTime", ctypes.c_longlong),
        ("UserTime", ctypes.c_longlong),
        ("KernelTime", ctypes.c_longlong),
        ("ImageNameLength", wintypes.USHORT),
        ("ImageNameMaximumLength", wintypes.USHORT),
        ("ImageNameBuffer", ctypes.c_void_p),
        ("BasePriority", wintypes.LONG),
        ("UniqueProcessId", ctypes.c_void_p),
        ("InheritedFromUniqueProcessId", ctypes.c_void_p),
        ("HandleCount", wintypes.ULONG),
        ("SessionId", wintypes.ULONG),
        ("UniqueProcessKey", ctypes.c_void_p),
        ("PeakVirtualSize", ctypes.c_size_t),
        ("VirtualSize", ctypes.c_size_t),
        ("PageFaultCount", wintypes.ULONG),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
    ]


_SystemProcessInformation = 5
_STATUS_INFO_LENGTH_MISMATCH = 0xC0000004
_ntdll = ctypes.WinDLL("ntdll")
_ntdll.NtQuerySystemInformation.restype = ctypes.c_ulong


def _snapshot():
    """All processes in one kernel call: [(pid, name, create_time, cpu_time_100ns, working_set)].

    Much faster than asking psutil per process, which falls back to a full
    system query for every process it isn't allowed to open.
    """
    size = 1 << 20
    while True:
        buf = ctypes.create_string_buffer(size)
        needed = wintypes.ULONG()
        status = _ntdll.NtQuerySystemInformation(_SystemProcessInformation, buf, size,
                                                 ctypes.byref(needed))
        if status == _STATUS_INFO_LENGTH_MISMATCH:
            size = max(size * 2, needed.value + 65536)
            continue
        if status != 0:
            raise OSError(f"NtQuerySystemInformation failed: 0x{status:08X}")
        break
    out, offset = [], 0
    while True:
        info = _SYSTEM_PROCESS_INFORMATION.from_buffer(buf, offset)
        pid = info.UniqueProcessId or 0
        name = (ctypes.wstring_at(info.ImageNameBuffer, info.ImageNameLength // 2)
                if info.ImageNameBuffer else "")
        out.append((pid, name, info.CreateTime, info.UserTime + info.KernelTime,
                    info.WorkingSetSize))
        if not info.NextEntryOffset:
            return out
        offset += info.NextEntryOffset


class ProcessSampler:
    """Remembers each process's CPU time between samples to compute CPU %."""

    def __init__(self):
        self._prev: dict[tuple[int, int], int] = {}
        self._prev_wall = None
        self._cpus = psutil.cpu_count() or 1

    def sample(self, group: bool = True) -> list[AppUsage]:
        now = time.perf_counter()
        procs = _snapshot()
        elapsed = (now - self._prev_wall) if self._prev_wall else 0
        scale = 100 / (elapsed * 1e7 * self._cpus) if elapsed else 0
        prev, self._prev, self._prev_wall = self._prev, {}, now

        rows: dict[str, AppUsage] = {}
        for pid, name, created, cpu_time, mem in procs:
            ident = (pid, created)
            self._prev[ident] = cpu_time
            cpu = max(0, cpu_time - prev[ident]) * scale if ident in prev else 0.0
            if pid == 0:
                name, cpu = "System Idle Process", 0.0  # its "CPU" is idle time
            key = name.lower() if group else str(pid)
            row = rows.get(key)
            if row is None:
                row = rows[key] = AppUsage(key=key, name=name)
            row.pids.append(pid)
            row.cpu += cpu
            row.memory += mem
        return list(rows.values())


def end_processes(pids: list[int]) -> tuple[int, int]:
    """Terminate the given processes. Returns (ended, denied)."""
    ended = denied = 0
    procs = []
    for pid in pids:
        try:
            proc = psutil.Process(pid)
            proc.terminate()
            procs.append(proc)
            ended += 1
        except psutil.NoSuchProcess:
            pass
        except psutil.AccessDenied:
            denied += 1
    psutil.wait_procs(procs, timeout=3)
    return ended, denied


def system_stats() -> dict:
    vm = psutil.virtual_memory()
    sysdrive = os.environ.get("SystemDrive", "C:") + "\\"
    disk = psutil.disk_usage(sysdrive)
    return {
        "cpu": psutil.cpu_percent(None),
        "mem_percent": vm.percent,
        "mem_used": vm.total - vm.available,
        "mem_total": vm.total,
        "disk_drive": sysdrive.rstrip("\\"),
        "disk_percent": disk.percent,
        "disk_free": disk.free,
        "disk_total": disk.total,
    }


def fmt_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"
