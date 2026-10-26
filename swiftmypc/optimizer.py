"""Smart Optimize: one button that applies only the safe fixes.

Rules this module follows, so it can run without asking about each item:
  * Files: only caches, temp files and crash reports. Never personal files,
    never the Recycle Bin. Browser caches only for browsers that are closed.
  * Background apps: only known updaters/helpers that have no open window
    and restart by themselves when needed. Never anything you are using.
  * Services: only updaters/telemetry, and only switched to Manual (they
    still start when something needs them). Never disabled.
  * Startup: only browser pre-launchers and updaters.
  * Every service/startup change is logged and can be undone.
"""

import ctypes
import json
import os
import subprocess
import time
from ctypes import wintypes
from dataclasses import dataclass, field

import psutil

from . import cleaner, services, startup
from .monitor import end_processes, fmt_bytes
from .settings import data_dir

LOG_FILE = "optimize_log.json"

# process name (lowercase) -> what it is
SAFE_BACKGROUND = {
    "googleupdate.exe": "Google updater",
    "microsoftedgeupdate.exe": "Edge updater",
    "adobearm.exe": "Adobe Acrobat updater",
    "ccxprocess.exe": "Adobe Creative Cloud helper",
    "jusched.exe": "Java update checker",
    "onedrivestandaloneupdater.exe": "OneDrive updater",
    "phoneexperiencehost.exe": "Phone Link (reopens when you open it)",
    "widgets.exe": "Widgets panel (reopens when you open it)",
    "gamebar.exe": "Xbox Game Bar (reopens with Win+G)",
    "gamebarftserver.exe": "Xbox Game Bar helper",
}
# Generic names that are only safe in a specific folder.
SAFE_BACKGROUND_IN = {"updater.exe": (r"\google\googleupdater", "Google updater")}

# Service name prefixes (lowercase) safe to switch to Manual.
SAFE_SERVICES = (
    "gupdate", "googleupdater", "edgeupdate", "microsoftedgeelevationservice", "brave",
    "mozillamaintenance", "adobearmservice", "adobeupdateservice", "dropboxupdate",
    "onedrive updater service", "foxitreaderupdateservice", "foxitpdfreaderupdateservice",
    "diagtrack", "dmwappushservice", "mapsbroker", "retaildemo", "wmpnetworksvc",
    "xblauthmanager", "xblgamesave", "xboxnetapisvc", "wisvc",
)

# Startup entry name prefixes (lowercase) safe to turn off.
SAFE_STARTUP = (
    "googlechromeautolaunch_", "microsoftedgeautolaunch_", "googleupdatertask",
    "adobe ccxprocess", "ccxprocess", "sunjavaupdatesched", "jusched",
)

DO_CACHE = os.path.join(os.environ.get("WINDIR", r"C:\Windows"),
                        r"ServiceProfiles\NetworkService\AppData\Local\Microsoft\Windows\DeliveryOptimization\Cache")


@dataclass
class Action:
    key: str
    group: str                # "files" | "apps" | "services" | "startup"
    label: str
    detail: str
    size: int = 0             # bytes of disk or memory this frees
    count: int = 0
    needs_admin: bool = False
    auto: bool = False        # included in the daily automatic run
    items: list = field(default_factory=list)
    result: str = ""


# --- helpers -------------------------------------------------------------------

def _pids_with_windows() -> set[int]:
    """PIDs that own a visible, titled window, i.e. something the user can see."""
    user32 = ctypes.windll.user32
    pids = set()
    proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def cb(hwnd, _):
        if user32.IsWindowVisible(hwnd) and user32.GetWindowTextLengthW(hwnd) > 0:
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            pids.add(pid.value)
        return True
    user32.EnumWindows(proc(cb), 0)
    return pids


def _running_names() -> set[str]:
    return {(p.info["name"] or "").lower() for p in psutil.process_iter(["name"])}


def _fmt_list(names, limit=4) -> str:
    names = list(dict.fromkeys(names))
    more = f" and {len(names) - limit} more" if len(names) > limit else ""
    return ", ".join(names[:limit]) + more


# --- planning ------------------------------------------------------------------

def _plan_files(admin: bool) -> list[Action]:
    actions = []
    running = _running_names()
    for t in cleaner.targets():
        if t.recycle_bin or t.key == "browser":
            continue
        if t.needs_admin and not admin:
            continue
        size, count = cleaner.scan(t)
        if count:
            actions.append(Action(f"files:{t.key}", "files", t.label, t.description, size, count,
                                  t.needs_admin, auto=True, items=[t]))

    closed, open_ = [], []
    for name, (exe, paths) in cleaner.browser_caches().items():
        if not paths:
            continue
        (open_ if exe in running else closed).append((name, paths))
    # Browser caches are left out of the daily automatic run: wiping them every
    # day makes browsing slower while the browser re-downloads everything.
    added = False
    if closed:
        t = cleaner.Target("browser_closed", "Browser caches", "", [p for _, ps in closed for p in ps])
        size, count = cleaner.scan(t)
        if count:
            detail = f"{_fmt_list(n for n, _ in closed)}. You stay logged in."
            if open_:
                detail += f" Skipping {_fmt_list(n for n, _ in open_)} because it's open."
            actions.append(Action("files:browser", "files", "Browser caches", detail, size, count,
                                  items=[t]))
            added = True
    if open_ and not added:
        actions.append(Action("note:browser", "note", "Browser caches skipped",
                              f"{_fmt_list(n for n, _ in open_)} is open. Close it and scan again "
                              "to clean its cache too."))

    if admin and os.path.isdir(DO_CACHE):
        t = cleaner.Target("do_cache", "", "", [DO_CACHE])
        size, count = cleaner.scan(t)
        if count:
            actions.append(Action("files:do", "files", "Windows Update sharing cache",
                                  "Update pieces Windows keeps to share with other PCs.",
                                  size, count, True, auto=True, items=[t]))
    return actions


def _plan_apps() -> list[Action]:
    visible = _pids_with_windows()
    found = []
    for p in psutil.process_iter(["name", "memory_info"]):
        name = (p.info["name"] or "").lower()
        what = SAFE_BACKGROUND.get(name)
        if not what and name in SAFE_BACKGROUND_IN:
            folder, what = SAFE_BACKGROUND_IN[name]
            try:
                if folder not in p.exe().lower():
                    continue
            except psutil.Error:
                continue
        if not what or p.pid in visible:
            continue
        mem = p.info["memory_info"].rss if p.info["memory_info"] else 0
        found.append((p.pid, what, mem))
    if not found:
        return []
    return [Action("apps", "apps", f"Close {len(found)} background helper{'s' * (len(found) > 1)}",
                   _fmt_list(w for _, w, _ in found) + ". They restart on their own when needed.",
                   sum(m for _, _, m in found), len(found), auto=True, items=found)]


def _plan_services(admin: bool) -> list[Action]:
    found = [s for s in services.list_services()
             if s.name.lower().startswith(SAFE_SERVICES) and s.can_change
             and s.start in ("auto", "delayed-auto")]
    if not found:
        return []
    return [Action("services", "services",
                   f"Set {len(found)} updater/telemetry service{'s' * (len(found) > 1)} to Manual",
                   _fmt_list(s.display for s in found) + ". They still start whenever something needs them.",
                   sum(s.memory for s in found), len(found), needs_admin=True, items=found)]


def _plan_startup(admin: bool) -> list[Action]:
    found = [i for i in startup.list_startup()
             if i.enabled and i.name.lower().startswith(SAFE_STARTUP) and (admin or not i.needs_admin)]
    if not found:
        return []
    names = [i.name.split("_")[0] for i in found]
    return [Action("startup", "startup",
                   f"Stop {len(found)} pre-launcher{'s' * (len(found) > 1)} / updater{'s' * (len(found) > 1)} at startup",
                   _fmt_list(names) + ". The apps still work normally when you open them.",
                   0, len(found), items=found)]


def _plan_stop_services() -> list[Action]:
    """Running updater/telemetry services that can be stopped right now (they start again when needed)."""
    found = [s for s in services.list_services()
             if s.name.lower().startswith(SAFE_SERVICES) and s.can_change and s.status == "running"]
    if not found:
        return []
    return [Action("stop", "stop", f"Stop {len(found)} updater/telemetry service{'s' * (len(found) > 1)}",
                   _fmt_list(s.display for s in found) + ".",
                   sum(s.memory for s in found), len(found), needs_admin=True, items=found)]


def plan_unnecessary(admin: bool) -> list[Action]:
    """What 'End unnecessary tasks' would close: windowless helpers, plus running
    updater services when we're admin."""
    return _plan_apps() + (_plan_stop_services() if admin else [])


def plan(admin: bool, auto_only: bool = False) -> list[Action]:
    actions = _plan_files(admin) + _plan_apps()
    if auto_only:
        return [a for a in actions if a.auto]
    if admin:
        actions += _plan_services(admin)
    return actions + _plan_startup(admin)


def admin_only_preview() -> list[str]:
    """What an admin run would add, to tell a non-admin user what they're missing."""
    extra = []
    if any(s.name.lower().startswith(SAFE_SERVICES) and s.can_change and s.start in ("auto", "delayed-auto")
           for s in services.list_services()):
        extra.append("updater/telemetry services")
    extra.append("Windows temp and update caches")
    return extra


# --- running -------------------------------------------------------------------

def _load_log() -> dict:
    try:
        with open(os.path.join(data_dir(), LOG_FILE), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save_log(log: dict) -> None:
    with open(os.path.join(data_dir(), LOG_FILE), "w", encoding="utf-8") as f:
        json.dump(log, f, indent=2)


def run(actions: list[Action]) -> int:
    """Apply the actions. Fills each action's .result; returns bytes of disk freed."""
    freed_disk = 0
    changes = {"time": time.time(), "services": [], "startup": []}
    for a in actions:
        try:
            if a.group == "files":
                freed = skipped = 0
                for t in a.items:
                    if t.key == "do_cache":
                        subprocess.run(["powershell", "-NoProfile", "-Command",
                                        "Delete-DeliveryOptimizationCache -Force"],
                                       capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
                        left, _ = cleaner.scan(t)
                        freed += max(0, a.size - left)
                    else:
                        f, _, s = cleaner.clean(t)
                        freed, skipped = freed + f, skipped + s
                freed_disk += freed
                a.result = f"freed {fmt_bytes(freed)}" + (f", {skipped} in use skipped" if skipped else "")
            elif a.group == "apps":
                ended, denied = end_processes([pid for pid, _, _ in a.items])
                a.result = f"closed {ended}" + (f", {denied} need admin" if denied else "")
            elif a.group == "stop":
                done = 0
                for s in a.items:
                    try:
                        services.stop(s)
                        done += 1
                    except OSError:
                        pass
                a.result = f"{done} stopped"
            elif a.group == "services":
                done = 0
                for s in a.items:
                    try:
                        services.set_start(s, "demand")
                        changes["services"].append(s.name)
                        done += 1
                    except OSError:
                        pass
                a.result = f"{done} set to Manual"
            elif a.group == "startup":
                done = 0
                for i in a.items:
                    try:
                        startup.set_enabled(i, False)
                        changes["startup"].append(i.name)
                        done += 1
                    except OSError:
                        pass
                a.result = f"{done} turned off"
        except Exception as e:  # one failing action must not stop the rest
            a.result = f"failed: {e}"
    if changes["services"] or changes["startup"]:
        _save_log(changes)
    return freed_disk


def can_undo() -> bool:
    log = _load_log()
    return bool(log.get("services") or log.get("startup"))


def undo() -> str:
    """Revert the service/startup changes of the last Smart Optimize run."""
    log = _load_log()
    restored, failed_services, failed_startup = 0, [], []
    if log.get("services"):
        by_name = {s.name: s for s in services.list_services()}
        for name in log["services"]:
            try:
                if name in by_name and services.restore(by_name[name]):
                    restored += 1
            except OSError:
                failed_services.append(name)
    if log.get("startup"):
        items = {i.name: i for i in startup.list_startup()}
        for name in log["startup"]:
            try:
                if name in items:
                    startup.set_enabled(items[name], True)
                    restored += 1
            except OSError:
                failed_startup.append(name)
    # Keep only what still needs undoing, so a later admin run can finish it.
    _save_log({"time": log.get("time"), "services": failed_services, "startup": failed_startup})
    msg = f"Restored {restored} change{'s' * (restored != 1)}."
    if failed_services or failed_startup:
        msg += " Couldn't restore (needs admin): " + ", ".join(failed_services + failed_startup)
    return msg
