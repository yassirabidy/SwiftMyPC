"""The window: an HTML/CSS interface (swiftmypc/ui) rendered with Edge WebView2 via
pywebview. Api is what the page's JavaScript calls (window.pywebview.api.*)."""

import ctypes
import os
import subprocess
import sys
import threading
import time
import webbrowser
import winreg

import psutil
import webview

from . import __version__, cleaner, installer, optimizer, services, startup, updater
from .core import Core, resource_path
from .health import run_health_check
from .monitor import end_processes
from .settings import set_start_with_windows, starts_with_windows

LIGHT_BG, DARK_BG = "#f6f7fb", "#0e0f14"
LIGHT_TEXT, DARK_TEXT = "#1b1c24", "#e9eaf2"


# --- title bar --------------------------------------------------------------------

def system_prefers_dark() -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as key:
            return winreg.QueryValueEx(key, "AppsUseLightTheme")[0] == 0
    except OSError:
        return False


def is_dark(theme: str) -> bool:
    return theme == "dark" or (theme == "system" and system_prefers_dark())


def _colorref(hex_color: str) -> int:
    r, g, b = (int(hex_color[i:i + 2], 16) for i in (1, 3, 5))
    return r | (g << 8) | (b << 16)


def style_title_bar(window, dark: bool) -> None:
    """Blend the native title bar into the page: dark mode, and on Windows 11 the
    exact background and text colors (older Windows ignores what it can't do)."""
    try:
        hwnd = window.native.Handle.ToInt64()
    except Exception:
        return
    dwm = ctypes.windll.dwmapi
    for attr, value in ((20, 1 if dark else 0),                                   # immersive dark mode
                        (35, _colorref(DARK_BG if dark else LIGHT_BG)),           # caption color
                        (36, _colorref(DARK_TEXT if dark else LIGHT_TEXT))):      # caption text color
        v = ctypes.c_int(value)
        dwm.DwmSetWindowAttribute(ctypes.c_void_p(hwnd), attr, ctypes.byref(v), ctypes.sizeof(v))
    # SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER | SWP_FRAMECHANGED: repaint the frame now.
    ctypes.windll.user32.SetWindowPos(ctypes.c_void_p(hwnd), None, 0, 0, 0, 0, 0x0027)


# --- API for the page ---------------------------------------------------------------

def _app_dict(a):
    return {"key": a.key, "name": a.name, "count": a.count, "cpu": round(a.cpu, 1), "mem": a.memory,
            "desc": a.description, "protected": a.protected}


def _action_dict(a: optimizer.Action):
    return {"key": a.key, "group": a.group, "label": a.label, "detail": a.detail, "size": a.size,
            "count": a.count, "needs_admin": a.needs_admin, "result": a.result}


class Api:
    """Every public method is callable from JavaScript and returns plain JSON data.
    Attributes are private (underscore) so pywebview doesn't try to expose them."""

    def __init__(self, core: Core, notice: str | None):
        self._core = core
        self._notice = notice
        self._startup: list[startup.StartupItem] = []
        self._services: dict[str, services.Service] = {}
        self._targets: dict[str, cleaner.Target] = {}
        self._plan: dict[str, optimizer.Action] = {}
        self._lock = threading.Lock()

    # app ---------------------------------------------------------------------------

    def bootstrap(self):
        c = self._core
        notice, self._notice = self._notice, None
        return {"version": __version__, "admin": c.admin, "frozen": installer.is_frozen(),
                "settings": self.get_settings(), "update": c._update_dict(), "notice": notice,
                "boot_time": psutil.boot_time()}

    def get_settings(self):
        s = dict(self._core.settings)
        s["start_with_windows"] = starts_with_windows()
        s["has_tray"] = self._core.tray is not None
        return s

    def set_setting(self, key, value):
        c = self._core
        if key == "start_with_windows":
            set_start_with_windows(bool(value))
        elif key == "alerts":
            c.set_alerts(bool(value))
        elif key in ("close_to_tray", "auto_optimize", "auto_update"):
            c.settings.set(key, bool(value))
            if c.tray:
                c.tray.refresh_menu()
        elif key == "theme" and value in ("system", "light", "dark"):
            c.settings.set("theme", value)
            if c.window:
                style_title_bar(c.window, is_dark(value))
        return self.get_settings()

    def hide_window(self):
        self._core.hide_window()

    def quit_app(self):
        self._core.quit_app()

    def open_url(self, url):
        if str(url).startswith(("https://", "http://")):
            webbrowser.open(url)

    def open_location(self, path):
        if path and os.path.exists(path):
            subprocess.Popen(f'explorer /select,"{path}"')
            return True
        return False

    def restart_admin(self):
        c = self._core
        if installer.is_frozen():
            exe, args, cwd = sys.executable, "", None
        else:
            exe = sys.executable
            args = "-m swiftmypc"
            cwd = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        if c.instance:
            c.instance.release()  # let the elevated copy become the running instance
            c.instance = None
        os.environ.update(installer.fresh_env())
        rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, args, cwd, 1)
        if rc > 32:
            c.quit_app()
            return True
        from .instance import SingleInstance
        c.instance = SingleInstance()
        c.instance.acquire()  # UAC was cancelled; keep being the running copy
        return False

    # monitor -----------------------------------------------------------------------

    def snapshot(self):
        c = self._core
        apps = list(c.apps)
        return {"stats": c.stats, "apps": [_app_dict(a) for a in apps], "history": list(c.history),
                "group": c.group, "processes": sum(a.count for a in apps), "cores": psutil.cpu_count()}

    def set_grouping(self, group):
        self._core.group = bool(group)

    def end_task(self, key):
        app = next((a for a in self._core.apps if a.key == key), None)
        if not app:
            return {"error": "That app isn't running any more."}
        if app.protected:
            return {"error": f"{app.name} is a core part of Windows, so SwiftMyPC won't end it."}
        ended, denied = end_processes(app.pids)
        return {"ended": ended, "denied": denied, "name": app.name}

    def end_unnecessary(self):
        actions = optimizer.plan_unnecessary(self._core.admin)
        optimizer.run(actions)
        return {"actions": [_action_dict(a) for a in actions], "freed": sum(a.size for a in actions)}

    # startup -------------------------------------------------------------------------

    def startup_list(self):
        self._startup = startup.list_startup()
        return [{"id": i, "name": it.name, "command": it.command, "location": it.location,
                 "enabled": it.enabled, "needs_admin": it.needs_admin, "path": it.path}
                for i, it in enumerate(self._startup)]

    def startup_set(self, item_id, enabled):
        try:
            item = self._startup[int(item_id)]
            startup.set_enabled(item, bool(enabled))
            return {"ok": True, "enabled": item.enabled}
        except (IndexError, ValueError):
            return {"error": "That startup entry changed. Refresh and try again."}
        except OSError:
            return {"error": "This one is set up for all users and needs administrator rights."}

    # services ------------------------------------------------------------------------

    def services_list(self):
        self._services = {s.name: s for s in services.list_services()}
        return [{"name": s.name, "display": s.display, "status": s.status, "start": s.start_label,
                 "source": s.source, "memory": s.memory, "advice": s.advice, "level": s.level,
                 "can_change": s.can_change, "description": s.description, "binpath": s.binpath}
                for s in self._services.values()]

    def service_action(self, names, action):
        if not self._core.admin:
            return {"errors": ["Changing services needs administrator rights."]}
        fn = {"manual": lambda s: services.set_start(s, "demand"),
              "disabled": lambda s: services.set_start(s, "disabled"),
              "restore": services.restore, "stop": services.stop}.get(action)
        errors = []
        for name in names:
            s = self._services.get(name)
            if not s or not fn:
                continue
            try:
                if fn(s) is False:
                    errors.append(f"{s.display}: nothing to restore (SwiftMyPC never changed it)")
            except OSError as e:
                errors.append(f"{s.display}: {e}")
        return {"errors": errors}

    # cleaner -------------------------------------------------------------------------

    def cleaner_scan(self):
        self._targets = {t.key: t for t in cleaner.targets()}
        out = []
        for t in self._targets.values():
            size, count = cleaner.scan(t)
            out.append({"key": t.key, "label": t.label, "description": t.description, "size": size,
                        "count": count, "needs_admin": t.needs_admin, "recycle": t.recycle_bin})
        return out

    def cleaner_clean(self, keys):
        freed = deleted = skipped = 0
        for key in keys:
            t = self._targets.get(key)
            if t:
                f, d, s = cleaner.clean(t)
                freed, deleted, skipped = freed + f, deleted + d, skipped + s
        return {"freed": freed, "deleted": deleted, "skipped": skipped}

    # smart optimize -------------------------------------------------------------------

    def optimize_plan(self):
        admin = self._core.admin
        actions = optimizer.plan(admin)
        self._plan = {a.key: a for a in actions}
        return {"actions": [_action_dict(a) for a in actions],
                "admin_extra": [] if admin else optimizer.admin_only_preview(),
                "can_undo": optimizer.can_undo()}

    def optimize_run(self, keys):
        chosen = [self._plan[k] for k in keys if k in self._plan and self._plan[k].group != "note"]
        freed = optimizer.run(chosen)
        return {"actions": [_action_dict(a) for a in chosen], "freed": freed,
                "closed": sum(a.count for a in chosen if a.group == "apps"),
                "can_undo": optimizer.can_undo()}

    def optimize_undo(self):
        return {"message": optimizer.undo(), "can_undo": optimizer.can_undo()}

    def health_check(self):
        return [{"level": lvl, "text": text} for lvl, text in run_health_check(self._core.admin)]

    # updates ---------------------------------------------------------------------------

    def check_update(self):
        if not installer.is_frozen():
            return {"source": True}
        try:
            return {"update": self._core.check_update(quiet=False)}
        except Exception as e:
            return {"error": str(e)}

    def install_update(self):
        c = self._core
        if not updater.can_self_update():
            webbrowser.open(updater.RELEASES_PAGE)  # not the installed copy: get it by hand
            return {"opened_page": True}
        threading.Thread(target=c.install_update, args=(False,), daemon=True).start()
        return {"started": True}


# --- windows ----------------------------------------------------------------------------

def _delete_download(path: str) -> None:
    """Remove the downloaded installer the user ran, once that process has exited."""
    name = os.path.basename(path).lower()
    if (not name.startswith("swiftmypc") or not name.endswith(".exe")
            or installer._same(path, installer.installed_exe())):
        return  # only ever our own downloaded .exe
    for _ in range(20):
        try:
            os.remove(path)
            return
        except FileNotFoundError:
            return
        except OSError:
            time.sleep(0.5)


def run_app(start_hidden: bool, instance, notice: str | None, cleanup_path: str | None) -> None:
    core = Core(instance)
    api = Api(core, notice)
    dark = is_dark(core.settings["theme"])
    window = webview.create_window(
        "SwiftMyPC", url=resource_path("swiftmypc", "ui", "index.html"), js_api=api,
        width=1200, height=800, min_size=(940, 620), hidden=start_hidden,
        background_color=DARK_BG if dark else LIGHT_BG)
    core.window = window
    core.visible = not start_hidden

    def on_shown():
        style_title_bar(window, is_dark(core.settings["theme"]))

    def on_closing():
        if core.exiting:
            return True
        if core.tray and core.settings["close_to_tray"]:
            core.hide_window()
            if not core.settings["tray_hint_shown"]:
                core.notify("SwiftMyPC is still running",
                            "It keeps watching for problems from the tray. Right-click the icon to quit.")
                core.settings.set("tray_hint_shown", True)
            return False
        core.exiting = True
        if core.tray:
            core.tray.stop()
        if core.instance:
            core.instance.release()
        return True

    def on_started():
        core.start()
        if installer.running_installed():
            try:
                installer.register(__version__)  # keep Settings > Apps showing the right version
            except OSError:
                pass
        if notice == "updated":
            updater.cleanup_after_update()
            core.notify("SwiftMyPC updated", f"You're now on version {__version__}.")
        elif notice == "installed":
            core.notify("SwiftMyPC is installed",
                        "Open it any time from the Desktop or Start menu shortcut. It updates itself.")
        if cleanup_path:
            _delete_download(cleanup_path)

    window.events.shown += on_shown
    window.events.closing += on_closing
    webview.start(on_started, private_mode=False, storage_path=os.path.join(
        os.environ.get("LOCALAPPDATA", ""), "SwiftMyPC", "webview"), icon=resource_path("assets", "icon.ico"))


class _InstallApi:
    def __init__(self, installed: str | None):
        self._installed = installed
        self._window = None
        self._result = (None, False)  # (choice, delete_download); stays None if the window is closed

    def info(self):
        return {"installed": self._installed, "version": __version__, "dark": system_prefers_dark()}

    def choose(self, choice, delete):
        self._result = (choice, bool(delete))
        self._window.destroy()


def run_install_dialog(installed: str | None):
    """Shows the install prompt. Returns (choice, delete_download); choice is None if closed."""
    api = _InstallApi(installed)
    dark = system_prefers_dark()
    window = webview.create_window(
        "Install SwiftMyPC", url=resource_path("swiftmypc", "ui", "install.html"), js_api=api,
        width=520, height=440, resizable=False, background_color=DARK_BG if dark else LIGHT_BG)
    api._window = window
    window.events.shown += lambda: style_title_bar(window, dark)
    webview.start(private_mode=True, icon=resource_path("assets", "icon.ico"))
    return api._result
