"""SwiftMyPC main window."""

import ctypes
import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk

import psutil

from . import __version__, cleaner, optimizer, services, startup
from .charts import HistoryChart
from .monitor import ProcessSampler, end_processes, fmt_bytes, system_stats
from .settings import Settings, set_start_with_windows, starts_with_windows
from .tray import AlertWatcher, Tray

BG = "#14161b"
PANEL = "#1d2027"
PANEL_HI = "#272b34"
TEXT = "#e6e8ee"
MUTED = "#9aa3b2"
ACCENT = "#4f8cff"
OK = "#3ecf8e"
WARN = "#f0a63a"
DANGER = "#ef5b5b"
PURPLE = "#b07cff"
FONT = "Segoe UI"

REFRESH_MS = 2000
HISTORY_SECONDS = 600
AUTO_FIRST_MS = 5 * 60 * 1000     # first automatic optimize check after start
AUTO_CHECK_MS = 30 * 60 * 1000    # then every 30 minutes (runs at most once a day)


def resource_path(*parts: str) -> str:
    """Path to a bundled file, both from source and inside the PyInstaller .exe."""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    return os.path.join(base, *parts)


class SingleInstance:
    """Only one SwiftMyPC at a time; starting a second one shows the first."""

    MUTEX, EVENT = "Local\\SwiftMyPC.Instance", "Local\\SwiftMyPC.Show"

    def __init__(self):
        k = self.kernel32 = ctypes.windll.kernel32
        k.CreateMutexW.restype = k.CreateEventW.restype = k.OpenEventW.restype = ctypes.c_void_p
        self.mutex = self.event = None

    def acquire(self) -> bool:
        k = self.kernel32
        self.mutex = k.CreateMutexW(None, False, self.MUTEX)
        if k.GetLastError() in (183, 5):  # already exists / owned by an elevated copy
            k.CloseHandle(ctypes.c_void_p(self.mutex))
            self.mutex = None
            return False
        self.event = k.CreateEventW(None, False, False, self.EVENT)
        return True

    def signal_existing(self) -> bool:
        ev = self.kernel32.OpenEventW(0x0002, False, self.EVENT)  # EVENT_MODIFY_STATE
        if not ev:
            return False
        self.kernel32.SetEvent(ctypes.c_void_p(ev))
        self.kernel32.CloseHandle(ctypes.c_void_p(ev))
        return True

    def show_requested(self) -> bool:
        return bool(self.event) and self.kernel32.WaitForSingleObject(ctypes.c_void_p(self.event), 0) == 0

    def release(self):
        for h in (self.mutex, self.event):
            if h:
                self.kernel32.CloseHandle(ctypes.c_void_p(h))
        self.mutex = self.event = None


class App(tk.Tk):
    def __init__(self, start_hidden: bool = False, instance: SingleInstance | None = None):
        super().__init__()
        self.title("SwiftMyPC")
        self.geometry("1100x820")
        self.minsize(900, 600)
        self.configure(bg=BG)
        if start_hidden:
            self.withdraw()
        try:
            self.iconbitmap(default=resource_path("assets", "icon.ico"))
        except tk.TclError:
            pass
        self._queue: queue.Queue = queue.Queue()
        self.instance = instance
        self.admin = startup.is_admin()
        self.settings = Settings()
        self.watcher = AlertWatcher()
        self._optimizer_win = None
        self._optimizing = False
        self._last_cpu = None
        self.tray = None
        try:
            self.tray = Tray(self, resource_path("assets", "icon.png"))
        except Exception:
            self.deiconify()  # no tray (e.g. pystray missing): never stay invisible

        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self._build_style()
        self._build_header()

        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=16, pady=(0, 16))
        self.monitor_tab = MonitorTab(nb, self)
        self.startup_tab = StartupTab(nb, self)
        self.services_tab = ServicesTab(nb, self)
        self.cleaner_tab = CleanerTab(nb, self)
        nb.add(self.monitor_tab, text="  Monitor  ")
        nb.add(self.startup_tab, text="  Startup apps  ")
        nb.add(self.services_tab, text="  Services  ")
        nb.add(self.cleaner_tab, text="  Cleaner  ")

        self.after(100, self._drain)
        self.after(AUTO_FIRST_MS, self._auto_optimize_tick)

    # --- window / tray -----------------------------------------------------------

    def call_soon(self, fn):
        """Run fn on the UI thread (safe to call from any thread)."""
        self._queue.put((lambda _r, _e: fn(), None, None))

    def show_window(self):
        self.deiconify()
        self.state("normal")
        self.lift()
        self.focus_force()

    def on_close(self):
        if self.tray and self.settings["close_to_tray"]:
            self.withdraw()
            if not self.settings["tray_hint_shown"]:
                self.tray.notify("SwiftMyPC is still running",
                                 "It keeps watching for problems from the tray. "
                                 "Right-click the icon to quit.")
                self.settings.set("tray_hint_shown", True)
        else:
            self.quit_app()

    def quit_app(self):
        if self.tray:
            self.tray.stop()
        if self.instance:
            self.instance.release()
        self.destroy()

    def set_alerts(self, enabled: bool):
        self.settings.set("alerts", enabled)
        self.alerts_var.set(enabled)
        if self.tray:
            self.tray.refresh_menu()

    # --- smart optimize --------------------------------------------------------

    def open_optimizer(self):
        if self._optimizer_win is not None and self._optimizer_win.winfo_exists():
            self._optimizer_win.lift()
            return
        self.show_window()
        self._optimizer_win = OptimizeDialog(self)

    def _auto_optimize_tick(self):
        """Daily automatic run: junk files and background helpers only, when the PC isn't busy."""
        self.after(AUTO_CHECK_MS, self._auto_optimize_tick)
        s = self.settings
        if (not s["auto_optimize"] or self._optimizing
                or time.time() - s["last_auto_optimize"] < 24 * 3600
                or self._last_cpu is None or self._last_cpu > 50):
            return
        self._optimizing = True

        def work():
            actions = optimizer.plan(self.admin, auto_only=True)
            return actions, optimizer.run(actions)

        def done(result, err):
            self._optimizing = False
            self.settings.set("last_auto_optimize", time.time())
            if err or not self.tray:
                return
            actions, freed = result
            closed = sum(a.count for a in actions if a.group == "apps")
            if freed >= 20 * 1024 ** 2 or closed:
                msg = f"Freed {fmt_bytes(freed)} of disk space"
                msg += f" and closed {closed} background helper{'s' * (closed > 1)}." if closed else "."
                self.tray.notify("SwiftMyPC cleaned up", msg)
        self.run_bg(work, done)

    def on_sample(self, stats, apps):
        """Called every refresh by the Monitor tab, even while the window is hidden."""
        self._last_cpu = stats["cpu"]
        if not self.tray:
            return
        self.tray.set_tooltip(f"SwiftMyPC · CPU {stats['cpu']:.0f}% · Memory {stats['mem_percent']:.0f}%")
        alerts = self.watcher.feed(stats, apps)
        if self.settings["alerts"]:
            for title, message in alerts:
                self.tray.notify(title, message)

    # --- background work -------------------------------------------------------

    def run_bg(self, fn, callback=None):
        """Run fn in a thread; callback(result, error) runs on the UI thread."""
        def worker():
            try:
                res, err = fn(), None
            except Exception as e:  # reported to the callback
                res, err = None, e
            self._queue.put((callback, res, err))
        threading.Thread(target=worker, daemon=True).start()

    def _drain(self):
        try:
            while True:
                cb, res, err = self._queue.get_nowait()
                if cb:
                    cb(res, err)
        except queue.Empty:
            pass
        if self.instance and self.instance.show_requested():
            self.show_window()
        self.after(100, self._drain)

    # --- layout ------------------------------------------------------------------

    def _build_style(self):
        s = ttk.Style(self)
        s.theme_use("clam")
        s.configure(".", background=BG, foreground=TEXT, font=(FONT, 10),
                    bordercolor=PANEL_HI, lightcolor=PANEL, darkcolor=PANEL,
                    troughcolor=PANEL_HI, focuscolor=ACCENT)
        s.configure("TFrame", background=BG)
        s.configure("Card.TFrame", background=PANEL)
        s.configure("TLabel", background=BG, foreground=TEXT)
        s.configure("Muted.TLabel", background=BG, foreground=MUTED)
        s.configure("Card.TLabel", background=PANEL, foreground=MUTED)
        s.configure("CardValue.TLabel", background=PANEL, foreground=TEXT, font=(FONT, 20, "bold"))
        s.configure("Title.TLabel", background=BG, foreground=TEXT, font=(FONT, 20, "bold"))
        s.configure("TButton", background=PANEL_HI, foreground=TEXT, borderwidth=0, padding=(14, 7))
        s.map("TButton", background=[("active", "#323744"), ("disabled", PANEL)],
              foreground=[("disabled", MUTED)])
        s.configure("Accent.TButton", background=ACCENT, foreground="white")
        s.map("Accent.TButton", background=[("active", "#3a76e6")])
        s.configure("Danger.TButton", background="#5a2a2e", foreground="#ffd7d7")
        s.map("Danger.TButton", background=[("active", "#74333a")])
        s.configure("TCheckbutton", background=BG, foreground=TEXT)
        s.map("TCheckbutton", background=[("active", BG)])
        s.configure("Card.TCheckbutton", background=PANEL, foreground=TEXT, font=(FONT, 10, "bold"))
        s.map("Card.TCheckbutton", background=[("active", PANEL)])
        s.configure("TEntry", fieldbackground=PANEL, foreground=TEXT, insertcolor=TEXT, padding=6)
        s.configure("TCombobox", fieldbackground=PANEL, background=PANEL_HI, foreground=TEXT,
                    arrowcolor=TEXT, padding=5)
        s.map("TCombobox", fieldbackground=[("readonly", PANEL)], foreground=[("readonly", TEXT)],
              selectbackground=[("readonly", PANEL)], selectforeground=[("readonly", TEXT)])
        self.option_add("*TCombobox*Listbox.background", PANEL)
        self.option_add("*TCombobox*Listbox.foreground", TEXT)
        self.option_add("*TCombobox*Listbox.selectBackground", "#2c4a80")
        s.configure("TMenubutton", background=PANEL_HI, foreground=TEXT, borderwidth=0,
                    padding=(14, 7), arrowcolor=TEXT)
        s.map("TMenubutton", background=[("active", "#323744")])
        s.configure("TNotebook", background=BG, borderwidth=0, tabmargins=0)
        s.configure("TNotebook.Tab", background=BG, foreground=MUTED, padding=(10, 8), borderwidth=0)
        s.map("TNotebook.Tab", background=[("selected", PANEL)], foreground=[("selected", TEXT)])
        s.configure("Treeview", background=PANEL, fieldbackground=PANEL, foreground=TEXT,
                    rowheight=28, borderwidth=0)
        s.map("Treeview", background=[("selected", "#2c4a80")])
        s.configure("Treeview.Heading", background=PANEL_HI, foreground=MUTED,
                    font=(FONT, 9, "bold"), borderwidth=0, padding=6)
        s.map("Treeview.Heading", background=[("active", "#323744")])
        for name, color in (("Ok", OK), ("Warn", WARN), ("Danger", DANGER)):
            s.configure(f"{name}.Horizontal.TProgressbar", background=color,
                        troughcolor=PANEL_HI, borderwidth=0, thickness=6)

    def _build_header(self):
        h = ttk.Frame(self)
        h.pack(fill="x", padx=16, pady=(14, 10))
        ttk.Label(h, text="SwiftMyPC", style="Title.TLabel").pack(side="left")
        ttk.Label(h, text=f"  v{__version__}  ·  keep your PC quick", style="Muted.TLabel").pack(
            side="left", pady=(8, 0))
        ttk.Button(h, text="⚡ Smart Optimize", style="Accent.TButton",
                   command=self.open_optimizer).pack(side="right")
        ttk.Button(h, text="Health check", command=self.health_check).pack(side="right", padx=(8, 0))
        self._build_settings_menu(h).pack(side="right", padx=(8, 0))
        if self.admin:
            ttk.Label(h, text="Administrator  ", foreground=OK).pack(side="right")
        else:
            ttk.Button(h, text="Restart as admin", command=self.restart_as_admin).pack(
                side="right", padx=(8, 0))

    def _build_settings_menu(self, parent):
        btn = ttk.Menubutton(parent, text="Settings")
        menu = tk.Menu(btn, tearoff=False, bg=PANEL, fg=TEXT, activebackground="#2c4a80",
                       activeforeground=TEXT, selectcolor=TEXT, bd=0)
        self.alerts_var = tk.BooleanVar(value=self.settings["alerts"])
        tray_var = tk.BooleanVar(value=self.settings["close_to_tray"])
        boot_var = tk.BooleanVar(value=starts_with_windows())

        def toggle_tray():
            self.settings.set("close_to_tray", tray_var.get())
            if self.tray:
                self.tray.refresh_menu()

        def toggle_boot():
            try:
                set_start_with_windows(boot_var.get())
            except OSError as e:
                boot_var.set(starts_with_windows())
                messagebox.showerror("Settings", str(e), parent=self)

        state = "normal" if self.tray else "disabled"
        menu.add_checkbutton(label="Alerts for high CPU, memory and low disk", variable=self.alerts_var,
                             command=lambda: self.set_alerts(self.alerts_var.get()), state=state)
        menu.add_checkbutton(label="Keep running in the tray when closed", variable=tray_var,
                             command=toggle_tray, state=state)
        menu.add_checkbutton(label="Start with Windows (in the tray)", variable=boot_var,
                             command=toggle_boot)
        auto_var = tk.BooleanVar(value=self.settings["auto_optimize"])
        menu.add_checkbutton(label="Smart Optimize automatically once a day (junk files + helpers)",
                             variable=auto_var,
                             command=lambda: self.settings.set("auto_optimize", auto_var.get()))
        menu.add_separator()
        menu.add_command(label="Quit SwiftMyPC", command=self.quit_app)
        btn["menu"] = menu
        menu.bind("<Map>", lambda _: (tray_var.set(self.settings["close_to_tray"]),
                                      self.alerts_var.set(self.settings["alerts"])))
        return btn

    def restart_as_admin(self):
        if getattr(sys, "frozen", False):
            exe, args, cwd = sys.executable, "", None
        else:
            exe = sys.executable
            args = "-m swiftmypc"
            cwd = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        if self.instance:
            self.instance.release()  # let the elevated copy become the running instance
        rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, args, cwd, 1)
        if rc > 32:
            self.instance = None
            self.quit_app()
        elif self.instance:
            self.instance.acquire()  # UAC was cancelled; keep being the running copy

    # --- health check ----------------------------------------------------------

    def health_check(self):
        win = tk.Toplevel(self, bg=BG)
        win.title("Health check")
        win.geometry("640x520")
        win.transient(self)
        ttk.Label(win, text="Health check", style="Title.TLabel").pack(anchor="w", padx=20, pady=(16, 4))
        status = ttk.Label(win, text="Checking your PC… (a few seconds)", style="Muted.TLabel")
        status.pack(anchor="w", padx=20)
        body = tk.Frame(win, bg=BG)
        body.pack(fill="both", expand=True, padx=20, pady=12)

        def show(findings, err):
            if not win.winfo_exists():
                return
            if err:
                status.config(text=f"Health check failed: {err}")
                return
            warns = sum(1 for lvl, _ in findings if lvl == "warn")
            status.config(text="Looking good!" if not warns else
                          f"{warns} thing{'s' if warns > 1 else ''} worth fixing:")
            colors = {"ok": OK, "warn": WARN, "tip": ACCENT}
            marks = {"ok": "✓", "warn": "!", "tip": "→"}
            for lvl, text in findings:
                row = tk.Frame(body, bg=PANEL, padx=12, pady=10)
                row.pack(fill="x", pady=4)
                tk.Label(row, text=marks[lvl], fg=colors[lvl], bg=PANEL,
                         font=(FONT, 12, "bold"), width=2).pack(side="left", anchor="n")
                tk.Label(row, text=text, fg=TEXT, bg=PANEL, font=(FONT, 10),
                         wraplength=540, justify="left").pack(side="left", fill="x")

        sizes = dict(self.cleaner_tab.sizes)
        self.run_bg(lambda: run_health_check(self.admin, sizes), show)


class OptimizeDialog(tk.Toplevel):
    GROUPS = (("files", "Free disk space"), ("apps", "Free memory"),
              ("services", "Lighter background"), ("startup", "Faster startup"))

    def __init__(self, app: App):
        super().__init__(app, bg=BG)
        self.app = app
        self.title("Smart Optimize")
        self.geometry("780x800")
        self.minsize(700, 600)
        self.transient(app)
        self.actions: list[optimizer.Action] = []
        self.vars: dict[str, tk.BooleanVar] = {}
        self.results: dict[str, tk.Label] = {}

        ttk.Label(self, text="⚡ Smart Optimize", style="Title.TLabel").pack(anchor="w", padx=20, pady=(16, 2))
        ttk.Label(self, text="Finds and applies only safe fixes. No personal files are deleted, no app you "
                             "have open is closed, and every setting change can be undone.",
                  style="Muted.TLabel", wraplength=720, justify="left").pack(anchor="w", padx=20)
        self.status = ttk.Label(self, text="Scanning your PC…", font=(FONT, 12, "bold"))
        self.status.pack(anchor="w", padx=20, pady=(12, 6))
        bottom = ttk.Frame(self)
        bottom.pack(side="bottom", fill="x", padx=20, pady=16)  # packed first so it's never cut off
        self.body = tk.Frame(self, bg=BG)
        self.body.pack(fill="both", expand=True, padx=20)

        self.rescan = ttk.Button(bottom, text="Scan again", command=self.scan)
        self.rescan.pack(side="left")
        self.go = ttk.Button(bottom, text="Optimize now", style="Accent.TButton", command=self.optimize)
        self.go.pack(side="right")
        self.undo_btn = ttk.Button(bottom, text="Undo last changes", command=self.undo)
        self.scan()

    # --- scanning --------------------------------------------------------------

    def scan(self):
        for w in self.body.winfo_children():
            w.destroy()
        self.status.config(text="Scanning your PC…")
        self.go.state(["disabled"])
        self.rescan.state(["disabled"])
        self.go.config(text="Optimize now", command=self.optimize)
        self._update_undo()
        admin = self.app.admin

        def work():
            return optimizer.plan(admin), ([] if admin else optimizer.admin_only_preview())

        self.app.run_bg(work, self._show_plan)

    def _show_plan(self, result, err):
        if not self.winfo_exists():
            return
        self.rescan.state(["!disabled"])
        if err:
            self.status.config(text=f"Scan failed: {err}")
            return
        self.actions, admin_extra = result
        self.vars, self.results = {}, {}
        doable = [a for a in self.actions if a.group != "note"]
        if not doable:
            self.status.config(text="Your PC is already in good shape. Nothing to optimize right now.")
        for group, title in self.GROUPS:
            items = [a for a in self.actions if a.group == group or (group == "files" and a.group == "note")]
            if not items:
                continue
            tk.Label(self.body, text=title.upper(), bg=BG, fg=MUTED, font=(FONT, 8, "bold")).pack(
                anchor="w", pady=(8, 2))
            for a in items:
                self._row(a)
        if admin_extra:
            row = tk.Frame(self.body, bg=BG)
            row.pack(fill="x", pady=(10, 0))
            tk.Label(row, text="Run as administrator to also include: " + ", ".join(admin_extra) + ".",
                     bg=BG, fg=MUTED, font=(FONT, 9)).pack(side="left")
            ttk.Button(row, text="Restart as admin", command=self.app.restart_as_admin).pack(side="right")
        self._update_total()
        if doable:
            self.go.state(["!disabled"])

    def _row(self, a: optimizer.Action):
        row = tk.Frame(self.body, bg=PANEL, padx=12, pady=8)
        row.pack(fill="x", pady=2)
        left = tk.Frame(row, bg=PANEL)
        left.pack(side="left", fill="x", expand=True)
        if a.group == "note":
            tk.Label(left, text="ℹ  " + a.label, bg=PANEL, fg=MUTED, font=(FONT, 10, "bold")).pack(anchor="w")
        else:
            var = tk.BooleanVar(value=True)
            self.vars[a.key] = var
            ttk.Checkbutton(left, text=a.label, variable=var, style="Card.TCheckbutton",
                            command=self._update_total).pack(anchor="w")
        tk.Label(left, text=a.detail, bg=PANEL, fg=MUTED, font=(FONT, 9), wraplength=520,
                 justify="left").pack(anchor="w", padx=(22 if a.group != "note" else 0, 0))
        right = tk.Frame(row, bg=PANEL)
        right.pack(side="right")
        if a.size:
            unit = " RAM" if a.group == "apps" else ""
            tk.Label(right, text=fmt_bytes(a.size) + unit, bg=PANEL, fg=TEXT,
                     font=(FONT, 10, "bold")).pack(anchor="e")
        self.results[a.key] = tk.Label(right, text="", bg=PANEL, fg=OK, font=(FONT, 9))
        self.results[a.key].pack(anchor="e")

    def _chosen(self):
        return [a for a in self.actions if a.key in self.vars and self.vars[a.key].get()]

    def _update_total(self):
        chosen = self._chosen()
        if not chosen:
            if self.actions:
                self.status.config(text="Nothing selected.")
            return
        disk = sum(a.size for a in chosen if a.group == "files")
        mem = sum(a.size for a in chosen if a.group in ("apps", "services"))
        parts = []
        if disk:
            parts.append(f"{fmt_bytes(disk)} of disk space")
        if mem:
            parts.append(f"{fmt_bytes(mem)} of memory")
        fixes = len(chosen)
        self.status.config(text=f"{fixes} safe fix{'es' * (fixes > 1)} found"
                                + (" · frees about " + " and ".join(parts) if parts else ""))

    # --- running ---------------------------------------------------------------

    def optimize(self):
        chosen = self._chosen()
        if not chosen:
            return
        self.go.state(["disabled"])
        self.rescan.state(["disabled"])
        for key in self.vars:
            self.results[key].config(text="")
        self.status.config(text="Optimizing…")
        self.app.run_bg(lambda: optimizer.run(chosen), lambda freed, err: self._done(chosen, freed, err))

    def _done(self, chosen, freed, err):
        if not self.winfo_exists():
            return
        self.rescan.state(["!disabled"])
        if err:
            self.status.config(text=f"Something went wrong: {err}")
            self.go.state(["!disabled"])
            return
        for a in chosen:
            ok = not a.result.startswith("failed")
            self.results[a.key].config(text=("✓ " if ok else "✗ ") + a.result, fg=OK if ok else DANGER)
        closed = sum(a.count for a in chosen if a.group == "apps")
        msg = f"Done! Freed {fmt_bytes(freed)} of disk space"
        msg += f" and closed {closed} background helper{'s' * (closed > 1)}." if closed else "."
        self.status.config(text=msg)
        self.go.config(text="Close", command=self.destroy)
        self.go.state(["!disabled"])
        self._update_undo()
        self.app.cleaner_tab.scan()
        self.app.startup_tab.load()
        self.app.services_tab.load()

    def _update_undo(self):
        if optimizer.can_undo():
            self.undo_btn.pack(side="right", padx=8)
        else:
            self.undo_btn.pack_forget()

    def undo(self):
        def done(msg, err):
            if not self.winfo_exists():
                return
            messagebox.showinfo("Smart Optimize", str(err) if err else msg, parent=self)
            self.app.startup_tab.load()
            self.app.services_tab.load()
            self.scan()
        self.app.run_bg(optimizer.undo, done)


def run_health_check(admin: bool, junk_sizes: dict | None = None) -> list[tuple[str, str]]:
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
                                 "Closing one of them in the Monitor tab will free real memory."))
    else:
        findings.append(("ok", f"Memory is fine ({stats['mem_percent']:.0f}% used). Biggest users: {top_mem}."))

    top_cpu = max(apps, key=lambda a: a.cpu, default=None)
    if top_cpu and top_cpu.cpu >= 25:
        findings.append(("warn", f"{top_cpu.name} is using {top_cpu.cpu:.0f}% of your CPU right now. "
                                 "If you're not using it, close it from the Monitor tab."))
    elif stats["cpu"] >= 70:
        findings.append(("warn", f"CPU is busy ({stats['cpu']:.0f}%) but spread across many apps. "
                                 "Sort the Monitor tab by CPU to see what's active."))
    else:
        findings.append(("ok", f"No app is hogging the CPU ({stats['cpu']:.0f}% total right now)."))

    free_pct = 100 - stats["disk_percent"]
    if free_pct < 10 or stats["disk_free"] < 15 * 1024 ** 3:
        findings.append(("warn", f"Drive {stats['disk_drive']} is almost full ({fmt_bytes(stats['disk_free'])} free). "
                                 "A nearly full system drive slows Windows down; use the Cleaner tab."))
    else:
        findings.append(("ok", f"Drive {stats['disk_drive']} has {fmt_bytes(stats['disk_free'])} free."))

    enabled = [i for i in startup.list_startup() if i.enabled]
    if len(enabled) > 8:
        findings.append(("warn", f"{len(enabled)} apps start with Windows. Disable the ones you don't need "
                                 "in the Startup apps tab for a faster boot."))
    else:
        findings.append(("ok", f"{len(enabled)} apps start with Windows. That's reasonable."))

    optional = [s for s in services.list_services() if s.level]
    if optional:
        findings.append(("tip", f"{len(optional)} background services start automatically but probably "
                                "don't need to. Review them in the Services tab."))

    junk = 0
    for t in cleaner.targets():
        if t.needs_admin and not admin:
            continue
        junk += (junk_sizes.get(t.key) if junk_sizes and t.key in junk_sizes else cleaner.scan(t))[0]
    if junk > 500 * 1024 ** 2:
        findings.append(("tip", f"About {fmt_bytes(junk)} of junk files can be cleaned in the Cleaner tab."))

    days = (time.time() - psutil.boot_time()) / 86400
    if days >= 7:
        findings.append(("tip", f"Your PC hasn't restarted in {days:.0f} days. A restart (not shut down, "
                                "which keeps Fast Startup state) clears leaks and finishes updates."))
    return findings


class MonitorTab(ttk.Frame):
    COLUMNS = (("name", "App", 220), ("count", "Processes", 80), ("cpu", "CPU", 70),
               ("mem", "Memory", 100), ("desc", "What is it?", 520))

    def __init__(self, parent, app: App):
        super().__init__(parent, padding=14)
        self.app = app
        self.sampler = ProcessSampler()
        self.rows = {}
        self.sort_col, self.sort_desc = "mem", True
        self._group = True  # plain copy of self.group for the sampling thread
        self.group = tk.BooleanVar(value=True)
        self.query = tk.StringVar()

        cards = ttk.Frame(self)
        cards.pack(fill="x", pady=(0, 12))
        self.cards = {}
        for key, title in (("cpu", "CPU"), ("mem", "Memory"), ("disk", "System drive")):
            c = ttk.Frame(cards, style="Card.TFrame", padding=14)
            c.pack(side="left", fill="x", expand=True, padx=(0 if key == "cpu" else 6, 0))
            ttk.Label(c, text=title, style="Card.TLabel").pack(anchor="w")
            val = ttk.Label(c, text="…", style="CardValue.TLabel")
            val.pack(anchor="w")
            sub = ttk.Label(c, text="", style="Card.TLabel")
            sub.pack(anchor="w")
            bar = ttk.Progressbar(c, maximum=100, style="Ok.Horizontal.TProgressbar")
            bar.pack(fill="x", pady=(8, 0))
            self.cards[key] = (val, sub, bar)

        self.chart = HistoryChart(self, [("cpu", "CPU", ACCENT), ("mem", "Memory", PURPLE)],
                                  capacity=HISTORY_SECONDS * 1000 // REFRESH_MS,
                                  span_label=f"{HISTORY_SECONDS // 60} min ago",
                                  bg=PANEL, grid=PANEL_HI, text=TEXT, muted=MUTED, height=130)
        self.chart.pack(fill="x", pady=(0, 12))

        bar = ttk.Frame(self)
        bar.pack(fill="x", pady=(0, 8))
        ttk.Label(bar, text="Search").pack(side="left")
        e = ttk.Entry(bar, textvariable=self.query, width=28)
        e.pack(side="left", padx=(6, 12))
        self.query.trace_add("write", lambda *_: self._render())
        ttk.Checkbutton(bar, text="Group by app", variable=self.group,
                        command=self._regroup).pack(side="left")
        ttk.Button(bar, text="End task", style="Danger.TButton",
                   command=self.end_task).pack(side="right")
        ttk.Label(bar, text="Orange = heavy. Select a row and End task to free its CPU and memory.  ",
                  style="Muted.TLabel").pack(side="right")

        frame = ttk.Frame(self)
        frame.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(frame, columns=[c[0] for c in self.COLUMNS], show="headings",
                                 selectmode="browse")
        for col, title, width in self.COLUMNS:
            self.tree.heading(col, text=title, command=lambda c=col: self._sort_by(c))
            anchor = "e" if col in ("count", "cpu", "mem") else "w"
            self.tree.column(col, width=width, anchor=anchor, stretch=(col == "desc"))
        self.tree.tag_configure("hot", foreground=WARN)
        sb = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.tree.bind("<Delete>", lambda _: self.end_task())

        threading.Thread(target=self._sample_loop, daemon=True).start()

    def _sample_loop(self):
        # One long-lived thread: psutil tracks CPU deltas per thread, so the
        # samples must always come from the same one.
        system_stats()
        self.sampler.sample(self._group)
        time.sleep(1)
        while True:
            try:
                result = (system_stats(), self.sampler.sample(self._group))
                self.app._queue.put((self._on_sample, result, None))
            except Exception:
                pass
            time.sleep(REFRESH_MS / 1000)

    def _on_sample(self, result, err):
        stats, apps = result
        self.chart.add(cpu=stats["cpu"], mem=stats["mem_percent"])
        self.app.on_sample(stats, apps)
        if any(a.key.isdigit() == self._group for a in apps[:1]):
            return  # sampled before the grouping changed
        self.rows = {a.key: a for a in apps}
        self._update_cards(stats)
        self._render()

    def _regroup(self):
        self._group = self.group.get()
        self.tree.delete(*self.tree.get_children())
        self.rows = {}

    def _update_cards(self, s):
        def level(p):
            return "Danger" if p >= 90 else "Warn" if p >= 75 else "Ok"
        val, sub, bar = self.cards["cpu"]
        val.config(text=f"{s['cpu']:.0f}%")
        sub.config(text=f"{psutil.cpu_count()} logical cores")
        bar.config(value=s["cpu"], style=f"{level(s['cpu'])}.Horizontal.TProgressbar")
        val, sub, bar = self.cards["mem"]
        val.config(text=f"{s['mem_percent']:.0f}%")
        sub.config(text=f"{fmt_bytes(s['mem_used'])} of {fmt_bytes(s['mem_total'])} in use")
        bar.config(value=s["mem_percent"], style=f"{level(s['mem_percent'])}.Horizontal.TProgressbar")
        val, sub, bar = self.cards["disk"]
        val.config(text=f"{fmt_bytes(s['disk_free'])} free")
        sub.config(text=f"Drive {s['disk_drive']} · {fmt_bytes(s['disk_total'])} total")
        bar.config(value=s["disk_percent"], style=f"{level(s['disk_percent'])}.Horizontal.TProgressbar")

    def _sort_by(self, col):
        if self.sort_col == col:
            self.sort_desc = not self.sort_desc
        else:
            self.sort_col, self.sort_desc = col, col in ("cpu", "mem", "count")
        self._render()

    def _render(self):
        q = self.query.get().strip().lower()
        apps = [a for a in self.rows.values()
                if not q or q in a.name.lower() or q in a.description.lower()]
        keyf = {"name": lambda a: a.name.lower(), "count": lambda a: a.count,
                "cpu": lambda a: a.cpu, "mem": lambda a: a.memory,
                "desc": lambda a: a.description}[self.sort_col]
        apps.sort(key=keyf, reverse=self.sort_desc)

        # Update rows in place so the selection and scroll position survive refreshes.
        wanted = {a.key for a in apps}
        for iid in self.tree.get_children():
            if iid not in wanted:
                self.tree.delete(iid)
        for i, a in enumerate(apps):
            name = a.name if self.group.get() else f"{a.name}  ({a.pids[0]})"
            values = (name, a.count, f"{a.cpu:.1f}%", fmt_bytes(a.memory), a.description)
            heavy = a.cpu >= 20 or a.memory >= 1024 ** 3
            tags = ("hot",) if heavy and not a.protected else ()
            if self.tree.exists(a.key):
                self.tree.item(a.key, values=values, tags=tags)
                self.tree.move(a.key, "", i)
            else:
                self.tree.insert("", i, iid=a.key, values=values, tags=tags)

    def end_task(self):
        sel = self.tree.selection()
        if not sel or sel[0] not in self.rows:
            messagebox.showinfo("End task", "Select an app in the list first.", parent=self)
            return
        a = self.rows[sel[0]]
        if a.protected:
            messagebox.showwarning("End task", f"{a.name} is a core part of Windows. "
                                   "Ending it could crash or lock your PC, so SwiftMyPC won't do it.",
                                   parent=self)
            return
        what = f"all {a.count} {a.name} processes" if a.count > 1 else a.name
        if not messagebox.askyesno("End task", f"End {what}?\n\nUnsaved work in it will be lost.",
                                   icon="warning", parent=self):
            return

        def done(res, err):
            if err:
                messagebox.showerror("End task", str(err), parent=self)
                return
            ended, denied = res
            if denied:
                messagebox.showwarning("End task", f"Ended {ended}, but Windows refused {denied}. "
                                       "Try 'Restart as admin'.", parent=self)
        self.app.run_bg(lambda: end_processes(a.pids), done)


class StartupTab(ttk.Frame):
    def __init__(self, parent, app: App):
        super().__init__(parent, padding=14)
        self.app = app
        self.items: list[startup.StartupItem] = []

        ttk.Label(self, text="Apps that launch when you sign in. Fewer startup apps = faster boot and "
                             "less background load. Disabling is safe and reversible (same as Task Manager).",
                  style="Muted.TLabel", wraplength=1000, justify="left").pack(anchor="w", pady=(0, 10))

        bar = ttk.Frame(self)
        bar.pack(fill="x", pady=(0, 8))
        ttk.Button(bar, text="Disable", style="Danger.TButton",
                   command=lambda: self.toggle(False)).pack(side="left")
        ttk.Button(bar, text="Enable", command=lambda: self.toggle(True)).pack(side="left", padx=6)
        ttk.Button(bar, text="Open file location", command=self.open_location).pack(side="left")
        ttk.Button(bar, text="Refresh", command=self.load).pack(side="right")
        self.count = ttk.Label(bar, text="", style="Muted.TLabel")
        self.count.pack(side="right", padx=12)

        frame = ttk.Frame(self)
        frame.pack(fill="both", expand=True)
        cols = (("name", "Name", 220), ("status", "Status", 90),
                ("location", "Where", 190), ("command", "Command", 520))
        self.tree = ttk.Treeview(frame, columns=[c[0] for c in cols], show="headings",
                                 selectmode="extended")
        for col, title, width in cols:
            self.tree.heading(col, text=title)
            self.tree.column(col, width=width, stretch=(col == "command"))
        self.tree.tag_configure("off", foreground=MUTED)
        sb = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.load()

    def load(self):
        def show(items, err):
            if err:
                messagebox.showerror("Startup apps", str(err), parent=self)
                return
            self.items = items
            self.tree.delete(*self.tree.get_children())
            for i, it in enumerate(items):
                self.tree.insert("", "end", iid=str(i), values=self._values(it),
                                 tags=() if it.enabled else ("off",))
            on = sum(1 for it in items if it.enabled)
            self.count.config(text=f"{on} enabled · {len(items) - on} disabled")
        self.app.run_bg(startup.list_startup, show)

    @staticmethod
    def _values(it):
        return (it.name, "Enabled" if it.enabled else "Disabled", it.location, it.command)

    def toggle(self, enabled: bool):
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo("Startup apps", "Select one or more apps first.", parent=self)
            return
        failed = []
        for iid in sel:
            it = self.items[int(iid)]
            try:
                startup.set_enabled(it, enabled)
            except OSError:
                failed.append(it.name)
                continue
            self.tree.item(iid, values=self._values(it), tags=() if enabled else ("off",))
        on = sum(1 for it in self.items if it.enabled)
        self.count.config(text=f"{on} enabled · {len(self.items) - on} disabled")
        if failed:
            messagebox.showwarning("Startup apps", "These apps are set up for all users and need "
                                   "administrator rights:\n\n" + "\n".join(failed) +
                                   "\n\nClick 'Restart as admin' at the top and try again.", parent=self)

    def open_location(self):
        sel = self.tree.selection()
        if not sel:
            return
        path = self.items[int(sel[0])].path
        if os.path.exists(path):
            subprocess.Popen(f'explorer /select,"{path}"')
        else:
            messagebox.showinfo("Startup apps", f"Couldn't find the file:\n{path}", parent=self)


class ServicesTab(ttk.Frame):
    FILTERS = ("Worth reviewing", "App services", "All services")
    LEVEL_ORDER = {"optional": 0, "review": 1, "": 2}

    def __init__(self, parent, app: App):
        super().__init__(parent, padding=14)
        self.app = app
        self.services: dict[str, services.Service] = {}
        self.filter = tk.StringVar(value=self.FILTERS[0])

        ttk.Label(self, text="Services run in the background even when no app is open. Setting the ones "
                             "you don't need to Manual means they only start when something actually "
                             "needs them. Core Windows services are locked, and every change can be restored.",
                  style="Muted.TLabel", wraplength=1000, justify="left").pack(anchor="w", pady=(0, 10))

        bar = ttk.Frame(self)
        bar.pack(fill="x", pady=(0, 8))
        box = ttk.Combobox(bar, textvariable=self.filter, values=self.FILTERS, state="readonly", width=18)
        box.pack(side="left")
        box.bind("<<ComboboxSelected>>", lambda _: self._render())
        ttk.Button(bar, text="Set to Manual", style="Accent.TButton",
                   command=lambda: self._change("demand")).pack(side="left", padx=(12, 0))
        ttk.Button(bar, text="Disable", style="Danger.TButton",
                   command=lambda: self._change("disabled")).pack(side="left", padx=6)
        ttk.Button(bar, text="Restore original", command=self._restore).pack(side="left")
        ttk.Button(bar, text="Stop now", command=self._stop).pack(side="left", padx=6)
        ttk.Button(bar, text="Refresh", command=self.load).pack(side="right")
        self.count = ttk.Label(bar, text="Loading…", style="Muted.TLabel")
        self.count.pack(side="right", padx=12)

        frame = ttk.Frame(self)
        frame.pack(fill="both", expand=True)
        cols = (("name", "Service", 270), ("status", "Status", 80), ("start", "Starts", 150),
                ("source", "From", 70), ("mem", "Memory", 80), ("advice", "Advice", 480))
        self.tree = ttk.Treeview(frame, columns=[c[0] for c in cols], show="headings",
                                 selectmode="extended")
        for col, title, width in cols:
            self.tree.heading(col, text=title)
            self.tree.column(col, width=width, anchor="e" if col == "mem" else "w",
                             stretch=(col == "advice"))
        self.tree.tag_configure("optional", foreground=WARN)
        self.tree.tag_configure("review", foreground="#8fb6ff")
        self.tree.tag_configure("locked", foreground=MUTED)
        sb = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.tree.bind("<<TreeviewSelect>>", lambda _: self._show_details())

        self.details = ttk.Label(self, text="Select a service to see what it does.", style="Muted.TLabel",
                                 wraplength=1000, justify="left")
        self.details.pack(anchor="w", pady=(8, 0))
        ttk.Label(self, text="Orange = safe to set to Manual.   Blue = app service that starts with Windows: "
                             "check whether you use that app.", style="Muted.TLabel").pack(anchor="w")
        self.load()

    def load(self):
        self.count.config(text="Loading…")

        def show(result, err):
            if err:
                self.count.config(text=f"Couldn't list services: {err}")
                return
            self.services = {s.name: s for s in result}
            self._render()
        self.app.run_bg(services.list_services, show)

    def _visible(self):
        f = self.filter.get()
        items = list(self.services.values())
        if f == self.FILTERS[0]:
            items = [s for s in items if s.level]
        elif f == self.FILTERS[1]:
            items = [s for s in items if not s.is_windows]
        return sorted(items, key=lambda s: (self.LEVEL_ORDER[s.level], s.display.lower()))

    def _row(self, s):
        status = {"running": "Running", "stopped": "Stopped"}.get(s.status, s.status.replace("_", " ").title())
        values = (s.display, status, s.start_label, s.source, fmt_bytes(s.memory) if s.memory else "",
                  s.advice)
        tag = s.level if s.level else ("locked" if not s.can_change else "")
        return values, (tag,) if tag else ()

    def _render(self):
        sel = set(self.tree.selection())
        self.tree.delete(*self.tree.get_children())
        visible = self._visible()
        for s in visible:
            values, tags = self._row(s)
            self.tree.insert("", "end", iid=s.name, values=values, tags=tags)
        keep = [iid for iid in sel if self.tree.exists(iid)]
        if keep:
            self.tree.selection_set(keep)
        worth = sum(1 for s in self.services.values() if s.level)
        self.count.config(text=f"{worth} to review · {len(self.services)} total")

    def _show_details(self):
        sel = self.tree.selection()
        if not sel:
            return
        s = self.services[sel[0]]
        lock = "" if s.can_change else "  (core Windows service: locked)"
        self.details.config(text=f"{s.display}{lock}: {s.description or 'No description.'}\n{s.binpath}")

    def _selected(self):
        sel = [self.services[iid] for iid in self.tree.selection()]
        if not sel:
            messagebox.showinfo("Services", "Select one or more services first.", parent=self)
            return []
        if not self.app.admin:
            messagebox.showinfo("Services", "Changing services needs administrator rights.\n\n"
                                "Click 'Restart as admin' at the top, then try again.", parent=self)
            return []
        locked = [s.display for s in sel if not s.can_change]
        if locked:
            messagebox.showwarning("Services", "These are core Windows services and SwiftMyPC won't change "
                                   "them:\n\n" + "\n".join(locked), parent=self)
        return [s for s in sel if s.can_change]

    def _run(self, chosen, action, verb):
        def work():
            errors = []
            for s in chosen:
                try:
                    if action(s) is False:
                        errors.append(f"{s.display}: nothing to restore (SwiftMyPC never changed it)")
                except OSError as e:
                    errors.append(f"{s.display}: {e}")
            return errors

        def done(errors, err):
            if err:
                errors = [str(err)]
            self._render()
            self._show_details()
            if errors:
                messagebox.showwarning("Services", f"Some services couldn't be {verb}:\n\n" + "\n".join(errors),
                                       parent=self)
        self.app.run_bg(work, done)

    def _change(self, start):
        chosen = self._selected()
        if not chosen:
            return
        if start == "disabled" and not messagebox.askyesno(
                "Disable services", "Disabled services can't start at all, even when an app needs them. "
                "If something stops working, select it here and click 'Restore original'.\n\nDisable "
                + ", ".join(s.display for s in chosen) + "?", icon="warning", parent=self):
            return
        self._run(chosen, lambda s: services.set_start(s, start), "changed")

    def _restore(self):
        chosen = self._selected()
        if chosen:
            self._run(chosen, services.restore, "restored")

    def _stop(self):
        chosen = self._selected()
        if chosen and messagebox.askyesno("Stop services", "Stop " + ", ".join(s.display for s in chosen)
                                          + " now? It may start again later unless you also set it to "
                                          "Manual or Disabled.", parent=self):
            self._run(chosen, services.stop, "stopped")


class CleanerTab(ttk.Frame):
    def __init__(self, parent, app: App):
        super().__init__(parent, padding=14)
        self.app = app
        self.targets = cleaner.targets()
        self.vars, self.size_labels = {}, {}
        self.sizes = {}

        ttk.Label(self, text="Free up disk space by removing files Windows and your apps don't need. "
                             "Files that are in use are skipped automatically.",
                  style="Muted.TLabel", wraplength=1000, justify="left").pack(anchor="w", pady=(0, 10))

        for t in self.targets:
            row = ttk.Frame(self, style="Card.TFrame", padding=(14, 10))
            row.pack(fill="x", pady=3)
            default = not t.recycle_bin and (not t.needs_admin or app.admin)
            var = tk.BooleanVar(value=default)
            self.vars[t.key] = var
            label = t.label + ("  (admin)" if t.needs_admin and not app.admin else "")
            ttk.Checkbutton(row, text=label, variable=var, style="Card.TCheckbutton",
                            command=self._update_total).pack(side="left")
            ttk.Label(row, text="   " + t.description, style="Card.TLabel").pack(side="left")
            size = ttk.Label(row, text="…", style="Card.TLabel", width=18, anchor="e")
            size.pack(side="right")
            self.size_labels[t.key] = size

        bar = ttk.Frame(self)
        bar.pack(fill="x", pady=(14, 0))
        self.clean_btn = ttk.Button(bar, text="Clean selected", style="Accent.TButton", command=self.clean)
        self.clean_btn.pack(side="right")
        ttk.Button(bar, text="Scan again", command=self.scan).pack(side="right", padx=8)
        self.total = ttk.Label(bar, text="Scanning…", font=(FONT, 12, "bold"))
        self.total.pack(side="left")
        self.result = ttk.Label(self, text="", style="Muted.TLabel")
        self.result.pack(anchor="w", pady=(8, 0))

        self.after(400, self.scan)

    def scan(self):
        for lbl in self.size_labels.values():
            lbl.config(text="scanning…")
        self.total.config(text="Scanning…")

        def scan_all():
            return {t.key: cleaner.scan(t) for t in self.targets}

        def show(sizes, err):
            if err:
                self.total.config(text=f"Scan failed: {err}")
                return
            self.sizes = sizes
            for key, (size, count) in sizes.items():
                self.size_labels[key].config(text=f"{fmt_bytes(size)}  ·  {count} files")
            self._update_total()
        self.app.run_bg(scan_all, show)

    def _update_total(self):
        total = sum(self.sizes.get(k, (0, 0))[0] for k, v in self.vars.items() if v.get())
        self.total.config(text=f"{fmt_bytes(total)} can be freed")

    def clean(self):
        chosen = [t for t in self.targets if self.vars[t.key].get()]
        if not chosen:
            return
        names = "\n".join("• " + t.label for t in chosen)
        if not messagebox.askyesno("Clean", f"Delete files from:\n\n{names}\n\nThis can't be undone.",
                                   icon="warning", parent=self):
            return
        self.clean_btn.state(["disabled"])
        self.result.config(text="Cleaning…")

        def clean_all():
            freed = deleted = skipped = 0
            for t in chosen:
                f, d, s = cleaner.clean(t)
                freed, deleted, skipped = freed + f, deleted + d, skipped + s
            return freed, deleted, skipped

        def done(res, err):
            self.clean_btn.state(["!disabled"])
            if err:
                self.result.config(text=f"Cleaning failed: {err}")
            else:
                freed, deleted, skipped = res
                msg = f"Freed {fmt_bytes(freed)} ({deleted} files deleted"
                msg += f", {skipped} in use or protected were skipped)." if skipped else ")."
                self.result.config(text=msg)
            self.scan()
        self.app.run_bg(clean_all, done)


def main():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
        # Own taskbar identity, so Windows shows our icon instead of Python's.
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("SwiftMyPC")
    except Exception:
        pass
    instance = SingleInstance()
    if not instance.acquire():
        if not instance.signal_existing():
            ctypes.windll.user32.MessageBoxW(None, "SwiftMyPC is already running. Look for its icon "
                                             "in the system tray.", "SwiftMyPC", 0x40)
        return
    App(start_hidden="--tray" in sys.argv, instance=instance).mainloop()
