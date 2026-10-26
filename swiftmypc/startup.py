"""Startup apps: list and enable/disable them the same way Task Manager does.

Disabling never deletes anything. Like Task Manager, it writes a flag under
...\\Explorer\\StartupApproved, so the item can be re-enabled at any time.
"""

import ctypes
import os
import time
import winreg
from dataclasses import dataclass

RUN = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN32 = r"Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Run"
APPROVED = r"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved"

USER_FOLDER = os.path.join(os.environ.get("APPDATA", ""), r"Microsoft\Windows\Start Menu\Programs\Startup")
COMMON_FOLDER = os.path.join(os.environ.get("PROGRAMDATA", ""), r"Microsoft\Windows\Start Menu\Programs\StartUp")


@dataclass
class StartupItem:
    name: str
    command: str
    location: str
    hive: int
    approved_key: str     # "Run", "Run32" or "StartupFolder"
    approved_value: str   # value name under StartupApproved\<approved_key>
    enabled: bool

    @property
    def needs_admin(self) -> bool:
        return self.hive == winreg.HKEY_LOCAL_MACHINE

    @property
    def path(self) -> str:
        """Best guess at the program file, for 'open file location'."""
        cmd = os.path.expandvars(self.command.strip())
        if cmd.startswith('"'):
            return cmd[1:].split('"', 1)[0]
        lower = cmd.lower()
        for ext in (".exe", ".bat", ".cmd", ".lnk", ".vbs", ".ps1"):
            i = lower.find(ext)
            if i != -1:
                return cmd[: i + len(ext)]
        return cmd.split(" ", 1)[0]


def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _approved_enabled(hive: int, sub: str, value: str) -> bool:
    try:
        with winreg.OpenKey(hive, rf"{APPROVED}\{sub}") as key:
            data, _ = winreg.QueryValueEx(key, value)
    except OSError:
        return True  # no flag means enabled
    return not (data and data[0] & 1)  # odd first byte means disabled


def _read_run_key(hive: int, path: str, sub: str, label: str) -> list[StartupItem]:
    items = []
    try:
        key = winreg.OpenKey(hive, path)
    except OSError:
        return items
    with key:
        i = 0
        while True:
            try:
                name, value, _ = winreg.EnumValue(key, i)
            except OSError:
                break
            i += 1
            if not isinstance(value, str):
                continue
            items.append(StartupItem(name, value, label, hive, sub, name,
                                     _approved_enabled(hive, sub, name)))
    return items


def _read_folder(folder: str, hive: int, label: str) -> list[StartupItem]:
    items = []
    try:
        entries = os.listdir(folder)
    except OSError:
        return items
    for fname in entries:
        if fname.lower() == "desktop.ini":
            continue
        name = os.path.splitext(fname)[0]
        items.append(StartupItem(name, os.path.join(folder, fname), label, hive,
                                 "StartupFolder", fname,
                                 _approved_enabled(hive, "StartupFolder", fname)))
    return items


def list_startup() -> list[StartupItem]:
    HKCU, HKLM = winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE
    items = []
    items += _read_run_key(HKCU, RUN, "Run", "Registry (you)")
    items += _read_run_key(HKCU, RUN32, "Run32", "Registry (you, 32-bit)")
    items += _read_run_key(HKLM, RUN, "Run", "Registry (all users)")
    items += _read_run_key(HKLM, RUN32, "Run32", "Registry (all users, 32-bit)")
    items += _read_folder(USER_FOLDER, HKCU, "Startup folder (you)")
    items += _read_folder(COMMON_FOLDER, HKLM, "Startup folder (all users)")
    return sorted(items, key=lambda it: it.name.lower())


def set_enabled(item: StartupItem, enabled: bool) -> None:
    """Raises PermissionError if the item belongs to all users and we're not admin."""
    if enabled:
        data = bytes([0x02]) + bytes(11)
    else:
        filetime = int((time.time() + 11644473600) * 10_000_000)
        data = bytes([0x03, 0, 0, 0]) + filetime.to_bytes(8, "little")
    with winreg.CreateKeyEx(item.hive, rf"{APPROVED}\{item.approved_key}", 0,
                            winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, item.approved_value, 0, winreg.REG_BINARY, data)
    item.enabled = enabled
