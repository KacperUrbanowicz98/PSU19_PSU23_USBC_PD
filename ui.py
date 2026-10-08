"""Interfejs operatora (Tkinter). Obsługuje jeden lub kilka kanałów pomiarowych.

Liczba kolumn wynika z listy `stations` w konfiguracji: jeden wpis to interfejs
jednokanałowy, dwa wpisy to dwa niezależne gniazda obok siebie. Sprzęt i testy
każdego kanału pracują w osobnych wątkach; wspólne pozostają: zalogowany operator,
raport, licznik prób, dane surowe i panel inżynieryjny.
"""
from __future__ import annotations

import logging
import queue
import threading
import tkinter as tk
import time
from collections import deque
from pathlib import Path
from tkinter import messagebox, simpledialog, ttk

from config import AppConfig, StationConfig
from hardware import Hardware, PM125Client, SimulatedPM125
from logging_utils import configure_logging
from loop_ui import LoopWindow
from models import FinalStatus, TestResult
from settings_ui import SettingsDialog
from station_panel import StationPanel
from storage import (
    AttemptStore,
    CsvResultRepository,
    PendingRecord,
    PendingResultStore,
    RawSampleWriter,
)
from theme import COLORS

logger = logging.getLogger(__name__)


class TesterApp:
    """Okno główne: nagłówek, logowanie operatora, kanały, statystyki i historia."""

    def __init__(
        self,
        root: tk.Tk,
        config: AppConfig,
        hardware: Hardware | list[Hardware],
        config_path: Path,
        simulation_mode: bool = False,
    ) -> None:
        self.root = root
        self.config = config
        self.config_path = config_path
        self.simulation_mode = simulation_mode

        self.results = CsvResultRepository(config.reports_path, config.backup_path, config.maximum_csv_rows)
        self.raw_writer = RawSampleWriter(
            config.raw_path, config.raw_samples_enabled, config.raw_retention_days
        )
        self.attempts = AttemptStore(config.attempts_path, config.attempts_retention_days)
        self.pending = PendingResultStore(config.pending_path)

        self.operator_hrid: str | None = None
        self.closing = False
        self.ui_queue: queue.Queue[tuple] = queue.Queue()
        self.history: deque[tuple[str, str, str, str, float]] = deque(maxlen=4)
        self._engineering_presses: list[float] = []
        self._settings_dialog: SettingsDialog | None = None
        self._loop_window: LoopWindow | None = None
        self._loop_running = False

        devices = hardware if isinstance(hardware, list) else [hardware]
        stations = config.active_stations()
        self.panels: list[StationPanel] = []

        self._configure_window(len(stations))
        self._build_ui(stations, devices)
        self._sync_all()
        self.root.after(100, self._poll_queue)
        self.root.after(250, self._connect_all)
        self._schedule_health_check()
        self._refresh_header()
        if self.pending.count:
            logger.warning("Odtworzono %s niezapisanych wyników z poprzedniej sesji.", self.pending.count)
        if config.verify_station_configuration and len(stations) > 1:
            self.root.after(1500, self._verify_station_configuration)

    # ------------------------------------------------ usługi wspólne dla paneli

    @property
    def operator_logged(self) -> bool:
        return self.operator_hrid is not None

    @property
    def blocked(self) -> bool:
        return bool(self.pending.count) or self._loop_running

    @property
    def blocked_reason(self) -> str:
        if self._loop_running:
            return "Trwa seria walidacyjna — testy produkcyjne są zablokowane."
        if self.pending.count:
            return "Niezapisany wynik. Użyj „Ponów zapis”."
        return ""

    def post(self, event: tuple) -> None:
        """Wątki robocze paneli oddają zdarzenia do wątku GUI przez tę kolejkę."""
        self.ui_queue.put(event)

    def add_pending(self, record: PendingRecord) -> None:
        self.pending.add(record)
        self._sync_pending_banner()
        self._sync_all()

    def register_result(self, panel: StationPanel, result: TestResult) -> None:
        self.history.appendleft(
            (
                result.timestamp,
                panel.station.name,
                result.serial_number,
                result.final_status.value,
                result.duration_seconds,
            )
        )
        self._update_stats()
        self._refresh_history()

    # ------------------------------------------------------------------ okno

    def _configure_window(self, station_count: int) -> None:
        title = "PSU USB-C Tester — Reconext"
        self.root.title(title)
        screen_width = self.root.winfo_screenwidth()
        screen_height = self.root.winfo_screenheight()
        preferred = 1000 if station_count < 2 else min(1850, 560 + 460 * station_count)
        width = max(900, min(preferred, screen_width - 40))
        height = max(680, min(950, screen_height - 80))
        self.root.geometry(f"{width}x{height}")
        self.root.minsize(min(900 if station_count < 2 else 1200, width), min(680, height))
        self.root.configure(bg=COLORS["background"])
        self.root.protocol("WM_DELETE_WINDOW", self._close_app)
        self.root.bind("<Control-Shift-D>", self._engineering_key)

        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure(
            "Modern.TEntry",
            fieldbackground=COLORS["entry"], bordercolor=COLORS["border"],
            lightcolor=COLORS["border"], darkcolor=COLORS["border"], padding=8,
        )
        style.map(
            "Modern.TEntry",
            bordercolor=[("focus", COLORS["primary"])],
            lightcolor=[("focus", COLORS["primary"])],
            darkcolor=[("focus", COLORS["primary"])],
        )
        for name, row_height, font in (
            ("Session.Treeview", 29, ("Segoe UI", 10)),
            ("Result.Treeview", 28, ("Segoe UI", 10, "bold")),
            ("Settings.Treeview", 30, ("Segoe UI", 10)),
        ):
            style.configure(
                name, rowheight=row_height, font=font, background=COLORS["card"],
                fieldbackground=COLORS["card"], foreground=COLORS["text"], borderwidth=0,
            )
            style.configure(
                f"{name}.Heading", font=("Segoe UI", 9, "bold"), background=COLORS["primary_soft"],
                foreground=COLORS["primary"], relief=tk.FLAT, padding=(6, 7),
            )
        style.map("Session.Treeview", background=[("selected", COLORS["primary_soft"])])
        style.configure(
            "Test.Horizontal.TProgressbar",
            troughcolor=COLORS["primary_soft"], background=COLORS["primary"],
            bordercolor=COLORS["primary_soft"], lightcolor=COLORS["primary"],
            darkcolor=COLORS["primary"],
        )
        style.configure("TNotebook.Tab", padding=(12, 7))

    def _build_ui(self, stations: list[StationConfig], devices: list[Hardware]) -> None:
        self._build_header(len(stations))

        body = tk.Frame(self.root, bg=COLORS["background"])
        body.pack(fill=tk.BOTH, expand=True, padx=18, pady=(14, 10))

        operator_card = self._card(body)
        operator_card.pack(fill=tk.X)
        self._build_operator_bar(operator_card)

        self.pending_banner = tk.Frame(
            body, bg=COLORS["error_soft"], highlightbackground="#F1B7B7", highlightthickness=1
        )
        self.pending_message = tk.Label(
            self.pending_banner, text="", bg=COLORS["error_soft"], fg=COLORS["error"],
            font=("Segoe UI", 10, "bold"), anchor="w",
        )
        self.pending_message.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=14, pady=9)
        self.pending_retry_button = self._button(
            self.pending_banner, "Ponów zapis", self.retry_pending_save,
            COLORS["error"], COLORS["error_hover"], font_size=9, padx=14, pady=6,
        )
        self.pending_retry_button.pack(side=tk.RIGHT, padx=10, pady=6)

        channels = tk.Frame(body, bg=COLORS["background"])
        channels.pack(fill=tk.BOTH, expand=True, pady=(12, 0))
        compact = len(stations) > 1
        for index, station in enumerate(stations):
            channels.columnconfigure(index, weight=1, uniform="channel")
            device = devices[index] if index < len(devices) else devices[-1]
            panel = StationPanel(channels, station, self.config, device, self, compact=compact)
            panel.frame.grid(
                row=0, column=index, sticky="nsew",
                padx=(0 if index == 0 else 8, 0 if index == len(stations) - 1 else 8),
            )
            self.panels.append(panel)
        channels.rowconfigure(0, weight=1)

        stats_row = tk.Frame(body, bg=COLORS["background"])
        stats_row.pack(fill=tk.X, pady=(12, 0))
        for column in range(3):
            stats_row.columnconfigure(column, weight=1)
        self.pass_value = self._stat_card(stats_row, 0, "PASS", COLORS["success"])
        self.fail_value = self._stat_card(stats_row, 1, "FAIL / ERROR", COLORS["error"])
        self.rate_value = self._stat_card(stats_row, 2, "PASS RATE", COLORS["primary"])

        history_card = self._card(body)
        history_card.pack(fill=tk.X, pady=(12, 0))
        self._build_history(history_card)

        footer = tk.Frame(self.root, bg=COLORS["background"], height=30)
        footer.pack(fill=tk.X)
        footer.pack_propagate(False)
        tk.Label(
            footer, text="Autor: Kacper Urbanowicz", bg=COLORS["background"],
            fg=COLORS["muted"], font=("Segoe UI", 9),
        ).pack(side=tk.LEFT, padx=20, pady=5)

        self._update_stats()
        self._sync_pending_banner()

    def _build_header(self, station_count: int) -> None:
        header = tk.Frame(self.root, bg=COLORS["primary"], height=74)
        header.pack(fill=tk.X)
        header.pack_propagate(False)

        area = tk.Frame(header, bg=COLORS["primary"])
        area.pack(side=tk.LEFT, fill=tk.Y, padx=22)
        title = "PSU USB-C Tester"
        if self.simulation_mode:
            title += "  ·  SIMULATOR"
        tk.Label(
            area, text=title, bg=COLORS["primary"], fg="white",
            font=("Segoe UI", 21, "bold"), anchor="w",
        ).pack(anchor="w", pady=(11, 0))
        subtitle = "Automatyczny test zasilaczy USB Power Delivery"
        if station_count > 1:
            subtitle += f"   ·   {station_count} stanowiska niezależne"
        tk.Label(
            area, text=subtitle, bg=COLORS["primary"], fg="#DDEAF4",
            font=("Segoe UI", 9), anchor="w",
        ).pack(anchor="w", pady=(0, 9))

    def _build_operator_bar(self, card: tk.Frame) -> None:
        content = tk.Frame(card, bg=COLORS["card"])
        content.pack(fill=tk.X, padx=16, pady=12)

        tk.Label(
            content, text="Operator", bg=COLORS["card"], fg=COLORS["text"],
            font=("Segoe UI", 12, "bold"),
        ).pack(side=tk.LEFT, padx=(0, 14))

        self.operator_state_label = tk.Label(
            content, text="", bg=COLORS["primary_soft"], fg=COLORS["primary"],
            font=("Segoe UI", 10, "bold"), padx=10, pady=6,
        )
        self.operator_state_label.pack(side=tk.LEFT, padx=(0, 16))

        tk.Label(
            content, text="Numer HRID", bg=COLORS["card"], fg=COLORS["text"],
            font=("Segoe UI", 9, "bold"),
        ).pack(side=tk.LEFT, padx=(0, 6))
        self.hrid_entry = ttk.Entry(
            content, style="Modern.TEntry", font=("Segoe UI", 11), justify="center", width=18
        )
        self.hrid_entry.pack(side=tk.LEFT)
        self.hrid_entry.bind("<Return>", lambda _event: self.login())

        self.login_button = self._button(
            content, "Zaloguj", self.login, COLORS["primary"], COLORS["primary_hover"],
            padx=18, pady=7,
        )
        self.login_button.pack(side=tk.LEFT, padx=(8, 0))
        self.logout_button = self._button(
            content, "Wyloguj", self.logout, COLORS["error"], COLORS["error_hover"],
            padx=18, pady=7,
        )
        self.logout_button.pack(side=tk.LEFT, padx=(8, 0))

        self.station_hint = tk.Label(
            content, text="", bg=COLORS["card"], fg=COLORS["muted"], font=("Segoe UI", 9)
        )
        self.station_hint.pack(side=tk.RIGHT)

    def _build_history(self, card: tk.Frame) -> None:
        header = tk.Frame(card, bg=COLORS["card"])
        header.pack(fill=tk.X, padx=16, pady=(10, 6))
        tk.Label(
            header, text="Ostatnie testy tej sesji", bg=COLORS["card"], fg=COLORS["text"],
            font=("Segoe UI", 11, "bold"),
        ).pack(side=tk.LEFT)

        columns = ("date", "station", "serial", "result", "duration")
        self.history_tree = ttk.Treeview(
            card, columns=columns, show="headings", height=4,
            style="Session.Treeview", selectmode="none",
        )
        for column, heading, width, stretch in (
            ("date", "Data", 150, False),
            ("station", "Stanowisko", 130, False),
            ("serial", "Numer seryjny", 420, True),
            ("result", "Wynik", 120, False),
            ("duration", "Czas", 90, False),
        ):
            self.history_tree.heading(column, text=heading)
            self.history_tree.column(
                column, width=width, stretch=stretch,
                anchor=tk.W if column == "serial" else tk.CENTER,
            )
        self.history_tree.tag_configure("pass", foreground=COLORS["success"], font=("Segoe UI", 10, "bold"))
        self.history_tree.tag_configure("nonpass", foreground=COLORS["error"], font=("Segoe UI", 10, "bold"))
        self.history_tree.pack(fill=tk.X, padx=16, pady=(0, 12))

    # ------------------------------------------------------------- elementy

    def _card(self, parent: tk.Widget) -> tk.Frame:
        return tk.Frame(
            parent, bg=COLORS["card"], highlightbackground=COLORS["border"], highlightthickness=1
        )

    def _stat_card(self, parent: tk.Widget, column: int, title: str, color: str) -> tk.Label:
        card = tk.Frame(
            parent, bg=COLORS["card"], highlightbackground=COLORS["border"], highlightthickness=1
        )
        card.grid(row=0, column=column, sticky="ew", padx=(0 if column == 0 else 6, 0 if column == 2 else 6))
        tk.Frame(card, bg=color, width=5).pack(side=tk.LEFT, fill=tk.Y)
        content = tk.Frame(card, bg=COLORS["card"])
        content.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=12, pady=7)
        tk.Label(
            content, text=title, bg=COLORS["card"], fg=COLORS["muted"],
            font=("Segoe UI", 8, "bold"), anchor="w",
        ).pack(anchor="w")
        value = tk.Label(
            content, text="0", bg=COLORS["card"], fg=color, font=("Segoe UI", 21, "bold"), anchor="w"
        )
        value.pack(anchor="w")
        return value

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
        button.bind(
            "<Enter>", lambda _e: button.config(bg=hover_color) if button["state"] == tk.NORMAL else None
        )
        button.bind(
            "<Leave>", lambda _e: button.config(bg=color) if button["state"] == tk.NORMAL else None
        )
        return button

    # -------------------------------------------------------------- operator

    def login(self) -> None:
        hrid = self.hrid_entry.get().strip()
        if hrid not in self.config.valid_hrids:
            messagebox.showwarning("HRID", "Nieprawidłowy HRID.")
            return
        self.operator_hrid = hrid
        self.hrid_entry.delete(0, tk.END)
        logger.info("Operator zalogowany: %s", hrid)
        self._sync_all()
        if self.panels:
            self.panels[0].focus_serial()

    def logout(self) -> None:
        if any(panel.busy for panel in self.panels):
            messagebox.showwarning("Operator", "Poczekaj na zakończenie trwających testów.")
            return
        logger.info("Operator wylogowany: %s", self.operator_hrid)
        self.operator_hrid = None
        self._sync_all()
        self.hrid_entry.focus_set()

    def _update_operator_state(self) -> None:
        if self.operator_hrid:
            self.operator_state_label.config(
                text=f"Zalogowano: {self.operator_hrid}",
                bg=COLORS["success_soft"], fg=COLORS["success"],
            )
        else:
            self.operator_state_label.config(
                text="Operator niezalogowany", bg=COLORS["primary_soft"], fg=COLORS["primary"]
            )
        if len(self.panels) > 1:
            running = [panel.station.name for panel in self.panels if panel.busy]
            self.station_hint.config(
                text=("Test w toku: " + ", ".join(running)) if running else "Oba gniazda wolne"
            )

    # ---------------------------------------------------------------- sprzęt

    def _refresh_header(self) -> None:
        """Odświeża informację o zajętości gniazd — stan zmienia się w wątkach paneli."""
        if self.closing:
            return
        self._update_operator_state()
        self.root.after(500, self._refresh_header)

    def _connect_all(self) -> None:
        for panel in self.panels:
            panel.connect_hardware()

    def _schedule_health_check(self) -> None:
        interval = self.config.health_check_seconds
        if interval <= 0:
            return
        self.root.after(int(interval * 1000), self._health_check)

    def _health_check(self) -> None:
        if self.closing:
            return
        if not self._loop_running:
            for panel in self.panels:
                panel.health_check()
        self._schedule_health_check()

    def _verify_station_configuration(self) -> None:
        """Porównuje konfigurację testerów (-r). Różnica = inny wynik zależnie od gniazda."""
        def worker() -> None:
            readings: list[tuple[str, str | None]] = []
            for panel in self.panels:
                reader = getattr(panel.hardware, "read_configuration", None)
                readings.append((panel.station.name, reader() if callable(reader) else None))
            self.post(("station_config", readings))

        threading.Thread(target=worker, daemon=True, name="verify-stations").start()

    def _handle_station_config(self, readings: list[tuple[str, str | None]]) -> None:
        available = [(name, text) for name, text in readings if text]
        if len(available) < 2:
            return
        reference_name, reference = available[0]
        differing = [name for name, text in available[1:] if text.strip() != reference.strip()]
        if not differing:
            logger.info("Konfiguracja testerów zgodna na wszystkich stanowiskach.")
            return
        logger.warning(
            "Różna konfiguracja testerów: %s vs %s", reference_name, ", ".join(differing)
        )
        messagebox.showwarning(
            "Konfiguracja testerów",
            f"Tester na stanowisku {', '.join(differing)} ma inną konfigurację niż "
            f"{reference_name}.\n\nTa sama sztuka może dostać różny wynik zależnie od gniazda. "
            "Porównaj ustawienia testerów (komenda -r) przed uruchomieniem produkcji.",
        )

    # --------------------------------------------------------------- kolejka

    def _poll_queue(self) -> None:
        try:
            while True:
                event = self.ui_queue.get_nowait()
                kind = event[0]
                try:
                    if kind == "connection":
                        _, panel, connected, error = event
                        panel.connecting = False
                        if not connected and error:
                            logger.warning("[%s] Brak testera: %s", panel.station.name, error)
                        panel.sync_controls()
                        self._update_operator_state()
                    elif kind == "health":
                        _, panel, alive, error = event
                        if not alive:
                            logger.warning("[%s] Kontrola obecności nieudana: %s",
                                           panel.station.name, error)
                        panel.sync_controls()
                    elif kind == "progress":
                        event[1].handle_progress(event[2])
                    elif kind == "test_done":
                        event[1].handle_test_done(event[2], event[3])
                    elif kind == "test_error":
                        event[1].handle_test_error(event[2], event[3])
                    elif kind == "no_dut":
                        event[1].handle_no_dut(event[2])
                    elif kind == "save_done":
                        self._handle_save_done(event[1], event[2])
                    elif kind == "station_config":
                        self._handle_station_config(event[1])
                except Exception:
                    logger.exception("Błąd obsługi zdarzenia %s", kind)
        except queue.Empty:
            pass
        finally:
            if not self.closing:
                self.root.after(100, self._poll_queue)

    # ------------------------------------------------- wyniki oczekujące

    def retry_pending_save(self) -> None:
        if not self.pending.count or any(panel.busy for panel in self.panels):
            return
        records = self.pending.all()

        def worker() -> None:
            saved: list[PendingRecord] = []
            failure = ""
            for record in records:
                outcome = self.results.save_record(record)
                if outcome.saved:
                    saved.append(record)
                else:
                    failure = outcome.message
                    break
            self.post(("save_done", saved, failure))

        threading.Thread(target=worker, daemon=True, name="retry-save").start()

    def _handle_save_done(self, saved: list[PendingRecord], failure: str) -> None:
        for record in saved:
            self.pending.remove(record)
        if failure:
            messagebox.showerror("CSV", f"Zapisano {len(saved)} wyników.\n\n{failure}")
        else:
            messagebox.showinfo("CSV", f"Zapisano zaległe wyniki: {len(saved)}.")
        self._sync_pending_banner()
        self._sync_all()

    def _sync_pending_banner(self) -> None:
        count = self.pending.count
        if count:
            self.pending_message.config(
                text=f"Niezapisane wyniki: {count}. Testy są zablokowane. "
                     "Wyniki są zabezpieczone na dysku i przetrwają restart aplikacji."
            )
            if not self.pending_banner.winfo_manager():
                self.pending_banner.pack(fill=tk.X, pady=(10, 0))
            self.pending_retry_button.config(
                state=tk.DISABLED if any(panel.busy for panel in self.panels) else tk.NORMAL
            )
        elif self.pending_banner.winfo_manager():
            self.pending_banner.pack_forget()

    # -------------------------------------------------------------- podsumy

    def _update_stats(self) -> None:
        passed = sum(panel.session_pass for panel in self.panels)
        failed = sum(panel.session_non_pass for panel in self.panels)
        total = passed + failed
        rate = passed / total * 100 if total else 0.0
        self.pass_value.config(text=str(passed))
        self.fail_value.config(text=str(failed))
        self.rate_value.config(text=f"{rate:.1f}%")

    def _refresh_history(self) -> None:
        for item in self.history_tree.get_children():
            self.history_tree.delete(item)
        for timestamp, station, serial, status, duration in self.history:
            tag = "pass" if status == FinalStatus.PASS.value else "nonpass"
            self.history_tree.insert(
                "", tk.END, values=(timestamp, station, serial, status, f"{duration:.1f} s"),
                tags=(tag,),
            )

    def _sync_all(self) -> None:
        logged = self.operator_logged
        busy = any(panel.busy for panel in self.panels)
        self.hrid_entry.config(state=tk.DISABLED if logged else tk.NORMAL)
        self.login_button.config(state=tk.DISABLED if logged else tk.NORMAL)
        self.logout_button.config(state=tk.NORMAL if logged and not busy else tk.DISABLED)
        self._update_operator_state()
        for panel in self.panels:
            panel.sync_controls()
        self._sync_pending_banner()

    # ------------------------------------------------- panel inżynieryjny

    def _engineering_key(self, _event=None) -> None:
        if any(panel.busy for panel in self.panels):
            return
        now = time.monotonic()
        self._engineering_presses = [stamp for stamp in self._engineering_presses if now - stamp < 2.0]
        self._engineering_presses.append(now)
        if len(self._engineering_presses) >= 3:
            self._engineering_presses.clear()
            password = simpledialog.askstring(
                "Ustawienia inżynieryjne", "Hasło:", show="*", parent=self.root
            )
            if password is None:
                return
            if not self.config.verify_engineering_password(password):
                messagebox.showerror("Ustawienia inżynieryjne", "Nieprawidłowe hasło.")
                logger.warning("Nieudana próba wejścia do panelu inżynieryjnego.")
                return
            logger.info("Wejście do panelu inżynieryjnego (operator=%s).", self.operator_hrid or "-")
            self._open_settings()

    def _open_settings(self) -> None:
        if self._settings_dialog and self._settings_dialog.exists():
            self._settings_dialog.focus()
            return
        self._settings_dialog = SettingsDialog(
            self.root,
            self.config,
            self.config_path,
            self._apply_new_config,
            attempts=self.attempts,
            is_busy=lambda: any(panel.busy for panel in self.panels),
            operator_hrid=self.operator_hrid,
            open_loop=self._open_loop_window,
        )

    def _open_loop_window(self) -> None:
        """Tryb walidacyjny — wyłącznie z panelu inżynieryjnego, czyli po haśle."""
        if self._loop_window is not None and self._loop_window.exists():
            self._loop_window.focus()
            return
        if any(panel.busy for panel in self.panels):
            messagebox.showwarning("Tryb walidacyjny", "Trwa test zasilacza.")
            return
        panel = self._choose_loop_panel()
        if panel is None:
            return
        self._settings_dialog = None
        self._loop_window = LoopWindow(
            self.root,
            self.config,
            panel.engine,
            self.raw_writer,
            self.operator_hrid,
            on_state_change=self._loop_state_changed,
            on_closed=self._loop_closed,
            station_name=panel.station.name,
        )
        self._sync_all()

    def _choose_loop_panel(self) -> StationPanel | None:
        if len(self.panels) == 1:
            return self.panels[0]
        names = [panel.station.name for panel in self.panels]
        answer = simpledialog.askstring(
            "Tryb walidacyjny",
            "Na którym stanowisku uruchomić serię?\n\nDostępne: " + ", ".join(names),
            initialvalue=names[0],
            parent=self.root,
        )
        if answer is None:
            return None
        chosen = answer.strip().upper()
        for panel in self.panels:
            if panel.station.name.upper() == chosen:
                return panel
        messagebox.showwarning("Tryb walidacyjny", f"Nie ma stanowiska o nazwie „{answer}”.")
        return None

    def _loop_state_changed(self, running: bool) -> None:
        self._loop_running = running
        self._sync_all()

    def _loop_closed(self) -> None:
        self._loop_window = None
        self._loop_running = False
        self._sync_all()

    def _apply_new_config(self, new_config: AppConfig) -> None:
        self.config = new_config
        self.results.reconfigure(
            new_config.reports_path, new_config.backup_path, new_config.maximum_csv_rows
        )
        self.raw_writer.reconfigure(
            new_config.raw_path, new_config.raw_samples_enabled, new_config.raw_retention_days
        )
        self.attempts.reconfigure(new_config.attempts_path, new_config.attempts_retention_days)
        self.pending.reconfigure(new_config.pending_path)
        for panel in self.panels:
            panel.reconfigure(new_config)
        configure_logging(new_config, console=self.simulation_mode)
        self._settings_dialog = None
        if len(new_config.active_stations()) != len(self.panels):
            messagebox.showinfo(
                "Ustawienia",
                "Zmieniono liczbę stanowisk. Uruchom aplikację ponownie, aby zobaczyć nowy układ.",
            )
        self._sync_all()
        self._connect_all()

    # ------------------------------------------------------------ zamykanie

    def _close_app(self) -> None:
        busy = [panel.station.name for panel in self.panels if panel.busy]
        if busy and not messagebox.askyesno(
            "PSU19 Tester",
            f"Trwa test na stanowisku: {', '.join(busy)}. Zamknąć aplikację i anulować?",
        ):
            return
        if self._loop_running and not messagebox.askyesno(
            "PSU19 Tester", "Trwa seria walidacyjna. Przerwać ją i zamknąć aplikację?"
        ):
            return
        if self.pending.count and not messagebox.askyesno(
            "PSU19 Tester",
            f"Nie zapisano {self.pending.count} wyników.\n\n"
            "Są zabezpieczone na dysku i zostaną odtworzone po ponownym uruchomieniu, "
            "ale nie ma ich jeszcze w raporcie. Zamknąć mimo to?",
        ):
            return

        self.closing = True
        for panel in self.panels:
            panel.shutdown()
        if self._loop_window is not None and self._loop_window.exists():
            self._loop_window.cancel_event.set()

        def worker() -> None:
            for panel in self.panels:
                try:
                    panel.hardware.disconnect()
                except Exception:
                    logger.exception("[%s] Błąd zamykania sprzętu", panel.station.name)

        threading.Thread(target=worker, daemon=True, name="disconnect-all").start()
        self.root.after(1500, self._force_destroy)

    def _force_destroy(self) -> None:
        try:
            self.root.destroy()
        except tk.TclError:
            pass


def build_hardware(config: AppConfig, simulate: bool, mode: str, delay: float) -> list[Hardware]:
    """Tworzy po jednym kliencie na aktywne stanowisko."""
    devices: list[Hardware] = []
    for station in config.active_stations():
        if simulate:
            devices.append(SimulatedPM125(mode, command_delay=delay))
        else:
            devices.append(
                PM125Client(config.console_path, station.device_serial, config.command_timeout)
            )
    return devices
