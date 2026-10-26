"""Per-user install (no admin): one fixed location, shortcuts, Apps & features entry.

Installed layout:
    %LOCALAPPDATA%\\Programs\\SwiftMyPC\\SwiftMyPC.exe
    Desktop\\SwiftMyPC.lnk and Start menu\\Programs\\SwiftMyPC.lnk
    HKCU\\...\\Uninstall\\SwiftMyPC  (shows up in Settings > Apps)

SWIFTMYPC_INSTALL_DIR / SWIFTMYPC_SHORTCUT_DIR override the locations (used by tests).
"""

import ctypes
import os
import shutil
import subprocess
import sys
import uuid
import winreg

APP = "SwiftMyPC"
EXE_NAME = "SwiftMyPC.exe"
PUBLISHER = "Yassir Abidy"
# A sandbox install (tests) behaves as a separate app so it never touches the real one.
SANDBOX_SUFFIX = ".Sandbox" if os.environ.get("SWIFTMYPC_INSTALL_DIR") else ""
UNINSTALL_KEY = rf"Software\Microsoft\Windows\CurrentVersion\Uninstall\{APP}{SANDBOX_SUFFIX}"
FOLDERID_DESKTOP = "B4BFCC3A-DB2C-424C-B029-7FE99A87C641"
FOLDERID_PROGRAMS = "A77F5D77-2E2B-44C3-A6A2-ABA601054A51"
NO_WINDOW = subprocess.CREATE_NO_WINDOW


def install_dir() -> str:
    return os.environ.get("SWIFTMYPC_INSTALL_DIR") or os.path.join(
        os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "Programs", APP)


def installed_exe() -> str:
    return os.path.join(install_dir(), EXE_NAME)


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def _same(a: str, b: str) -> bool:
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def running_installed() -> bool:
    """True when this process is the installed copy (the only one that can self-update)."""
    return is_frozen() and _same(sys.executable, installed_exe())


def installed_version() -> str | None:
    if not os.path.isfile(installed_exe()):
        return None
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY) as key:
            return winreg.QueryValueEx(key, "DisplayVersion")[0]
    except OSError:
        return "unknown"


# --- shortcuts -----------------------------------------------------------------

class _GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_ulong), ("Data2", ctypes.c_ushort),
                ("Data3", ctypes.c_ushort), ("Data4", ctypes.c_ubyte * 8)]


def _known_folder(folder_id: str) -> str:
    u = uuid.UUID(folder_id)
    guid = _GUID(u.time_low, u.time_mid, u.time_hi_version, (ctypes.c_ubyte * 8)(*u.bytes[8:]))
    path = ctypes.c_wchar_p()
    if ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(guid), 0, None, ctypes.byref(path)) != 0:
        raise OSError("Couldn't find a Windows folder")
    try:
        return path.value
    finally:
        ctypes.windll.ole32.CoTaskMemFree(path)


def shortcut_paths() -> list[str]:
    override = os.environ.get("SWIFTMYPC_SHORTCUT_DIR")
    if override:
        return [os.path.join(override, "Desktop", f"{APP}.lnk"),
                os.path.join(override, "Programs", f"{APP}.lnk")]
    return [os.path.join(_known_folder(FOLDERID_DESKTOP), f"{APP}.lnk"),
            os.path.join(_known_folder(FOLDERID_PROGRAMS), f"{APP}.lnk")]


def _ps_quote(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def create_shortcuts() -> None:
    exe = installed_exe()
    lines = ["$sh = New-Object -ComObject WScript.Shell"]
    for path in shortcut_paths():
        os.makedirs(os.path.dirname(path), exist_ok=True)
        lines += [f"$s = $sh.CreateShortcut({_ps_quote(path)})",
                  f"$s.TargetPath = {_ps_quote(exe)}",
                  f"$s.WorkingDirectory = {_ps_quote(install_dir())}",
                  f"$s.IconLocation = {_ps_quote(exe + ',0')}",
                  "$s.Description = 'Keep your PC quick'",
                  "$s.Save()"]
    r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", "; ".join(lines)],
                       capture_output=True, text=True, creationflags=NO_WINDOW)
    if r.returncode != 0:
        raise OSError(f"Couldn't create shortcuts: {r.stderr.strip()[:200]}")


# --- install / uninstall -------------------------------------------------------

def register(version: str) -> None:
    """Add or refresh the Settings > Apps entry."""
    exe = installed_exe()
    size_kb = os.path.getsize(exe) // 1024 if os.path.isfile(exe) else 0
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY, 0, winreg.KEY_SET_VALUE) as key:
        for name, value in (("DisplayName", APP), ("DisplayVersion", version), ("Publisher", PUBLISHER),
                            ("DisplayIcon", exe), ("InstallLocation", install_dir()),
                            ("UninstallString", f'"{exe}" --uninstall'),
                            ("QuietUninstallString", f'"{exe}" --uninstall --quiet'),
                            ("URLInfoAbout", "https://github.com/yassirabidy/SwiftMyPC")):
            winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)
        for name, value in (("NoModify", 1), ("NoRepair", 1), ("EstimatedSize", size_kb)):
            winreg.SetValueEx(key, name, 0, winreg.REG_DWORD, value)


def install(source: str, version: str) -> None:
    """Copy source exe into place, then shortcuts + Apps entry. A running copy must be closed first."""
    os.makedirs(install_dir(), exist_ok=True)
    if not _same(source, installed_exe()):
        shutil.copy2(source, installed_exe())
    create_shortcuts()
    register(version)
    from .settings import set_start_with_windows, starts_with_windows
    if starts_with_windows():
        set_start_with_windows(True)  # point the existing autostart entry at the installed copy


def uninstall() -> None:
    """Remove shortcuts, Apps entry and autostart. The folder itself is removed by
    schedule_folder_removal() once this process has exited."""
    for path in shortcut_paths():
        try:
            os.remove(path)
        except OSError:
            pass
    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY)
    except OSError:
        pass
    from .settings import data_dir, set_start_with_windows
    set_start_with_windows(False)
    try:
        os.remove(os.path.join(data_dir(), "settings.json"))
    except OSError:
        pass  # service/optimize backups are kept so changes can still be restored


def schedule_folder_removal() -> None:
    folder = install_dir()
    # If something still holds the folder after the retries below, remove it at next sign-in.
    try:
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\RunOnce",
                                0, winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, f"{APP}{SANDBOX_SUFFIX}Cleanup", 0, winreg.REG_SZ,
                              f'cmd /c rmdir /s /q "{folder}"')
    except OSError:
        pass
    # Retry for ~2 min: the .exe stays locked until this process (and any antivirus scan) lets go.
    loop = (f'for /l %i in (1,1,60) do (ping -n 3 127.0.0.1 >nul & rmdir /s /q "{folder}" 2>nul '
            f'& if not exist "{folder}" exit /b 0)')
    # A hidden console, not DETACHED_PROCESS: without a console the cmd loop exits immediately.
    subprocess.Popen(f'cmd /c {loop}', creationflags=NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP,
                     cwd=os.environ.get("TEMP"))


def fresh_env() -> dict:
    """Environment for starting another SwiftMyPC.exe. Without the reset flag, a new copy
    started from inside a running one would reuse the running copy's temporary files."""
    env = dict(os.environ)
    env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    return env


def launch_installed(*args: str) -> None:
    subprocess.Popen([installed_exe(), *args], cwd=install_dir(), env=fresh_env(),
                     creationflags=subprocess.DETACHED_PROCESS)
