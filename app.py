"""DeskSearch - local file search popup, opened by tapping Ctrl twice.

Run:  pythonw app.py      (or double-click run.bat)
"""
import os
import queue
import socket
import subprocess
import sys
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
from datetime import datetime
from tkinter import filedialog, messagebox, ttk

import config
import engine
import extractors
from hotkey import DoubleCtrlDetector, start_listener
from version import VERSION as APP_VERSION   # a launch of a different version replaces the running copy

IS_WIN = sys.platform.startswith("win")
SINGLE_INSTANCE_PORT = 47653
ROWS_VISIBLE = 7       # rows before the list scrolls

THEMES = {
    "light": dict(bg="#ffffff", panel="#f5f6f8", border="#dfe3e8", fg="#1f2328", sub="#6b7280",
                  faint="#9aa1ad", accent="#2563eb", sel="#e8f0ff", hover="#f3f5f8", chip="#eceff3",
                  chip_fg="#3b4351", ph="#9aa1ad"),
    "dark": dict(bg="#1d2027", panel="#171a1f", border="#343a45", fg="#e8eaed", sub="#a0a7b4",
                 faint="#6f7785", accent="#7ea6ff", sel="#2a3552", hover="#242933", chip="#2a2f39",
                 chip_fg="#c4cad6", ph="#6f7785"),
}
# icon tile colour per file family
BADGE = {
    "documents": "#3b82f6", "sheets": "#16a34a", "slides": "#ea580c", "pdf": "#dc2626",
    "music": "#9333ea", "pictures": "#0d9488", "videos": "#db2777", "code": "#475569",
    "archives": "#b45309", "apps": "#0284c7", "other": "#6b7280",
}
CHIPS = [("All", None), ("Documents", "documents"), ("Music", "music"),
         ("Pictures", "pictures"), ("Videos", "videos"), ("Apps", "apps"), ("Code", "code")]


# --------------------------------------------------------------------------- helpers

def human_size(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def short_path(p, limit=100):
    if len(p) <= limit:
        return p
    head = p[:limit // 3]
    return head + "…" + p[-(limit - len(head) - 1):]


def open_path(path):
    try:
        if IS_WIN:
            os.startfile(path)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
    except OSError as exc:
        messagebox.showerror("DeskSearch", f"Could not open file:\n{exc}")


def show_in_folder(path):
    try:
        if IS_WIN:
            subprocess.Popen(f'explorer /select,"{os.path.normpath(path)}"')
        else:
            open_path(os.path.dirname(path))
    except OSError:
        pass


def detect_theme(pref):
    """Light unless the user explicitly picked dark in settings (Windows dark mode is ignored)."""
    return "dark" if pref == "dark" else "light"


def _hwnd(tk_window):
    import ctypes
    return ctypes.windll.user32.GetParent(tk_window.winfo_id()) or tk_window.winfo_id()


def force_foreground(tk_window):
    """Windows blocks background apps from stealing focus; attach input queues to get around it."""
    if not IS_WIN:
        return
    try:
        import ctypes
        user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
        hwnd = _hwnd(tk_window)
        fg = user32.GetForegroundWindow()
        fg_thread = user32.GetWindowThreadProcessId(fg, None)
        cur_thread = kernel32.GetCurrentThreadId()
        if fg_thread and fg_thread != cur_thread:
            user32.AttachThreadInput(fg_thread, cur_thread, True)
            user32.SetForegroundWindow(hwnd)
            user32.BringWindowToTop(hwnd)
            user32.AttachThreadInput(fg_thread, cur_thread, False)
        else:
            user32.SetForegroundWindow(hwnd)
    except Exception:
        pass


def style_window_windows(tk_window, border_hex):
    """Windows 11: rounded corners + themed border; drop shadow on older builds. Best effort."""
    if not IS_WIN:
        return
    try:
        import ctypes
        from ctypes import byref, c_int
        hwnd = _hwnd(tk_window)
        dwm = ctypes.windll.dwmapi
        dwm.DwmSetWindowAttribute(hwnd, 33, byref(c_int(2)), 4)          # DWMWA_WINDOW_CORNER_PREFERENCE = round
        r, g, b = (int(border_hex[i:i + 2], 16) for i in (1, 3, 5))
        dwm.DwmSetWindowAttribute(hwnd, 34, byref(c_int(b << 16 | g << 8 | r)), 4)   # DWMWA_BORDER_COLOR
        user32 = ctypes.windll.user32
        style = user32.GetClassLongW(hwnd, -26)
        user32.SetClassLongW(hwnd, -26, style | 0x00020000)              # CS_DROPSHADOW
    except Exception:
        pass


def make_tray_image():
    from PIL import Image, ImageDraw
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((6, 6, 40, 40), outline=(37, 99, 235, 255), width=7)
    d.line((36, 36, 57, 57), fill=(37, 99, 235, 255), width=9)
    return img


def segments_for_words(text, words):
    """[(text, highlighted)] with case-insensitive occurrences of any word marked."""
    low = text.lower()
    spans = []
    for w in words:
        start = 0
        while w and (i := low.find(w, start)) != -1:
            spans.append((i, i + len(w)))
            start = i + len(w)
    spans.sort()
    out, pos = [], 0
    for a, b in spans:
        if a < pos:
            continue
        out.append((text[pos:a], False))
        out.append((text[a:b], True))
        pos = b
    out.append((text[pos:], False))
    return [s for s in out if s[0]]


def segments_for_snippet(snip):
    out = []
    for i, chunk in enumerate(snip.replace("\n", " ").split(engine.HL_START)):
        if i == 0:
            out.append((chunk, False))
            continue
        hit, _, rest = chunk.partition(engine.HL_END)
        out += [(hit, True), (rest, False)]
    return [s for s in out if s[0]]


# --------------------------------------------------------------------------- app

class App:
    def __init__(self):
        if IS_WIN:
            try:
                import ctypes
                ctypes.windll.shcore.SetProcessDpiAwareness(1)
            except Exception:
                pass
        self.cfg = config.load()
        self.q = queue.Queue()
        self.root = tk.Tk()
        self.root.withdraw()
        self.root.title("DeskSearch")
        self.s = max(1.0, self.root.winfo_fpixels("1i") / 96.0)
        self.family = "Segoe UI" if IS_WIN else tkfont.nametofont("TkDefaultFont").actual("family")

        self.indexer = engine.Indexer(config.db_path(), lambda: self.cfg)
        self.searcher = engine.Searcher(config.db_path())
        self.results, self.words = [], []
        self.rows = []
        self.shown = 0                  # how many results are drawn; the rest wait behind "Show more"
        self.query, self.ms = "", 0.0
        self.more_pending = False
        self.heading = ""
        self.sel, self.hover = 0, None
        self.seq = 0
        self._req = None                # latest search request; older ones are dropped unrun
        self._req_ev = threading.Event()
        self._counts, self._counts_at, self._counts_busy = None, 0.0, False
        threading.Thread(target=self._search_worker, daemon=True).start()
        self.category = None            # None = All
        self.shown_at = 0.0
        self.settings_win = None
        self.menu_open = False
        self._stats_at, self._info_txt = 0.0, ""
        self.tray = None
        self.theme_name = None
        self.win = None

        self._build_popup()
        self.detector = DoubleCtrlDetector(lambda: self.q.put("toggle"), gap_ms=self.cfg["double_tap_ms"])
        self.listeners = None
        try:
            self.listeners = start_listener(self.detector)
        except Exception as exc:
            messagebox.showerror("DeskSearch", f"Could not start the Ctrl-Ctrl hotkey:\n{exc}")

        self._start_tray()
        self._start_instance_server()
        self.indexer.start()
        self.root.after(30, self._poll)
        if not self.cfg["folders"]:
            self.q.put("settings")

    # ================================================================ popup construction
    def _px(self, v):
        return int(v * self.s)

    def _build_popup(self):
        if self.win is not None:
            self.win.destroy()
        self.theme_name = detect_theme(self.cfg.get("theme", "light"))
        T = self.T = THEMES[self.theme_name]
        fam = self.family
        self.f_entry = tkfont.Font(family=fam, size=18)
        self.f_title = tkfont.Font(family=fam, size=11, weight="bold")
        self.f_sub = tkfont.Font(family=fam, size=9)
        self.f_sub_b = tkfont.Font(family=fam, size=9, weight="bold")
        self.f_small = tkfont.Font(family=fam, size=8)
        self.f_badge = tkfont.Font(family=fam, size=8, weight="bold")
        self.f_head = tkfont.Font(family=fam, size=8, weight="bold")
        self.f_chip = tkfont.Font(family=fam, size=9)
        self.W = self._px(760)
        self.ROW_H = self._px(68)
        self.MORE_H = self._px(40)
        self.HEAD_H = self._px(30)

        self.win = tk.Toplevel(self.root)
        self.win.withdraw()
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        self.win.configure(bg=T["border"])
        body = tk.Frame(self.win, bg=T["bg"])
        body.pack(padx=1, pady=1, fill="both", expand=True)
        tk.Frame(body, width=self.W, height=1, bg=T["bg"]).pack()

        # --- search row
        top = tk.Frame(body, bg=T["bg"])
        top.pack(fill="x", padx=self._px(20), pady=(self._px(16), self._px(8)))
        ico = tk.Canvas(top, width=self._px(26), height=self._px(26), bg=T["bg"], highlightthickness=0)
        ico.pack(side="left", padx=(0, self._px(12)))
        p = self._px
        ico.create_oval(p(3), p(3), p(17), p(17), outline=T["accent"], width=max(2, p(2)))
        ico.create_line(p(15), p(15), p(23), p(23), fill=T["accent"], width=max(2, p(2)), capstyle="round")
        self.var = tk.StringVar()
        self.entry = tk.Entry(top, textvariable=self.var, font=self.f_entry, relief="flat", bg=T["bg"],
                              fg=T["fg"], insertbackground=T["accent"], insertwidth=2,
                              highlightthickness=0, selectbackground=T["sel"], selectforeground=T["fg"])
        self.entry.pack(side="left", fill="x", expand=True, ipady=self._px(5))
        self.ph = tk.Label(top, text="Search files and contents…   try  *.pdf  or  report*.docx", font=self.f_entry,
                           bg=T["bg"], fg=T["ph"])
        self.ph.place(in_=self.entry, x=2, rely=0.5, anchor="w")
        self.ph.bind("<Button-1>", lambda e: self.entry.focus_set())
        gear = tk.Label(top, text="⚙", font=(fam, 15), bg=T["bg"], fg=T["faint"], cursor="hand2")
        gear.pack(side="right", padx=(self._px(8), 0))
        gear.bind("<Button-1>", lambda e: self.q.put("settings"))
        self.clear = tk.Label(top, text="✕", font=(fam, 11), bg=T["bg"], fg=T["faint"], cursor="hand2")
        self.clear.bind("<Button-1>", lambda e: (self.var.set(""), self.entry.focus_set()))

        # --- category chips
        chips = tk.Frame(body, bg=T["bg"])
        chips.pack(fill="x", padx=self._px(20), pady=(0, self._px(10)))
        self.chip_labels = []
        for i, (label, _cat) in enumerate(CHIPS):
            lb = tk.Label(chips, text=label, font=self.f_chip, cursor="hand2",
                          padx=self._px(12), pady=self._px(3))
            lb.pack(side="left", padx=(0, self._px(6)))
            lb.bind("<Button-1>", lambda e, i=i: self._set_category(i))
            self.chip_labels.append(lb)
        self._paint_chips()

        # --- results canvas
        self.sep = tk.Frame(body, bg=T["border"], height=1)
        self.cv_frame = tk.Frame(body, bg=T["bg"])
        self.cv = tk.Canvas(self.cv_frame, width=self.W, height=self.ROW_H, bg=T["bg"],
                            highlightthickness=0, yscrollincrement=self._px(22))
        self.cv.pack(fill="both", expand=True)
        self.cv.bind("<Motion>", self._on_motion)
        self.cv.bind("<Leave>", lambda e: self._set_hover(None))
        self.cv.bind("<Button-1>", self._on_click)          # one click opens the file
        self.cv.bind("<Button-3>", self._on_right_click)
        self.cv.bind("<MouseWheel>", lambda e: self.cv.yview_scroll(-1 if e.delta > 0 else 1, "units") or "break")
        self.cv.bind("<Button-4>", lambda e: self.cv.yview_scroll(-1, "units"))
        self.cv.bind("<Button-5>", lambda e: self.cv.yview_scroll(1, "units"))

        # --- footer
        self.footer = tk.Frame(body, bg=T["panel"])
        self.footer.pack(fill="x", side="bottom")
        self.status = tk.Label(self.footer, text="", font=self.f_small, bg=T["panel"], fg=T["sub"], anchor="w")
        self.status.pack(side="left", padx=self._px(20), pady=self._px(6))
        self.HINTS = "↑↓ select    ↵ open    Ctrl+↵ folder    Tab filter    Esc close"
        self.intent = tk.Label(self.footer, text=self.HINTS, font=self.f_small, bg=T["panel"], fg=T["faint"])
        self.intent.pack(side="right", padx=self._px(20))

        # --- context menu
        self.menu = tk.Menu(self.win, tearoff=0, bg=T["bg"], fg=T["fg"], activebackground=T["sel"],
                            activeforeground=T["fg"], bd=0, relief="flat")
        self.menu.add_command(label="Open", command=lambda: self._open_selected())
        self.menu.add_command(label="Show in folder", command=lambda: self._open_selected(folder=True))
        self.menu.add_command(label="Copy full path", command=self._copy_path)

        # --- key bindings
        e = self.entry
        e.bind("<Down>", lambda ev: self._move(1))
        e.bind("<Up>", lambda ev: self._move(-1))
        e.bind("<Next>", lambda ev: self._move(ROWS_VISIBLE))
        e.bind("<Prior>", lambda ev: self._move(-ROWS_VISIBLE))
        e.bind("<Return>", lambda ev: self._open_selected())
        e.bind("<Control-Return>", lambda ev: self._open_selected(folder=True))
        e.bind("<Control-Shift-C>", lambda ev: self._copy_path())
        e.bind("<Control-a>", lambda ev: (e.select_range(0, "end"), "break")[1])
        e.bind("<Tab>", lambda ev: (self._set_category((self._chip_index() + 1) % len(CHIPS)), "break")[1])
        e.bind("<Shift-Tab>", lambda ev: (self._set_category((self._chip_index() - 1) % len(CHIPS)), "break")[1])
        e.bind("<ISO_Left_Tab>", lambda ev: (self._set_category((self._chip_index() - 1) % len(CHIPS)), "break")[1])
        for n in range(len(CHIPS)):
            e.bind(f"<Control-Key-{n + 1}>", lambda ev, n=n: (self._set_category(n), "break")[1])
        e.bind("<Escape>", lambda ev: self.hide())
        self.win.bind("<Escape>", lambda ev: self.hide())
        self.win.bind("<FocusOut>", self._on_focus_out)
        self._debounce = None
        self.var.trace_add("write", self._on_text_change)
        self._layout_for(None)

    # ================================================================ show / hide
    def show(self):
        if not self.cfg["folders"]:
            self.open_settings()
            return
        if detect_theme(self.cfg.get("theme", "light")) != self.theme_name:
            self._build_popup()
        self.win.update_idletasks()
        w = self.win.winfo_reqwidth()
        x = (self.win.winfo_screenwidth() - w) // 2
        y = int(self.win.winfo_screenheight() * 0.15)
        self.win.geometry(f"+{x}+{y}")
        try:
            self.win.attributes("-alpha", 0.0)
        except tk.TclError:
            pass
        self.win.deiconify()
        style_window_windows(self.win, self.T["border"])
        self.win.attributes("-topmost", True)
        self.win.lift()
        force_foreground(self.win)
        self.win.focus_force()
        self.entry.focus_force()
        self.entry.select_range(0, "end")
        self.entry.icursor("end")
        self.shown_at = time.time()
        self._fade(0)
        self._update_status(force=True)
        self._run_search()

    def _fade(self, step):
        try:
            self.win.attributes("-alpha", min(1.0, 0.25 + step * 0.25))
        except tk.TclError:
            return
        if step < 3:
            self.win.after(18, lambda: self._fade(step + 1))

    def hide(self):
        self.win.withdraw()

    def visible(self):
        return self.win.state() != "withdrawn"

    def toggle(self):
        self.hide() if self.visible() else self.show()

    def _on_focus_out(self, _event):
        if time.time() - self.shown_at < 0.5 or self.menu_open:
            return
        self.win.after(150, self._check_focus_lost)

    def _check_focus_lost(self):
        if not self.visible() or self.menu_open:
            return
        try:
            f = self.win.focus_displayof()
        except tk.TclError:
            f = None
        if f is None or f.winfo_toplevel() is not self.win:
            self.hide()

    # ================================================================ chips
    def _chip_index(self):
        return next(i for i, (_, c) in enumerate(CHIPS) if c == self.category)

    def _paint_chips(self):
        T = self.T
        for i, lb in enumerate(self.chip_labels):
            active = CHIPS[i][1] == self.category
            lb.configure(bg=T["accent"] if active else T["chip"],
                         fg="#ffffff" if active else T["chip_fg"])

    def _set_category(self, i):
        self.category = CHIPS[i][1]
        self._paint_chips()
        self._run_search()
        self.entry.focus_set()

    # ================================================================ searching
    def _on_text_change(self, *_):
        has = bool(self.var.get())
        if has:
            self.ph.place_forget()
            if not self.clear.winfo_ismapped():
                self.clear.pack(side="right", padx=(self._px(8), 0))
        else:
            self.ph.place(in_=self.entry, x=2, rely=0.5, anchor="w")
            self.clear.pack_forget()
        if self._debounce:
            self.win.after_cancel(self._debounce)
        self._debounce = self.win.after(60, self._run_search)

    def _page(self):
        try:
            return max(1, int(self.cfg.get("page_size", 5)))
        except (TypeError, ValueError):
            return 5

    def _run_search(self):
        self._debounce = None
        self.seq += 1
        cat_exts = extractors.CATEGORIES.get(self.category) if self.category else None
        self._req = (self.seq, self.var.get().strip(), cat_exts, self.cfg["max_results"], self._page())
        self._req_ev.set()

    def _search_worker(self):
        """One background thread. While you type, only the newest query is run; older ones are skipped."""
        while True:
            self._req_ev.wait()
            self._req_ev.clear()
            req, self._req = self._req, None
            if req is None:
                continue
            seq, query, cat_exts, limit, page = req
            t0 = time.perf_counter()
            try:
                if query:
                    res = self.searcher.search(query, limit, cat_exts, detail=0,
                                               is_stale=lambda: self._req is not None)
                else:
                    res = self.searcher.recent(max(8, page * 2), cat_exts)
            except Exception as exc:
                if self._req is None:
                    print("search error:", exc)
                res = []
            self.q.put(("results", seq, query, res, (time.perf_counter() - t0) * 1000))
            # results go on screen first; the content snippets (slower) follow a moment later
            if query and res and self._req is None:
                try:
                    self.searcher.fill_details(query, res[:page])
                except Exception as exc:
                    print("details error:", exc)
                self.q.put(("details", seq))

    def _show_more(self):
        """Reveal the next page of results (their snippets are built in the background first)."""
        if self.more_pending or self.shown >= len(self.results):
            return
        self.more_pending = True
        seq, query, start = self.seq, self.query, self.shown
        batch = self.results[start:start + self._page()]

        def work():
            try:
                self.searcher.fill_details(query, batch)
            except Exception as exc:
                print("details error:", exc)
            self.q.put(("more", seq, start + len(batch)))
        threading.Thread(target=work, daemon=True).start()

    def _reveal(self, n):
        old = self.shown
        self.shown = min(n, len(self.results))
        self._redraw(sel=old)                           # select the first newly shown result
        self._set_status(self._result_status())

    def _redraw(self, sel):
        """Redraw the list in place, keeping the scroll position."""
        if not self.results:
            return
        top_px = self.cv.canvasy(0)
        self.hover = None
        total = self._draw_list()
        self.cv.yview_moveto(top_px / total if total else 0)
        self.sel = max(0, min(sel, len(self.rows) - 1))
        self._paint_row(self.sel)
        self._ensure_visible(self.sel)

    # ================================================================ rendering
    def _layout_for(self, height):
        """Show the results area at `height` px, or hide it when height is None."""
        if height is None:
            self.sep.pack_forget()
            self.cv_frame.pack_forget()
            return
        self.sep.pack(fill="x", before=self.footer)
        self.cv_frame.pack(fill="both", expand=True, before=self.footer)
        self.cv.configure(height=height)

    def _render(self, results, query, ms):
        self.results, self.query, self.ms = results, query, ms
        self.shown = min(self._page(), len(results))
        self.more_pending = False
        pq = engine.parse_query(query) if query else None
        self.words = pq.words if pq else []
        if pq:
            self.intent.configure(text=pq.describe(), fg=self.T["accent"])
        else:
            self.intent.configure(text=self.HINTS, fg=self.T["faint"])
        self.heading = "" if query else "RECENT"
        self.sel, self.hover = 0, None
        cv, T, W = self.cv, self.T, self.W
        cv.delete("all")
        self.rows = []
        if not results:
            if not query:
                self._layout_for(None)
                self._set_status("")
                return
            self._layout_for(self._px(110))
            cv.configure(scrollregion=(0, 0, W, self._px(110)))
            indexing = self.indexer.state == "scanning" and not self.indexer.index_ready
            main = (f"Still indexing… {self.indexer.seen:,} files checked so far" if indexing
                    else f"No results for “{query}”")
            cv.create_text(W // 2, self._px(40), text=main, font=self.f_title, fill=T["fg"])
            cv.create_text(W // 2, self._px(68), font=self.f_sub, fill=T["sub"],
                           text="Patterns: *.pdf   report*.docx   IMG_????.jpg      Filters: type:music   size:>5mb   modified:week   name:   content:")
            self._set_status(f"0 results · {ms:.0f} ms")
            return
        self._draw_list()
        cv.yview_moveto(0)
        self._paint_row(0)
        self._set_status(self._result_status())

    def _result_status(self):
        if not self.query:
            return ""
        pq = engine.parse_query(self.query)
        cap = engine.LIST_LIMIT if pq.is_list else self.cfg["max_results"]
        n = len(self.results)
        plus = "+" if n >= cap else ""
        noun = "result" if n == 1 and not plus else "results"
        if self.shown < n:
            return f"{self.shown} of {n}{plus} {noun} · {self.ms:.0f} ms"
        return f"{n}{plus} {noun} · {self.ms:.0f} ms"

    def _draw_list(self):
        """Draw the shown results (plus the 'Show more' row) and size the popup. Returns content height."""
        cv, T, W = self.cv, self.T, self.W
        cv.delete("all")
        self.rows = []
        y0 = self.HEAD_H if self.heading else 0
        if self.heading:
            cv.create_text(self._px(22), self._px(14), text=self.heading, anchor="w",
                           font=self.f_head, fill=T["faint"])
        for i, r in enumerate(self.results[:self.shown]):
            self.rows.append(self._draw_row(i, r, y0 + i * self.ROW_H))
        y = y0 + self.shown * self.ROW_H
        rest = len(self.results) - self.shown
        if rest > 0:
            self.rows.append(self._draw_more(y, rest))
            y += self.MORE_H
        total = y + self._px(6)
        cv.configure(scrollregion=(0, 0, W, total))
        self._layout_for(min(total, y0 + ROWS_VISIBLE * self.ROW_H + self._px(6)))
        return total

    def _draw_more(self, top, rest):
        cv, T, W, p = self.cv, self.T, self.W, self._px
        h = self.MORE_H
        bg = self._rr(p(10), top + p(2), W - p(10), top + h - p(2), p(8), fill=T["bg"], outline="")
        bar = cv.create_rectangle(p(10), top + p(10), p(13), top + h - p(10),
                                  fill=T["accent"], outline="", state="hidden")
        n = min(self._page(), rest)
        cv.create_text(W // 2, top + h // 2, text=f"Show {n} more   ▾", font=self.f_sub_b, fill=T["accent"])
        cv.create_text(W - p(26), top + h // 2, text=f"{rest} more found", anchor="e",
                       font=self.f_small, fill=T["faint"])
        return dict(bg=bg, bar=bar, top=top, h=h, more=True)

    def _rr(self, x1, y1, x2, y2, r, **kw):
        pts = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2, x2 - r, y2,
               x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
        return self.cv.create_polygon(pts, smooth=True, **kw)

    def _rich(self, x, y, segs, font, hfont, color, hcolor, max_w):
        used = 0
        for text, hl in segs:
            f = hfont if hl else font
            col = hcolor if hl else color
            w = f.measure(text)
            if used + w > max_w:
                lo, hi = 0, len(text)
                while lo < hi:
                    mid = (lo + hi + 1) // 2
                    if used + f.measure(text[:mid] + "…") <= max_w:
                        lo = mid
                    else:
                        hi = mid - 1
                self.cv.create_text(x + used, y, text=text[:lo] + "…", anchor="nw", font=f, fill=col)
                return
            self.cv.create_text(x + used, y, text=text, anchor="nw", font=f, fill=col)
            used += w

    def _draw_row(self, i, r, top):
        cv, T, W, p = self.cv, self.T, self.W, self._px
        bg = self._rr(p(10), top + p(2), W - p(10), top + self.ROW_H - p(2), p(8),
                      fill=T["bg"], outline="")
        bar = cv.create_rectangle(p(10), top + p(14), p(13), top + self.ROW_H - p(14),
                                  fill=T["accent"], outline="", state="hidden")
        # icon tile
        cat = extractors.category_of(r.ext)
        tx, ty, ts = p(26), top + (self.ROW_H - p(40)) // 2, p(40)
        self._rr(tx, ty, tx + ts, ty + ts, p(9), fill=BADGE[cat], outline="")
        label = (r.ext[1:].upper() or "FILE")[:4]
        cv.create_text(tx + ts // 2, ty + ts // 2, text=label, font=self.f_badge, fill="#ffffff")

        x = p(80)
        right = W - p(26)
        when = datetime.fromtimestamp(r.mtime).strftime("%d %b %Y") if r.mtime else ""
        meta = f"{human_size(r.size or 0)}   {when}"
        mw = self.f_small.measure(meta)
        cv.create_text(right, top + p(13), text=meta, anchor="ne", font=self.f_small, fill=T["faint"])
        avail = right - x
        title_w = avail - mw - p(14)

        self._rich(x, top + p(9), segments_for_words(r.name, self.words), self.f_title, self.f_title,
                   T["fg"], T["accent"], title_w)
        line2 = r.info if r.info else short_path(os.path.dirname(r.path))
        l2_w = avail
        if r.reason:                                   # small tag on the right: why this file matched
            cv.create_text(right, top + p(31), text=r.reason, anchor="ne", font=self.f_small, fill=T["accent"])
            l2_w = avail - self.f_small.measure(r.reason) - p(14)
        self._rich(x, top + p(29), segments_for_words(line2, self.words), self.f_sub, self.f_sub_b,
                   T["sub"], T["fg"], l2_w)
        if r.snippet:
            segs = segments_for_snippet(r.snippet)
        elif r.info:
            segs = [(short_path(os.path.dirname(r.path)), False)]
        else:
            segs = []
        if segs:
            self._rich(x, top + p(47), segs, self.f_sub, self.f_sub_b,
                       T["faint"] if r.info and not r.snippet else T["sub"], T["fg"], avail)
        return dict(bg=bg, bar=bar, top=top, h=self.ROW_H, more=False)

    def _is_more(self, i):
        return 0 <= i < len(self.rows) and self.rows[i]["more"]

    def _paint_row(self, i):
        if not (0 <= i < len(self.rows)):
            return
        T = self.T
        row = self.rows[i]
        if i == self.sel:
            self.cv.itemconfigure(row["bg"], fill=T["sel"])
            self.cv.itemconfigure(row["bar"], state="normal")
        else:
            self.cv.itemconfigure(row["bg"], fill=T["hover"] if i == self.hover else T["bg"])
            self.cv.itemconfigure(row["bar"], state="hidden")

    def _set_sel(self, i):
        old, self.sel = self.sel, i
        self._paint_row(old)
        self._paint_row(i)
        self._ensure_visible(i)

    def _set_hover(self, i):
        old, self.hover = self.hover, i
        if old != i:
            self._paint_row(old) if old is not None else None
            self._paint_row(i) if i is not None else None

    def _ensure_visible(self, i):
        row = self.rows[i]
        total = int(float(self.cv.cget("scrollregion").split()[3])) if self.rows else 0
        view_h = int(self.cv.cget("height"))
        top_px = int(self.cv.canvasy(0))
        if row["top"] < top_px:
            self.cv.yview_moveto(max(0, (row["top"] - self._px(2)) / total))
        elif row["top"] + row["h"] > top_px + view_h:
            self.cv.yview_moveto((row["top"] + row["h"] - view_h + self._px(4)) / total)

    def _index_at(self, y):
        cy = self.cv.canvasy(y)
        for i, row in enumerate(self.rows):
            if row["top"] <= cy < row["top"] + row["h"]:
                return i
        return None

    def _on_motion(self, e):
        i = self._index_at(e.y)
        self.cv.configure(cursor="hand2" if i is not None else "")
        self._set_hover(i)

    def _on_click(self, e):
        i = self._index_at(e.y)
        if i is None:
            self.entry.focus_set()
            return "break"
        self._set_sel(i)
        self._open_selected()
        return "break"

    def _on_right_click(self, e):
        i = self._index_at(e.y)
        if i is None or self._is_more(i):
            return
        self._set_sel(i)
        self.menu_open = True
        try:
            self.menu.tk_popup(e.x_root, e.y_root)
        finally:
            self.menu.grab_release()
            self.menu_open = False
            self.entry.focus_force()

    def _move(self, delta):
        if self.rows:
            self._set_sel(max(0, min(len(self.rows) - 1, self.sel + delta)))
        return "break"

    def _current_path(self):
        if not self.rows or self._is_more(self.sel) or self.sel >= len(self.results):
            return None
        return self.results[self.sel].path

    def _open_selected(self, folder=False):
        if self._is_more(self.sel):          # Enter / click on "Show more"
            self._show_more()
            return "break"
        path = self._current_path()
        if not path:
            return "break"
        self.hide()
        try:
            self.searcher.record_open(path)
        except Exception:
            pass
        show_in_folder(path) if folder else open_path(path)
        return "break"

    def _copy_path(self):
        path = self._current_path()
        if path:
            self.root.clipboard_clear()
            self.root.clipboard_append(os.path.normpath(path))
            self._set_status("Path copied to clipboard")
        return "break"

    # ================================================================ status
    def _set_status(self, text):
        self._info_txt = text
        self.status.configure(text=text or self._index_status)

    _index_status = ""

    def _update_status(self, force=False):
        now = time.time()
        if not force and now - self._stats_at < 2.0:
            return
        self._stats_at = now
        # counting files takes ~0.2 s on a big index: do it in the background, at most every 15 s
        if (force or now - self._counts_at > 15) and not self._counts_busy:
            self._counts_busy = True

            def work():
                try:
                    counts = self.searcher.stats()
                except Exception:
                    counts = None
                self.q.put(("counts", counts))
            threading.Thread(target=work, daemon=True).start()
        self._compose_index_status()

    def _compose_index_status(self):
        if self._counts:
            n, c = self._counts
            self._index_status = f"{n:,} files indexed · {c:,} with contents · {self.indexer.status_text()}"
        else:
            self._index_status = self.indexer.status_text()
        if not self.var.get().strip():
            self.status.configure(text=self._index_status)

    # ================================================================ tray / single instance
    def _start_tray(self):
        try:
            import pystray
            menu = pystray.Menu(
                pystray.MenuItem("Search  (tap Ctrl twice)", lambda: self.q.put("show"), default=True),
                pystray.MenuItem("Settings...", lambda: self.q.put("settings")),
                pystray.MenuItem("Re-index now", lambda: self.q.put("reindex")),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem(f"DeskSearch v{APP_VERSION}", None, enabled=False),
                pystray.MenuItem("Quit", lambda: self.q.put("quit")),
            )
            self.tray = pystray.Icon("DeskSearch", make_tray_image(), f"DeskSearch v{APP_VERSION} - tap Ctrl twice", menu)
            self.tray.run_detached()
        except Exception as exc:
            print("tray unavailable:", exc)
            self.tray = None

    def _start_instance_server(self):
        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            self._srv.bind(("127.0.0.1", SINGLE_INSTANCE_PORT))
        except OSError:
            self._srv = None
            return
        self._srv.listen(2)

        def serve():
            while True:
                try:
                    conn, _ = self._srv.accept()
                except OSError:
                    return
                try:
                    conn.settimeout(0.4)
                    try:
                        data = conn.recv(64)
                    except OSError:
                        data = b""
                    parts = data.split()
                    if len(parts) >= 2 and parts[0] == b"hello" and parts[1].decode(errors="ignore") != APP_VERSION:
                        conn.sendall(b"quit")           # a newer copy is starting: step aside
                        self.q.put("quit")
                    else:
                        conn.sendall(b"ok")
                        self.q.put("show")
                except OSError:
                    self.q.put("show")
                finally:
                    conn.close()
        threading.Thread(target=serve, daemon=True).start()

    # ================================================================ settings window
    def open_settings(self):
        self.hide()
        if self.settings_win and self.settings_win.winfo_exists():
            self.settings_win.lift()
            self.settings_win.focus_force()
            return
        f = self.family
        w = tk.Toplevel(self.root)
        self.settings_win = w
        w.title(f"DeskSearch v{APP_VERSION} settings")
        w.geometry(f"{self._px(580)}x{self._px(680)}")
        pad = {"padx": 14, "pady": 4}

        tk.Label(w, text="Folders to index", font=(f, 10, "bold")).pack(anchor="w", **pad)
        row = tk.Frame(w)
        row.pack(fill="x", **pad)
        lb = tk.Listbox(row, height=7, font=(f, 9), activestyle="none")
        lb.pack(side="left", fill="both", expand=True)
        for p in self.cfg["folders"]:
            lb.insert("end", p)
        btns = tk.Frame(row)
        btns.pack(side="left", padx=(8, 0), anchor="n")

        def add_folder():
            d = filedialog.askdirectory(parent=w, title="Choose a folder to index")
            if d:
                d = os.path.normpath(d)
                if d not in lb.get(0, "end"):
                    lb.insert("end", d)

        def remove_folder():
            for i in reversed(lb.curselection()):
                lb.delete(i)
        ttk.Button(btns, text="Add folder...", command=add_folder).pack(fill="x", pady=2)
        ttk.Button(btns, text="Remove", command=remove_folder).pack(fill="x", pady=2)

        v_contents = tk.BooleanVar(value=self.cfg["index_contents"])
        v_auto = tk.BooleanVar(value=config.get_autostart())
        tk.Checkbutton(w, text="Index text and tags inside files (documents, PDF, Office, MP3/FLAC tags, code...)",
                       variable=v_contents).pack(anchor="w", **pad)
        v_dot = tk.BooleanVar(value=self.cfg.get("skip_hidden_folders", True))
        tk.Checkbutton(w, text="Skip folders that start with a dot (.git, .vscode, .kiro, ...)",
                       variable=v_dot).pack(anchor="w", **pad)
        tk.Checkbutton(w, text="Start DeskSearch when Windows starts", variable=v_auto,
                       state="normal" if IS_WIN else "disabled").pack(anchor="w", **pad)

        grid = tk.Frame(w)
        grid.pack(fill="x", **pad)
        tk.Label(grid, text="Skip content of files larger than (MB):").grid(row=0, column=0, sticky="w")
        v_mb = tk.IntVar(value=self.cfg["max_content_file_mb"])
        tk.Spinbox(grid, from_=1, to=500, textvariable=v_mb, width=6).grid(row=0, column=1, padx=8)
        tk.Label(grid, text="Max gap between the two Ctrl taps (ms):").grid(row=1, column=0, sticky="w", pady=4)
        v_gap = tk.IntVar(value=self.cfg["double_tap_ms"])
        tk.Spinbox(grid, from_=200, to=1000, increment=50, textvariable=v_gap, width=6).grid(row=1, column=1, padx=8)
        tk.Label(grid, text="Safety-net full scan every (minutes):").grid(row=2, column=0, sticky="w")
        v_scan = tk.IntVar(value=self.cfg["rescan_minutes"])
        tk.Spinbox(grid, from_=5, to=1440, textvariable=v_scan, width=6).grid(row=2, column=1, padx=8)
        tk.Label(grid, text="Appearance:").grid(row=3, column=0, sticky="w", pady=4)
        v_theme = tk.StringVar(value="dark" if self.cfg.get("theme") == "dark" else "light")
        ttk.Combobox(grid, textvariable=v_theme, values=("light", "dark"), width=8,
                     state="readonly").grid(row=3, column=1, padx=8)
        tk.Label(grid, text="Results shown at first (Show more adds the same):").grid(row=4, column=0, sticky="w")
        v_page = tk.IntVar(value=self._page())
        tk.Spinbox(grid, from_=1, to=50, textvariable=v_page, width=6).grid(row=4, column=1, padx=8)

        try:
            import watchdog  # noqa: F401
            live = "Live updates: ON - new and changed files are indexed within seconds."
        except ImportError:
            live = "Live updates: OFF - install 'watchdog' (pip install watchdog) for instant indexing."
        tk.Label(w, text=live, font=(f, 9), fg="#6b7280").pack(anchor="w", **pad)
        miss = extractors.missing_libs()
        if miss:
            tk.Label(w, fg="#b91c1c", font=(f, 9), justify="left", wraplength=self._px(540),
                     text="Not installed (those files are found by name only): "
                          + ", ".join(f"{m} ({p})" for m, p in miss)
                          + ".  Run  pip install -r requirements.txt  then re-index.").pack(anchor="w", **pad)

        tk.Label(w, text="Folder names to skip (comma separated)", font=(f, 10, "bold")).pack(anchor="w", **pad)
        v_ex = tk.StringVar(value=", ".join(self.cfg["excluded_dirs"]))
        tk.Entry(w, textvariable=v_ex).pack(fill="x", **pad)

        def read_int(var, default, lo, hi):
            try:
                return max(lo, min(hi, int(var.get())))
            except (tk.TclError, ValueError):
                return default

        def save(rebuild=False):
            old_theme = self.cfg.get("theme", "auto")
            self.cfg["folders"] = list(lb.get(0, "end"))
            self.cfg["index_contents"] = bool(v_contents.get())
            self.cfg["skip_hidden_folders"] = bool(v_dot.get())
            self.cfg["max_content_file_mb"] = read_int(v_mb, 20, 1, 500)
            self.cfg["double_tap_ms"] = read_int(v_gap, 400, 200, 1000)
            self.cfg["rescan_minutes"] = read_int(v_scan, 60, 5, 1440)
            self.cfg["theme"] = v_theme.get()
            self.cfg["page_size"] = read_int(v_page, 5, 1, 50)
            self.cfg["excluded_dirs"] = [x.strip() for x in v_ex.get().split(",") if x.strip()]
            config.save(self.cfg)
            if IS_WIN:
                config.set_autostart(bool(v_auto.get()))
            self.detector.set_gap_ms(self.cfg["double_tap_ms"])
            if self.cfg["theme"] != old_theme:
                self._build_popup()
            if rebuild:
                self.indexer.rebuild()
            else:
                self.indexer.rescan_now()
            w.destroy()
            if self.cfg["folders"]:
                self.show()

        bar = tk.Frame(w)
        bar.pack(fill="x", side="bottom", padx=14, pady=12)
        ttk.Button(bar, text="Save", command=save).pack(side="right")
        ttk.Button(bar, text="Cancel", command=w.destroy).pack(side="right", padx=8)

        def rebuild():
            if messagebox.askyesno("Rebuild index", "Delete the index and scan everything again?\n"
                                   "(Normally never needed - indexing is incremental.)", parent=w):
                save(rebuild=True)
        ttk.Button(bar, text="Rebuild index", command=rebuild).pack(side="left")
        w.lift()
        w.focus_force()

    # ================================================================ main loop
    def _poll(self):
        try:
            while True:
                msg = self.q.get_nowait()
                try:
                    if msg == "toggle":
                        self.toggle()
                    elif msg == "show":
                        self.show()
                    elif msg == "settings":
                        self.open_settings()
                    elif msg == "reindex":
                        self.indexer.rescan_now()
                    elif msg == "quit":
                        self.quit()
                        return
                    elif isinstance(msg, tuple) and msg[0] == "results":
                        _, seq, query, res, ms = msg
                        if seq == self.seq:
                            self._render(res, query, ms)
                    elif isinstance(msg, tuple) and msg[0] == "details":
                        if msg[1] == self.seq:
                            self._redraw(sel=self.sel)
                    elif isinstance(msg, tuple) and msg[0] == "more":
                        _, seq, n = msg
                        self.more_pending = False
                        if seq == self.seq and n > self.shown:
                            self._reveal(n)
                    elif isinstance(msg, tuple) and msg[0] == "counts":
                        self._counts_busy = False
                        if msg[1]:
                            self._counts, self._counts_at = msg[1], time.time()
                            self._compose_index_status()
                except Exception:
                    import traceback
                    traceback.print_exc()
        except queue.Empty:
            pass
        if self.visible():
            self._update_status()
        self.root.after(30, self._poll)

    def quit(self):
        self.indexer.stop()
        if getattr(self, "_srv", None):
            try:
                self._srv.close()                  # free the port for a replacement instance
            except OSError:
                pass
        if self.listeners:
            for l in self.listeners:
                try:
                    l.stop()
                except Exception:
                    pass
        if self.tray:
            try:
                self.tray.stop()
            except Exception:
                pass
        self.root.destroy()

    def run(self):
        if self.tray:
            try:
                self.tray.notify("Tap Ctrl twice to search your files.", "DeskSearch is running")
            except Exception:
                pass
        self.root.mainloop()


def already_running():
    """'none' | 'same' (shown it) | 'replaced' (older copy told to quit) | 'old' (too old to talk to)."""
    try:
        with socket.create_connection(("127.0.0.1", SINGLE_INSTANCE_PORT), timeout=0.5) as s:
            s.settimeout(1.0)
            s.sendall(f"hello {APP_VERSION}".encode())
            try:
                reply = s.recv(16)
            except OSError:
                reply = b""
    except OSError:
        return "none"
    if reply == b"ok":
        return "same"
    if reply == b"quit":
        return "replaced"
    return "old"


if __name__ == "__main__":
    state = already_running()
    if state == "same":
        sys.exit(0)
    if state == "old":                 # an early DeskSearch build holds the hotkey and cannot be replaced remotely
        r = tk.Tk()
        r.withdraw()
        messagebox.showwarning(
            "DeskSearch", "An older DeskSearch is still running (look for its magnifier icon in the system tray, "
            "near the clock).\n\nRight-click it, choose Quit, then start DeskSearch again to get the new version.")
        sys.exit(0)
    if state == "replaced":
        for _ in range(30):            # wait for the old copy to release the port
            time.sleep(0.2)
            if already_running() == "none":
                break
    App().run()
