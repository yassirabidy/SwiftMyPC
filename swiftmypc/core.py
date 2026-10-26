"""Everything that runs in the background, with or without the window:
sampling, alerts, tray, daily Smart Optimize, update checks and the
single-instance signals. The web UI only reads from and calls into this."""

import json
import os
import sys
import threading
import time
from collections import deque

from . import __version__, installer, optimizer, startup, updater
from .instance import SingleInstance
from .monitor import ProcessSampler, fmt_bytes, system_stats
from .settings import Settings
from .tray import AlertWatcher, Tray

REFRESH_SECONDS = 2
HISTORY_POINTS = 300                      # 10 minutes at 2 s
AUTO_FIRST, AUTO_EVERY = 5 * 60, 30 * 60  # daily optimize: first check, then re-check interval
UPDATE_FIRST = int(os.environ.get("SWIFTMYPC_UPDATE_FIRST_MS", 60 * 1000)) / 1000  # env: tests only
UPDATE_EVERY = 6 * 3600


def resource_path(*parts: str) -> str:
    """Path to a bundled file, both from source and inside the PyInstaller .exe."""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    return os.path.join(base, *parts)


class Core:
    def __init__(self, instance: SingleInstance | None):
        self.instance = instance
        self.settings = Settings()
        self.admin = startup.is_admin()
        self.window = None
        self.visible = True
        self.exiting = False
        self.tray = None
        self.watcher = AlertWatcher()
        self.sampler = ProcessSampler()
        self.group = True
        self.stats = None
        self.apps = []
        self.history = deque(maxlen=HISTORY_POINTS)
        self.update_info: updater.Update | None = None
        self.updating = False
        self.optimizing = False
        self._started = time.time()

    # --- lifecycle -------------------------------------------------------------

    def start(self):
        try:
            self.tray = Tray(self, resource_path("assets", "icon.png"))
        except Exception:
            self.tray = None  # no tray: closing the window quits instead
        for target in (self._sample_loop, self._timer_loop, self._instance_loop):
            threading.Thread(target=target, daemon=True).start()

    def quit_app(self):
        self.exiting = True
        if self.tray:
            self.tray.stop()
        if self.instance:
            self.instance.release()
            self.instance = None
        if self.window:
            self.window.destroy()

    def restart_into(self, args: list[str]):
        """Start the installed copy with args and quit this one (after an update)."""
        if self.instance:
            self.instance.release()  # the new copy must be able to take over
            self.instance = None
        installer.launch_installed(*args)
        self.quit_app()

    # --- tray interface (see tray.Tray) -------------------------------------------

    def call_soon(self, fn):
        threading.Thread(target=fn, daemon=True).start()

    def show_window(self):
        if self.window:
            self.window.show()
            self.window.restore()
            self.visible = True
            self.push("shown", None)

    def hide_window(self):
        if self.window:
            self.window.hide()
            self.visible = False

    def set_alerts(self, enabled: bool):
        self.settings.set("alerts", enabled)
        if self.tray:
            self.tray.refresh_menu()
        self.push("settings", dict(self.settings))

    def notify(self, title: str, message: str):
        if self.tray:
            self.tray.notify(title, message)

    def push(self, event: str, data):
        """Send an event to the web UI (window.backend(...))."""
        if self.window and not self.exiting:
            payload = json.dumps({"event": event, "data": data})
            try:
                self.window.evaluate_js(f"window.backend && window.backend({payload})")
            except Exception:
                pass

    # --- loops -----------------------------------------------------------------

    def _sample_loop(self):
        # One long-lived thread: psutil tracks CPU deltas per thread.
        system_stats()
        self.sampler.sample(self.group)
        time.sleep(1)
        while not self.exiting:
            try:
                group = self.group
                stats, apps = system_stats(), self.sampler.sample(group)
                if group == self.group:
                    self.stats, self.apps = stats, apps
                self.history.append((round(stats["cpu"], 1), round(stats["mem_percent"], 1)))
                self._on_sample(stats, apps)
            except Exception:
                pass
            time.sleep(REFRESH_SECONDS)

    def _on_sample(self, stats, apps):
        if not self.tray:
            return
        self.tray.set_tooltip(f"SwiftMyPC · CPU {stats['cpu']:.0f}% · Memory {stats['mem_percent']:.0f}%")
        alerts = self.watcher.feed(stats, apps)
        if self.settings["alerts"]:
            for title, message in alerts:
                self.notify(title, message)

    def _timer_loop(self):
        next_auto = self._started + AUTO_FIRST
        next_update = self._started + UPDATE_FIRST
        while not self.exiting:
            now = time.time()
            if now >= next_auto:
                next_auto = now + AUTO_EVERY
                self._auto_optimize()
            if now >= next_update:
                next_update = now + UPDATE_EVERY
                if installer.is_frozen():
                    self.check_update(quiet=True)
            time.sleep(2)

    def _instance_loop(self):
        while not self.exiting:
            if self.instance and self.instance.show_requested():
                self.show_window()
            if self.instance and self.instance.quit_requested():
                self.quit_app()  # the installer is replacing or removing this copy
                return
            time.sleep(0.2)

    # --- daily smart optimize ---------------------------------------------------

    def _auto_optimize(self):
        s = self.settings
        cpu = self.stats["cpu"] if self.stats else 100
        if (not s["auto_optimize"] or self.optimizing
                or time.time() - s["last_auto_optimize"] < 24 * 3600 or cpu > 50):
            return
        self.optimizing = True
        try:
            actions = optimizer.plan(self.admin, auto_only=True)
            freed = optimizer.run(actions)
        except Exception:
            return
        finally:
            self.optimizing = False
            s.set("last_auto_optimize", time.time())
        closed = sum(a.count for a in actions if a.group == "apps")
        if freed >= 20 * 1024 ** 2 or closed:
            msg = f"Freed {fmt_bytes(freed)} of disk space"
            msg += f" and closed {closed} background helper{'s' * (closed > 1)}." if closed else "."
            self.notify("SwiftMyPC cleaned up", msg)

    # --- updates -----------------------------------------------------------------

    def check_update(self, quiet: bool):
        """Look for a newer release. Returns its info dict, or None."""
        if self.updating:
            return self._update_dict()
        update = updater.check()
        if not update:
            return None
        self.update_info = update
        self.push("update", self._update_dict())
        s = self.settings
        if quiet and s["auto_update"] and updater.can_self_update() and not self.visible:
            self.install_update(silent=True)  # nobody is looking: update quietly, come back to the tray
        elif quiet and s["notified_version"] != update.version:
            self.notify(f"SwiftMyPC {update.version} is available", "Open SwiftMyPC and click Update.")
            s.set("notified_version", update.version)
        return self._update_dict()

    def _update_dict(self):
        u = self.update_info
        return {"version": u.version, "current": __version__, "can_self_update": updater.can_self_update(),
                "page": updater.RELEASES_PAGE} if u else None

    def install_update(self, silent: bool):
        u = self.update_info
        if not u or self.updating:
            return
        self.updating = True
        last = [-1]

        def progress(done, total):
            pct = done * 100 // total if total else 0
            if pct != last[0]:
                last[0] = pct
                self.push("update_progress", pct)
        try:
            updater.apply(updater.download(u, progress), u.version)
        except Exception as e:
            self.updating = False
            self.push("update_failed", str(e))
            return
        self.restart_into(["--updated"] + (["--tray"] if silent else []))
