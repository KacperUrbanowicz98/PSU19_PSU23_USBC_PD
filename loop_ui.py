"""Okno trybu walidacyjnego (loop). Otwierane wyłącznie z panelu inżynieryjnego."""
from __future__ import annotations

import logging
import queue
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Callable

from config import AppConfig
from engine import TestEngine
from loop_test import LoopProgress, LoopReportWriter, LoopRunner, LoopSummary, build_report_lines
from models import PHASE_LABELS, TestResult
from theme import COLORS

logger = logging.getLogger(__name__)


class LoopWindow:
    """Seria powtórzonych testów na jednej sztuce wraz z raportem statystycznym."""

    def __init__(
        self,
        parent: tk.Misc,
        config: AppConfig,
        engine: TestEngine,
        raw_writer,
        operator_hrid: str | None,
        on_state_change: Callable[[bool], None],
        on_closed: Callable[[], None] | None = None,
        station_name: str = "",
    ) -> None:
        self.config = config
        self.engine = engine
        self.raw_writer = raw_writer
        self.operator_hrid = operator_hrid or "-"
        self.on_state_change = on_state_change
        self.on_closed = on_closed
        self.station_name = station_name

        self.running = False
        self.cancel_event = threading.Event()
        self.queue: queue.Queue[tuple] = queue.Queue()
        self.results: list[TestResult] = []
        self.summary: LoopSummary | None = None
        self._started_at = 0.0

        self.window = tk.Toplevel(parent)
        title = "Tryb walidacyjny — seria testów"
        if station_name:
            title += f"   ·   stanowisko {station_name}"
        self.window.title(title)
        screen_width = self.window.winfo_screenwidth()
        screen_height = self.window.winfo_screenheight()
        width = max(840, min(1020, screen_width - 80))
        height = max(600, min(820, screen_height - 140))
        self.window.geometry(f"{width}x{height}")
        self.window.minsize(min(900, width), min(620, height))
        self.window.configure(bg=COLORS["background"])
        self.window.protocol("WM_DELETE_WINDOW", self._close)

        self._build()
        self._center(parent)
        self.window.after(120, self._poll)

    def exists(self) -> bool:
        try:
            return bool(self.window.winfo_exists())
        except tk.TclError:
            return False

    def focus(self) -> None:
        self.window.lift()
        self.window.focus_force()

    # ------------------------------------------------------------- interfejs

    def _build(self) -> None:
        header = tk.Frame(self.window, bg=COLORS["warning"], height=84)
        header.pack(fill=tk.X)
        header.pack_propagate(False)
        area = tk.Frame(header, bg=COLORS["warning"])
        area.pack(side=tk.LEFT, fill=tk.Y, padx=24)
        tk.Label(
            area, text="Tryb walidacyjny", bg=COLORS["warning"], fg="white",
            font=("Segoe UI", 21, "bold"), anchor="w",
        ).pack(anchor="w", pady=(13, 0))
        tk.Label(
            area,
            text=(
                (f"Stanowisko {station_name} · " if station_name else "")
                + "seria testów na jednej sztuce — bez zapisu do raportu produkcyjnego "
                  "i bez zużycia licznika prób"
            ),
            bg=COLORS["warning"], fg="#FFF3DC", font=("Segoe UI", 9), anchor="w",
        ).pack(anchor="w", pady=(0, 10))
        tk.Label(
            header, text="TYLKO INŻYNIERIA", bg="#FFFFFF", fg=COLORS["warning"],
            font=("Segoe UI", 9, "bold"), padx=14, pady=8,
        ).pack(side=tk.RIGHT, padx=24, pady=24)

        body = tk.Frame(self.window, bg=COLORS["background"])
        body.pack(fill=tk.BOTH, expand=True, padx=20, pady=(16, 10))

        setup = self._card(body)
        setup.pack(fill=tk.X)
        self._section(setup, "Parametry serii", "Próbka zostaje w stanowisku na cały czas serii")

        form = tk.Frame(setup, bg=COLORS["card"])
        form.pack(fill=tk.X, padx=18, pady=(2, 16))
        form.columnconfigure(1, weight=1)

        self.label_var = tk.StringVar(value="WALIDACJA")
        self.cycles_var = tk.StringVar(value=str(self.config.loop_default_cycles))
        self.pause_var = tk.StringVar(value=str(self.config.loop_pause_seconds))
        self.raw_var = tk.BooleanVar(value=False)

        for row, (label, variable, suffix) in enumerate(
            (
                ("Oznaczenie próbki", self.label_var, "trafia do nazwy plików"),
                ("Liczba cykli", self.cycles_var, "szt."),
                ("Przerwa między cyklami", self.pause_var, "s"),
            )
        ):
            tk.Label(
                form, text=label, bg=COLORS["card"], fg=COLORS["text"],
                font=("Segoe UI", 9, "bold"), anchor="w",
            ).grid(row=row, column=0, sticky="w", pady=6, padx=(0, 12))
            ttk.Entry(
                form, textvariable=variable, font=("Segoe UI", 11), justify="center", width=18
            ).grid(row=row, column=1, sticky="w", pady=6)
            tk.Label(
                form, text=suffix, bg=COLORS["card"], fg=COLORS["muted"], font=("Segoe UI", 8), anchor="w"
            ).grid(row=row, column=2, sticky="w", padx=(10, 0))

        ttk.Checkbutton(
            form, text="Zapisuj surowe próbki każdego cyklu (folder raw/)", variable=self.raw_var
        ).grid(row=3, column=0, columnspan=3, sticky="w", pady=(8, 0))

        self.estimate_label = tk.Label(
            form, text="", bg=COLORS["primary_soft"], fg=COLORS["primary"],
            font=("Segoe UI", 9, "bold"), padx=10, pady=6, anchor="w", justify=tk.LEFT,
        )
        self.estimate_label.grid(row=4, column=0, columnspan=3, sticky="ew", pady=(10, 0))
        for variable in (self.cycles_var, self.pause_var):
            variable.trace_add("write", lambda *_: self._refresh_estimate())
        self._refresh_estimate()

        actions = tk.Frame(setup, bg=COLORS["card"])
        actions.pack(fill=tk.X, padx=18, pady=(0, 16))
        self.start_button = self._button(
            actions, "URUCHOM SERIĘ", self._start, COLORS["success"], COLORS["success_hover"],
            font_size=10, padx=22, pady=9,
        )
        self.start_button.pack(side=tk.LEFT)
        self.stop_button = self._button(
            actions, "ZATRZYMAJ", self._stop, COLORS["error"], COLORS["error_hover"],
            font_size=10, padx=22, pady=9,
        )
        self.stop_button.pack(side=tk.LEFT, padx=10)
        self.stop_button.config(state=tk.DISABLED)

        self.status_label = tk.Label(
            actions, text="Gotowe.", bg=COLORS["card"], fg=COLORS["muted"], font=("Segoe UI", 10)
        )
        self.status_label.pack(side=tk.LEFT, padx=16)

        progress_card = self._card(body)
        progress_card.pack(fill=tk.X, pady=(12, 0))
        progress_content = tk.Frame(progress_card, bg=COLORS["card"])
        progress_content.pack(fill=tk.X, padx=18, pady=14)
        self.progress_bar = ttk.Progressbar(
            progress_content, mode="determinate", maximum=100, style="Test.Horizontal.TProgressbar"
        )
        self.progress_bar.pack(fill=tk.X)
        counters = tk.Frame(progress_content, bg=COLORS["card"])
        counters.pack(fill=tk.X, pady=(8, 0))
        self.counter_label = tk.Label(
            counters, text="PASS 0 · FAIL 0 · ERROR 0", bg=COLORS["card"], fg=COLORS["text"],
            font=("Segoe UI", 11, "bold"),
        )
        self.counter_label.pack(side=tk.LEFT)
        self.time_label = tk.Label(
            counters, text="", bg=COLORS["card"], fg=COLORS["muted"], font=("Segoe UI", 10)
        )
        self.time_label.pack(side=tk.RIGHT)

        report_card = self._card(body)
        report_card.pack(fill=tk.BOTH, expand=True, pady=(12, 0))
        self._section(report_card, "Raport serii", "Zapisywany także do pliku po zakończeniu")
        text_area = tk.Frame(report_card, bg=COLORS["card"])
        text_area.pack(fill=tk.BOTH, expand=True, padx=18, pady=(0, 16))
        scrollbar = ttk.Scrollbar(text_area)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.report_text = tk.Text(
            text_area, font=("Consolas", 9), wrap=tk.NONE, bg="#111820", fg="#DCE7F0",
            relief=tk.FLAT, borderwidth=0, padx=12, pady=10, yscrollcommand=scrollbar.set,
        )
        self.report_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.config(command=self.report_text.yview)
        self.report_text.insert(
            tk.END,
            "Ustaw liczbę cykli i uruchom serię.\n\n"
            "Wskazówki:\n"
            "  • 20–30 cykli na jednym sprawnym egzemplarzu wystarcza do oceny powtarzalności,\n"
            "  • powtórz serię na egzemplarzu granicznym (blisko dolnej granicy napięcia),\n"
            "  • zostaw sztukę w stanowisku na cały czas serii — inaczej mierzysz też styk,\n"
            "  • dryf napięcia w serii zwykle oznacza nagrzewanie się zasilacza lub obciążenia.",
        )
        self.report_text.config(state=tk.DISABLED)

        footer = tk.Frame(self.window, bg=COLORS["background"])
        footer.pack(side=tk.BOTTOM, fill=tk.X, padx=20, pady=(8, 12))
        self.path_label = tk.Label(
            footer, text="", bg=COLORS["background"], fg=COLORS["muted"], font=("Segoe UI", 9), anchor="w"
        )
        self.path_label.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self._button(
            footer, "ZAMKNIJ", self._close, COLORS["card"], COLORS["primary_soft"],
            foreground=COLORS["primary"], active_foreground=COLORS["primary"], padx=20, pady=8,
        ).pack(side=tk.RIGHT)

    # ---------------------------------------------------------------- akcje

    def _parameters(self) -> tuple[str, int, float] | None:
        label = self.label_var.get().strip() or "WALIDACJA"
        try:
            cycles = int(self.cycles_var.get())
            pause = float(str(self.pause_var.get()).replace(",", "."))
        except ValueError:
            messagebox.showerror(
                "Tryb walidacyjny", "Liczba cykli i przerwa muszą być liczbami.", parent=self.window
            )
            return None
        if not 1 <= cycles <= 500:
            messagebox.showerror(
                "Tryb walidacyjny", "Liczba cykli musi mieścić się w zakresie 1–500.", parent=self.window
            )
            return None
        if pause < 0:
            messagebox.showerror("Tryb walidacyjny", "Przerwa nie może być ujemna.", parent=self.window)
            return None
        return label, cycles, pause

    def _refresh_estimate(self) -> None:
        parameters = None
        try:
            cycles = int(self.cycles_var.get())
            pause = float(str(self.pause_var.get()).replace(",", "."))
            parameters = (cycles, pause)
        except (ValueError, tk.TclError):
            pass
        if not parameters:
            self.estimate_label.config(text="Podaj poprawną liczbę cykli i przerwę.")
            return
        cycles, pause = parameters
        per_cycle = self.config.estimated_test_seconds() + pause
        total = per_cycle * cycles
        minutes, seconds = divmod(int(total), 60)
        self.estimate_label.config(
            text=(
                f"Jeden cykl ≈ {per_cycle:.1f} s   ·   cała seria ≈ {minutes} min {seconds:02d} s.\n"
                "Przez ten czas stanowisko jest zajęte, a testy produkcyjne zablokowane."
            )
        )

    def _start(self) -> None:
        if self.running:
            return
        if not self.engine.hardware.connected:
            messagebox.showwarning(
                "Tryb walidacyjny", "PM125 nie jest połączony.", parent=self.window
            )
            return
        parameters = self._parameters()
        if parameters is None:
            return
        label, cycles, pause = parameters
        if not messagebox.askyesno(
            "Tryb walidacyjny",
            f"Uruchomić {cycles} cykli na próbce „{label}”?\n\n"
            "Zostaw zasilacz w stanowisku do końca serii.\n"
            "Wyniki nie trafią do raportu produkcyjnego.",
            parent=self.window,
        ):
            return

        self.running = True
        self.results = []
        self.summary = None
        self.cancel_event = threading.Event()
        self._started_at = time.monotonic()
        self.on_state_change(True)
        self.start_button.config(state=tk.DISABLED)
        self.stop_button.config(state=tk.NORMAL)
        self.progress_bar["value"] = 0
        self._set_report(["Seria w toku..."])
        logger.info(
            "Tryb walidacyjny: uruchomiono %s cykli, próbka=%s, operator=%s",
            cycles, label, self.operator_hrid,
        )

        runner = LoopRunner(self.config, self.engine)
        writer = LoopReportWriter(self.config.validation_path)
        raw_writer = self.raw_writer if self.raw_var.get() else None

        def worker() -> None:
            try:
                summary, results = runner.run(
                    label=(f"{self.station_name}-{label}" if self.station_name else label),
                    hrid=self.operator_hrid,
                    cycles=cycles,
                    pause_seconds=pause,
                    progress=lambda event: self.queue.put(("progress", event)),
                    cancel_event=self.cancel_event,
                    raw_writer=raw_writer,
                )
                self.results = results
                writer.write(summary, results)
                self.queue.put(("done", summary))
            except Exception as exc:
                logger.exception("Tryb walidacyjny: seria nie powiodła się")
                self.queue.put(("error", str(exc)))

        threading.Thread(target=worker, daemon=True, name="loop-validation").start()

    def _stop(self) -> None:
        if not self.running:
            return
        self.cancel_event.set()
        self.stop_button.config(state=tk.DISABLED, text="ZATRZYMYWANIE...")
        self.status_label.config(text="Zatrzymywanie po bieżącym cyklu...")

    def _close(self) -> None:
        if self.running and not messagebox.askyesno(
            "Tryb walidacyjny",
            "Seria trwa. Zatrzymać ją i zamknąć okno?",
            parent=self.window,
        ):
            return
        self.cancel_event.set()
        self.on_state_change(False)
        if self.window.winfo_exists():
            self.window.destroy()
        if self.on_closed:
            self.on_closed()

    # -------------------------------------------------------------- kolejka

    def _poll(self) -> None:
        try:
            while True:
                kind, payload = self.queue.get_nowait()
                if kind == "progress":
                    self._handle_progress(payload)
                elif kind == "done":
                    self._handle_done(payload)
                elif kind == "error":
                    self._handle_error(payload)
        except queue.Empty:
            pass
        except Exception:
            logger.exception("Tryb walidacyjny: błąd obsługi zdarzenia")
        if self.exists():
            self.window.after(120, self._poll)

    def _handle_progress(self, event: LoopProgress) -> None:
        done = event.cycle - (1 if event.stage == "test" else 0)
        self.progress_bar["value"] = min(100.0, done / max(1, event.total) * 100.0)
        self.counter_label.config(
            text=f"PASS {event.passed} · FAIL {event.failed} · ERROR {event.errors}"
        )
        minutes, seconds = divmod(int(event.elapsed_seconds), 60)
        remaining = ""
        if done and event.total > done:
            per_cycle = event.elapsed_seconds / done
            left = int(per_cycle * (event.total - done))
            left_minutes, left_seconds = divmod(left, 60)
            remaining = f"   ·   pozostało ~{left_minutes} min {left_seconds:02d} s"
        self.time_label.config(text=f"czas serii {minutes}:{seconds:02d}{remaining}")
        if event.stage == "test":
            self.status_label.config(text=f"Cykl {event.cycle} z {event.total} — test w toku...")
        elif event.stage == "przerwa":
            self.status_label.config(
                text=f"Cykl {event.cycle} z {event.total} — {event.last_status}, przerwa..."
            )

    def _handle_done(self, summary: LoopSummary) -> None:
        self.running = False
        self.summary = summary
        self.on_state_change(False)
        self.start_button.config(state=tk.NORMAL)
        self.stop_button.config(state=tk.DISABLED, text="ZATRZYMAJ")
        self.progress_bar["value"] = 100
        self.counter_label.config(
            text=f"PASS {summary.passed} · FAIL {summary.failed} · ERROR {summary.errors}"
        )
        self.status_label.config(
            text=("Seria przerwana." if summary.cancelled else "Seria zakończona.")
        )
        self._set_report(build_report_lines(summary))
        if summary.report_path:
            self.path_label.config(text=f"Raport: {summary.report_path}")
        worst = summary.worst_phase
        if worst is not None:
            logger.info(
                "Tryb walidacyjny: najciaśniejszy zapas %s %s = %.0f mV (%.1f %%)",
                worst.profile, PHASE_LABELS[worst.phase],
                worst.worst_margin * 1000, worst.worst_margin_percent,
            )

    def _handle_error(self, message: str) -> None:
        self.running = False
        self.on_state_change(False)
        self.start_button.config(state=tk.NORMAL)
        self.stop_button.config(state=tk.DISABLED, text="ZATRZYMAJ")
        self.status_label.config(text="Seria przerwana błędem.")
        messagebox.showerror("Tryb walidacyjny", message, parent=self.window)

    def _set_report(self, lines: list[str]) -> None:
        self.report_text.config(state=tk.NORMAL)
        self.report_text.delete("1.0", tk.END)
        self.report_text.insert(tk.END, "\n".join(lines))
        self.report_text.config(state=tk.DISABLED)

    # ------------------------------------------------------------ narzędzia

    def _card(self, parent: tk.Widget) -> tk.Frame:
        return tk.Frame(
            parent, bg=COLORS["card"], highlightbackground=COLORS["border"], highlightthickness=1
        )

    def _section(self, parent: tk.Widget, title: str, subtitle: str) -> None:
        area = tk.Frame(parent, bg=COLORS["card"])
        area.pack(fill=tk.X, padx=18, pady=(14, 6))
        tk.Label(
            area, text=title, bg=COLORS["card"], fg=COLORS["text"],
            font=("Segoe UI", 12, "bold"), anchor="w",
        ).pack(anchor="w")
        tk.Label(
            area, text=subtitle, bg=COLORS["card"], fg=COLORS["muted"],
            font=("Segoe UI", 8), anchor="w",
        ).pack(anchor="w", pady=(1, 0))

    def _button(
        self,
        parent: tk.Widget,
        text: str,
        command,
        color: str,
        hover_color: str,
        *,
        foreground: str = "white",
        active_foreground: str = "white",
        font_size: int = 9,
        padx: int = 14,
        pady: int = 8,
    ) -> tk.Button:
        button = tk.Button(
            parent, text=text, command=command, bg=color, fg=foreground,
            activebackground=hover_color, activeforeground=active_foreground,
            disabledforeground="#AAB4BD", relief=tk.FLAT, borderwidth=0,
            font=("Segoe UI", font_size, "bold"), padx=padx, pady=pady, cursor="hand2",
        )
        button.bind("<Enter>", lambda _e: button.config(bg=hover_color) if button["state"] == tk.NORMAL else None)
        button.bind("<Leave>", lambda _e: button.config(bg=color) if button["state"] == tk.NORMAL else None)
        return button

    def _center(self, parent: tk.Misc) -> None:
        self.window.update_idletasks()
        screen_width = self.window.winfo_screenwidth()
        screen_height = self.window.winfo_screenheight()
        width = min(max(self.window.winfo_width(), 900), screen_width - 60)
        height = min(max(self.window.winfo_height(), 620), screen_height - 120)
        x = max(0, (screen_width - width) // 2)
        y = max(0, (screen_height - height) // 3)
        self.window.geometry(f"{width}x{height}+{x}+{y}")
