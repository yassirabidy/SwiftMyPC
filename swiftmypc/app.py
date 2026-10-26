"""SwiftMyPC entry point: install / uninstall flows, then the main window (webapp)."""

import ctypes
import subprocess
import sys
import time

from . import __version__, installer
from .instance import SingleInstance

MB_YESNO, MB_ICONINFO, MB_ICONWARN, MB_ICONQUESTION, IDYES = 4, 0x40, 0x30, 0x20, 6


def _message(text, flags=MB_ICONINFO, title="SwiftMyPC") -> int:
    return ctypes.windll.user32.MessageBoxW(None, text, title, flags)


def _arg_value(args, name):
    return args[args.index(name) + 1] if name in args and args.index(name) + 1 < len(args) else None


def install_flow(silent: bool = False) -> bool:
    """Returns True when this process should exit (installed copy launched, or cancelled).
    silent: install without asking (SwiftMyPC.exe --install)."""
    if silent:
        choice, delete = "install", False
    else:
        from .webapp import run_install_dialog
        choice, delete = run_install_dialog(installer.installed_version())
    if choice is None:
        return True
    if choice == "run":
        # The dialog used up this process's window loop, so run the app as a fresh process.
        subprocess.Popen([sys.executable, "--portable"], env=installer.fresh_env())
        return True
    if choice == "open":
        installer.launch_installed()
        return True
    inst = SingleInstance()
    if inst.other_running() and not inst.ask_other_to_quit():
        _message("SwiftMyPC is still running. Quit it from its tray icon (right-click › Quit), "
                 "then open this file again.", MB_ICONWARN)
        return True
    last_error = None
    for _ in range(20):  # the old copy (or an antivirus scan) may hold the file for a moment
        try:
            installer.install(sys.executable, __version__)
            break
        except OSError as e:
            last_error = e
            time.sleep(1)
    else:
        hint = ("\n\nWindows (or your antivirus) is still busy with the previous copy. "
                "Wait a minute and open this file again." if isinstance(last_error, PermissionError) else "")
        _message(f"Installation failed:\n{last_error}{hint}", MB_ICONWARN)
        return True
    args = ["--installed"] + (["--cleanup", sys.executable] if delete else [])
    installer.launch_installed(*args)
    return True


def uninstall_flow(quiet: bool = False) -> None:
    if not quiet and _message(
            "Remove SwiftMyPC from this PC?\n\nThings it already cleaned stay cleaned. Service and "
            "startup changes stay as they are; restore them in the app first if you want them back.",
            MB_YESNO | MB_ICONQUESTION, "Uninstall SwiftMyPC") != IDYES:
        return
    inst = SingleInstance()
    if inst.other_running() and not inst.ask_other_to_quit():
        if not quiet:
            _message("Couldn't close the running SwiftMyPC. Quit it from its tray icon and try again.",
                     MB_ICONWARN)
        return
    installer.uninstall()
    if not quiet:
        _message("SwiftMyPC was removed.", MB_ICONINFO, "Uninstall SwiftMyPC")
    installer.schedule_folder_removal()  # deletes the folder (and this .exe) after we exit


def main():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        # Own taskbar identity, so Windows shows our icon instead of Python's.
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("SwiftMyPC")
    except Exception:
        pass
    args = sys.argv[1:]
    if "--uninstall" in args:
        uninstall_flow(quiet="--quiet" in args)
        return
    if installer.is_frozen() and not installer.running_installed() and "--portable" not in args:
        if install_flow(silent="--install" in args):
            return
    instance = SingleInstance()
    if not instance.acquire():
        if not instance.signal_existing():
            _message("SwiftMyPC is already running. Look for its icon in the system tray.")
        return
    from .webapp import run_app
    notice = "updated" if "--updated" in args else "installed" if "--installed" in args else None
    run_app(start_hidden="--tray" in args, instance=instance, notice=notice,
            cleanup_path=_arg_value(args, "--cleanup"))
