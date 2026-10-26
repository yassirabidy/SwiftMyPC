"""One-click health check: a short list of what's actually worth fixing."""

import time

import psutil

from . import cleaner, services, startup
from .monitor import ProcessSampler, fmt_bytes, system_stats


def run_health_check(admin: bool, junk_sizes: dict | None = None) -> list[tuple[str, str]]:
    """Returns [(level, text)] with level "ok", "warn" or "tip"."""
    findings = []
    sampler = ProcessSampler()
    sampler.sample()
    psutil.cpu_percent(None)
    time.sleep(1.5)
    apps = sampler.sample()
    stats = system_stats()

    by_mem = sorted(apps, key=lambda a: a.memory, reverse=True)[:3]
    top_mem = ", ".join(f"{a.name} ({fmt_bytes(a.memory)})" for a in by_mem)
    if stats["mem_percent"] >= 85:
        findings.append(("warn", f"Memory is {stats['mem_percent']:.0f}% full. Biggest users: {top_mem}. "
                                 "Closing one of them will free real memory."))
    else:
        findings.append(("ok", f"Memory is fine ({stats['mem_percent']:.0f}% used). Biggest users: {top_mem}."))

    top_cpu = max(apps, key=lambda a: a.cpu, default=None)
    if top_cpu and top_cpu.cpu >= 25:
        findings.append(("warn", f"{top_cpu.name} is using {top_cpu.cpu:.0f}% of your CPU right now. "
                                 "If you're not using it, end it from Processes."))
    elif stats["cpu"] >= 70:
        findings.append(("warn", f"CPU is busy ({stats['cpu']:.0f}%) but spread across many apps. "
                                 "Sort Processes by CPU to see what's active."))
    else:
        findings.append(("ok", f"No app is hogging the CPU ({stats['cpu']:.0f}% total right now)."))

    free_pct = 100 - stats["disk_percent"]
    if free_pct < 10 or stats["disk_free"] < 15 * 1024 ** 3:
        findings.append(("warn", f"Drive {stats['disk_drive']} is almost full ({fmt_bytes(stats['disk_free'])} free). "
                                 "A nearly full system drive slows Windows down; use the Cleaner."))
    else:
        findings.append(("ok", f"Drive {stats['disk_drive']} has {fmt_bytes(stats['disk_free'])} free."))

    enabled = [i for i in startup.list_startup() if i.enabled]
    if len(enabled) > 8:
        findings.append(("warn", f"{len(enabled)} apps start with Windows. Turn off the ones you don't need "
                                 "in Startup apps for a faster boot."))
    else:
        findings.append(("ok", f"{len(enabled)} apps start with Windows. That's reasonable."))

    optional = [s for s in services.list_services() if s.level]
    if optional:
        findings.append(("tip", f"{len(optional)} background services start automatically but probably "
                                "don't need to. Review them in Services."))

    junk = 0
    for t in cleaner.targets():
        if t.needs_admin and not admin:
            continue
        junk += (junk_sizes.get(t.key) if junk_sizes and t.key in junk_sizes else cleaner.scan(t))[0]
    if junk > 500 * 1024 ** 2:
        findings.append(("tip", f"About {fmt_bytes(junk)} of junk files can be cleaned in the Cleaner."))

    days = (time.time() - psutil.boot_time()) / 86400
    if days >= 7:
        findings.append(("tip", f"Your PC hasn't restarted in {days:.0f} days. A restart (not shut down, "
                                "which keeps Fast Startup state) clears leaks and finishes updates."))
    return findings
