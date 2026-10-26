"""Tray icon and background alerts."""

import threading
import time

from .monitor import fmt_bytes


class AlertWatcher:
    """Turns the 2-second samples into a few useful notifications.

    An alert fires only when a problem lasts (not on a short spike), and each
    kind of alert is then quiet for a while so it never nags.
    """

    CPU_HIGH, CPU_FOR = 85, 60          # % of the machine, seconds
    MEM_HIGH, MEM_FOR = 90, 30
    HOG_CPU, HOG_FOR = 40, 120          # one app using this much for this long
    DISK_LOW = 5 * 1024 ** 3
    COOLDOWN = 15 * 60
    DISK_COOLDOWN = 6 * 3600

    def __init__(self):
        self._since: dict[str, float] = {}
        self._last: dict[str, float] = {}

    def _sustained(self, key: str, condition: bool, seconds: float, now: float) -> bool:
        if not condition:
            self._since.pop(key, None)
            return False
        return now - self._since.setdefault(key, now) >= seconds

    def _ready(self, key: str, now: float, cooldown: float) -> bool:
        if now - self._last.get(key, -1e12) < cooldown:
            return False
        self._last[key] = now
        return True

    def feed(self, stats: dict, apps: list, now: float | None = None) -> list[tuple[str, str]]:
        now = time.monotonic() if now is None else now
        alerts = []
        user_apps = [a for a in apps if not a.protected]
        top_cpu = max(user_apps, key=lambda a: a.cpu, default=None)
        top_mem = max(user_apps, key=lambda a: a.memory, default=None)

        if (self._sustained("cpu", stats["cpu"] >= self.CPU_HIGH, self.CPU_FOR, now)
                and self._ready("cpu", now, self.COOLDOWN)):
            msg = f"CPU has been above {self.CPU_HIGH}% for a minute."
            if top_cpu and top_cpu.cpu >= 5:
                msg += f" Biggest user: {top_cpu.name} ({top_cpu.cpu:.0f}%)."
            alerts.append(("High CPU usage", msg))

        if (self._sustained("mem", stats["mem_percent"] >= self.MEM_HIGH, self.MEM_FOR, now)
                and self._ready("mem", now, self.COOLDOWN)):
            msg = f"Memory is {stats['mem_percent']:.0f}% full."
            if top_mem:
                msg += f" Biggest user: {top_mem.name} ({fmt_bytes(top_mem.memory)})."
            alerts.append(("Memory almost full", msg))

        hogs = set()
        for a in user_apps:
            key = "hog:" + a.key
            if a.cpu >= self.HOG_CPU:
                hogs.add(key)
            if (self._sustained(key, a.cpu >= self.HOG_CPU, self.HOG_FOR, now)
                    and self._ready(key, now, self.COOLDOWN)):
                alerts.append((f"{a.name} is working hard",
                               f"{a.name} has used about {a.cpu:.0f}% of your CPU for 2 minutes. "
                               "Close it if you're not using it."))
        for key in [k for k in self._since if k.startswith("hog:") and k not in hogs]:
            del self._since[key]  # app exited or calmed down

        if stats["disk_free"] < self.DISK_LOW and self._ready("disk", now, self.DISK_COOLDOWN):
            alerts.append(("Disk almost full",
                           f"Drive {stats['disk_drive']} has only {fmt_bytes(stats['disk_free'])} free. "
                           "Open SwiftMyPC → Cleaner to free space."))
        return alerts


class Tray:
    """System tray icon. Menu callbacks run on pystray's thread, so they are
    handed to the UI thread through app.call_soon()."""

    def __init__(self, app, icon_path: str):
        import pystray
        from PIL import Image

        self.app = app
        s = app.settings
        menu = pystray.Menu(
            pystray.MenuItem("Open SwiftMyPC", lambda: app.call_soon(app.show_window), default=True),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Alerts", lambda: app.call_soon(lambda: app.set_alerts(not s["alerts"])),
                             checked=lambda _: s["alerts"]),
            pystray.MenuItem("Keep running when closed",
                             lambda: app.call_soon(lambda: s.set("close_to_tray", not s["close_to_tray"])),
                             checked=lambda _: s["close_to_tray"]),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", lambda: app.call_soon(app.quit_app)),
        )
        self.icon = pystray.Icon("SwiftMyPC", Image.open(icon_path), "SwiftMyPC", menu)
        threading.Thread(target=self.icon.run, daemon=True).start()

    def notify(self, title: str, message: str) -> None:
        try:
            self.icon.notify(message, title)
        except Exception:
            pass

    def set_tooltip(self, text: str) -> None:
        try:
            self.icon.title = text[:120]
        except Exception:
            pass

    def refresh_menu(self) -> None:
        try:
            self.icon.update_menu()
        except Exception:
            pass

    def stop(self) -> None:
        try:
            self.icon.stop()
        except Exception:
            pass
