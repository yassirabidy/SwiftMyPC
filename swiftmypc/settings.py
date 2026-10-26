"""User settings, stored in %LOCALAPPDATA%\\SwiftMyPC\\settings.json."""

import json
import os
import sys
import winreg

DEFAULTS = {
    "alerts": True,           # tray notifications for high CPU / memory / low disk
    "close_to_tray": True,    # closing the window keeps SwiftMyPC running in the tray
    "tray_hint_shown": False,
    "auto_optimize": True,    # daily Smart Optimize of junk files + background helpers
    "last_auto_optimize": 0,
    "auto_update": True,      # install new versions by itself while running in the tray
    "notified_version": "",   # last update version we already told the user about
    "theme": "system",        # "system", "light" or "dark"
}

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_NAME = "SwiftMyPC"


def data_dir() -> str:
    path = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "SwiftMyPC")
    os.makedirs(path, exist_ok=True)
    return path


class Settings(dict):
    def __init__(self):
        super().__init__(DEFAULTS)
        try:
            with open(os.path.join(data_dir(), "settings.json"), encoding="utf-8") as f:
                self.update(json.load(f))
        except (OSError, ValueError):
            pass

    def set(self, key, value):
        self[key] = value
        try:
            with open(os.path.join(data_dir(), "settings.json"), "w", encoding="utf-8") as f:
                json.dump(self, f, indent=2)
        except OSError:
            pass


def launch_command() -> str:
    """Command that starts SwiftMyPC minimised to the tray."""
    if getattr(sys, "frozen", False):
        from .installer import installed_exe
        exe = installed_exe() if os.path.isfile(installed_exe()) else sys.executable
        return f'"{exe}" --tray'
    pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    launcher = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "launcher.py")
    return f'"{pythonw}" "{launcher}" --tray'


def starts_with_windows() -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.QueryValueEx(key, RUN_NAME)
        return True
    except OSError:
        return False


def set_start_with_windows(enabled: bool) -> None:
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            winreg.SetValueEx(key, RUN_NAME, 0, winreg.REG_SZ, launch_command())
        else:
            try:
                winreg.DeleteValue(key, RUN_NAME)
            except OSError:
                pass
