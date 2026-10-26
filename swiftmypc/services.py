"""Background services audit.

Lists Windows services, flags the ones that are safe to stop starting
automatically, and changes their startup type with sc.exe. The original
startup type is saved before the first change so it can be restored.
"""

import json
import os
import subprocess
import winreg
from dataclasses import dataclass

import psutil

from .settings import data_dir

WINDIR = os.environ.get("WINDIR", r"C:\Windows").lower()
BACKUP_FILE = "services_backup.json"

# Windows services that are safe to switch off for most people.
# Any Windows service NOT listed here is locked in the UI.
WINDOWS_OPTIONAL = {
    "diagtrack": "Sends usage and diagnostics data to Microsoft. Windows works the same without it.",
    "dmwappushservice": "Routes diagnostics data. Safe to disable.",
    "mapsbroker": "Downloads offline maps. Manual is fine.",
    "retaildemo": "Shop demo mode. Safe to disable.",
    "fax": "Fax support. Safe to disable unless you send faxes.",
    "wmpnetworksvc": "Shares your Windows Media Player library on the network.",
    "xblauthmanager": "Xbox sign-in. Manual is fine if you don't play Xbox / Game Pass games.",
    "xblgamesave": "Xbox cloud saves. Manual is fine if you don't play Xbox / Game Pass games.",
    "xboxnetapisvc": "Xbox networking. Manual is fine if you don't play Xbox / Game Pass games.",
    "xboxgipsvc": "Xbox accessories. Manual is fine if you don't use an Xbox controller.",
    "lfsvc": "Location for apps. Manual is fine if you don't use location features.",
    "phonesvc": "Telephony for SIM / phone features. Manual is fine.",
    "remoteregistry": "Lets other computers edit your registry. Best kept disabled for security.",
    "wisvc": "Windows Insider program. Manual is fine if you're not an Insider.",
    "wpcmonsvc": "Parental controls. Manual is fine if you don't use Family Safety.",
}

# Well-known app services (matched by name prefix, lowercase).
APP_KNOWN = {
    "gupdate": "Google updater. Manual is fine: Chrome still updates itself when it's open.",
    "googleupdater": "Google updater. Manual is fine: Chrome still updates itself when it's open.",
    "edgeupdate": "Edge updater. Manual is fine: Edge still updates itself when it's open.",
    "microsoftedgeelevationservice": "Edge helper. Manual is fine.",
    "brave": "Brave updater/helper. Manual is fine.",
    "mozillamaintenance": "Firefox background updater. Manual is fine.",
    "adobearmservice": "Adobe Acrobat updater. Manual is fine.",
    "agsservice": "Adobe Genuine Software check. Safe to disable.",
    "adobeupdateservice": "Adobe updater. Manual is fine.",
    "dropboxupdate": "Dropbox updater. Manual is fine.",
    "onedrive updater service": "OneDrive updater. Manual is fine.",
    "ccleaner": "CCleaner background service. Manual is fine.",
    "hola": "Hola VPN service. Disable if you don't use Hola.",
    "urbanvpn": "Urban VPN service. Disable if you don't use it.",
    "foxit": "Foxit updater. Manual is fine.",
    "apple mobile device": "iPhone / iTunes sync. Manual is fine if you don't plug in an iPhone.",
    "postgresql": "PostgreSQL database server. Manual is fine if you only use it for development.",
    "mysql": "MySQL database server. Manual is fine if you only use it for development.",
    "mongodb": "MongoDB database server. Manual is fine if you only use it for development.",
}

# App services that should stay as they are (matched by name prefix, lowercase).
APP_KEEP = {
    "clicktorunsvc": "Microsoft Office. Needed for Office apps to work and update. Leave it on.",
    "wslservice": "Windows Subsystem for Linux. Needed for WSL and Docker. Leave it on if you use them.",
    "nvdisplay.containerlocalsystem": "NVIDIA graphics driver service. Leave it on.",
    "amd external events": "AMD graphics driver service. Leave it on.",
    "rtkaudiouniversalservice": "Realtek audio driver service. Leave it on.",
    "vgc": "Riot Vanguard anti-cheat. Needed for Valorant / League of Legends.",
}

START_LABELS = {"auto": "Automatic", "delayed-auto": "Automatic (delayed)",
                "demand": "Manual", "disabled": "Disabled", "boot": "Boot", "system": "System"}


@dataclass
class Service:
    name: str
    display: str
    status: str
    start: str          # auto | delayed-auto | demand | disabled | boot | system
    binpath: str
    description: str
    pid: int | None
    memory: int
    is_windows: bool
    advice: str
    level: str          # "optional", "review" or "" (nothing to do)

    @property
    def start_label(self) -> str:
        return START_LABELS.get(self.start, self.start)

    @property
    def can_change(self) -> bool:
        return not self.is_windows or self.name.lower() in WINDOWS_OPTIONAL

    @property
    def source(self) -> str:
        return "Windows" if self.is_windows else "App"


def _start_type(name: str) -> str:
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            rf"SYSTEM\CurrentControlSet\Services\{name}") as key:
            start = winreg.QueryValueEx(key, "Start")[0]
            try:
                delayed = winreg.QueryValueEx(key, "DelayedAutostart")[0]
            except OSError:
                delayed = 0
    except OSError:
        return "demand"
    return {0: "boot", 1: "system", 2: "delayed-auto" if delayed else "auto",
            3: "demand", 4: "disabled"}.get(start, "demand")


def _is_windows(binpath: str) -> bool:
    p = binpath.lower().strip('"')
    return (p.startswith(WINDIR) or p.startswith("%systemroot%") or p.startswith(r"\systemroot")
            or r"\windows defender" in p or r"\microsoft\windows defender" in p)


def _advice(name: str, is_windows: bool, start: str) -> tuple[str, str]:
    lname = name.lower()
    auto = start in ("auto", "delayed-auto")
    if is_windows:
        if lname in WINDOWS_OPTIONAL:
            return WINDOWS_OPTIONAL[lname], "optional" if auto else ""
        return "Windows service. Leave it as is.", ""
    for prefix, text in APP_KEEP.items():
        if lname.startswith(prefix):
            return text, ""
    for prefix, text in APP_KNOWN.items():
        if lname.startswith(prefix):
            return text, "optional" if auto else ""
    if auto:
        return ("Third-party service that starts with Windows. If you don't recognise or use the "
                "app it belongs to, set it to Manual."), "review"
    return "", ""


def list_services() -> list[Service]:
    out = []
    for s in psutil.win_service_iter():
        try:
            d = s.as_dict()
        except (psutil.Error, OSError):
            continue
        start = _start_type(d["name"])
        binpath = d.get("binpath") or ""
        is_win = _is_windows(binpath)
        memory = 0
        pid = d.get("pid")
        if pid and "svchost.exe" not in binpath.lower():
            try:
                memory = psutil.Process(pid).memory_info().rss
            except psutil.Error:
                pass
        advice, level = _advice(d["name"], is_win, start)
        out.append(Service(d["name"], d.get("display_name") or d["name"], d.get("status") or "",
                           start, binpath, d.get("description") or "", pid, memory, is_win,
                           advice, level))
    return out


# --- changes -------------------------------------------------------------------

def _sc(*args: str) -> tuple[int, str]:
    r = subprocess.run(["sc.exe", *args], capture_output=True, text=True,
                       creationflags=subprocess.CREATE_NO_WINDOW)
    return r.returncode, (r.stdout + r.stderr).strip()


def _check(code: int, output: str) -> None:
    if code == 5:
        raise PermissionError("Administrator rights are needed. Use 'Restart as admin'.")
    if code not in (0, 1062):  # 1062: service not started (fine when stopping)
        raise OSError(output.splitlines()[-1] if output else f"sc.exe error {code}")


def _backup_path() -> str:
    return os.path.join(data_dir(), BACKUP_FILE)


def _load_backup() -> dict:
    try:
        with open(_backup_path(), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save_backup(data: dict) -> None:
    with open(_backup_path(), "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def has_backup(name: str) -> bool:
    return name in _load_backup()


def set_start(svc: Service, start: str) -> None:
    """start: 'demand', 'disabled', 'auto' or 'delayed-auto'."""
    if not svc.can_change:
        raise PermissionError(f"{svc.display} is a core Windows service; SwiftMyPC won't change it.")
    backup = _load_backup()
    _check(*_sc("config", svc.name, "start=", start))
    if svc.name not in backup:
        backup[svc.name] = svc.start
        _save_backup(backup)
    svc.start = start
    svc.advice, svc.level = _advice(svc.name, svc.is_windows, start)


def restore(svc: Service) -> bool:
    """Put back the startup type saved before the first change. False if none was saved."""
    backup = _load_backup()
    original = backup.get(svc.name)
    if not original:
        return False
    _check(*_sc("config", svc.name, "start=", original))
    del backup[svc.name]
    _save_backup(backup)
    svc.start = original
    svc.advice, svc.level = _advice(svc.name, svc.is_windows, original)
    return True


def stop(svc: Service) -> None:
    if not svc.can_change:
        raise PermissionError(f"{svc.display} is a core Windows service; SwiftMyPC won't stop it.")
    _check(*_sc("stop", svc.name))
    svc.status = "stop_pending"
