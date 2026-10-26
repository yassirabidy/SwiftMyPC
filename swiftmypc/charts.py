"""Small canvas line chart for CPU / memory history."""

import tkinter as tk
from collections import deque


class HistoryChart(tk.Canvas):
    """Plots percentages (0-100) over a fixed window of samples, newest on the right."""

    PAD_L, PAD_R, PAD_T, PAD_B = 36, 12, 28, 20

    def __init__(self, parent, series, capacity, span_label, bg, grid, text, muted, **kw):
        super().__init__(parent, bg=bg, highlightthickness=0, **kw)
        self.series = series            # [(key, label, color)]
        self.data = {key: deque(maxlen=capacity) for key, _, _ in series}
        self.capacity = capacity
        self.span_label = span_label
        self.colors = {"grid": grid, "text": text, "muted": muted}
        self.bind("<Configure>", lambda _: self.redraw())

    def add(self, **values):
        for key, value in values.items():
            self.data[key].append(max(0.0, min(100.0, value)))
        self.redraw()

    def redraw(self):
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        if w < 50 or h < 50:
            return
        x0, x1 = self.PAD_L, w - self.PAD_R
        y0, y1 = self.PAD_T, h - self.PAD_B
        font = ("Segoe UI", 8)

        for pct in (0, 50, 100):
            y = y1 - (y1 - y0) * pct / 100
            self.create_line(x0, y, x1, y, fill=self.colors["grid"], dash=(2, 3) if pct == 50 else ())
            self.create_text(x0 - 6, y, text=f"{pct}%", anchor="e", fill=self.colors["muted"], font=font)
        self.create_text(x0, h - 4, text=self.span_label, anchor="sw", fill=self.colors["muted"], font=font)
        self.create_text(x1, h - 4, text="now", anchor="se", fill=self.colors["muted"], font=font)

        step = (x1 - x0) / max(1, self.capacity - 1)
        lx = x0
        for key, label, color in self.series:
            values = self.data[key]
            if len(values) >= 2:
                start = self.capacity - len(values)
                points = []
                for i, v in enumerate(values):
                    points += [x0 + (start + i) * step, y1 - (y1 - y0) * v / 100]
                self.create_line(*points, fill=color, width=2, smooth=False)
            current = f"{values[-1]:.0f}%" if values else "…"
            self.create_rectangle(lx, 9, lx + 10, 19, fill=color, outline="")
            item = self.create_text(lx + 16, 14, text=f"{label}  {current}", anchor="w",
                                    fill=self.colors["text"], font=("Segoe UI", 9))
            lx = self.bbox(item)[2] + 18
