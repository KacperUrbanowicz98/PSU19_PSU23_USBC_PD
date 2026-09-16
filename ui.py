"""Interfejs operatora (Tkinter). Sprzęt i testy działają poza wątkiem GUI."""
from __future__ import annotations

import logging
import queue
import threading
import time
import tkinter as tk
from collections import deque
from pathlib import Path
from tkinter import messagebox, simpledialog, ttk

from config import AppConfig
from engine import ProgressEvent, TestEngine
from hardware import Hardware, PM125Client
from logging_utils import configure_logging
from models import PHASE_LOAD, FinalStatus, ProfileStatus, TestResult
from settings_ui import SettingsDialog
from storage import (
    AttemptStore,
    CsvResultRepository,
    PendingRecord,
    PendingResultStore,
    RawSampleWriter,
    SaveOutcome,
    SaveState,
)
from theme import COLORS

logger = logging.getLogger(__name__)

#: Statusy, które zużywają próbę numeru seryjnego. Błąd stanowiska jej nie zużywa.
ATTEMPT_CONSUMING_STATUSES = {FinalStatus.PASS, FinalStatus.FAIL}


def friendly_error(exc: BaseException) -> str:
    """Komunikat dla operatora zamiast surowej treści wyjątku Pythona."""
    if isinstance(exc, ConnectionError):
        return "Brak połączenia z PM125. Sprawdź kabel USB i użyj przycisku „Połącz ponownie”."
    if isinstance(exc, TimeoutError):
        return "PM125 nie odpowiedział w wymaganym czasie. Powtórz test lub połącz ponownie."
    if isinstance(exc, PermissionError):
        return "Brak uprawnień do pliku na stanowisku. Zgłoś to do IT."
    if isinstance(exc, OSError):
        return "Błąd dostępu do pliku lub urządzenia na stanowisku. Zgłoś to do utrzymania ruchu."
    return "Błąd stanowiska. Powtórz test; jeśli się powtórzy, zgłoś utrzymaniu ruchu."


class TesterApp:
    def __init__(
        self,
        root: tk.Tk,
        config: AppConfig,
        hardware: Hardware,
        config_path: Path,
        simulation_mode: bool = False,
    ) -> None:
        self.root = root
        self.config = config
        self.config_path = config_path
        self.hardware = hardware
        self.simulation_mode = simulation_mode
        self.engine = TestEngine(config, hardware)
        self.results = CsvResultRepository(config.reports_path, config.backup_path, config.maximum_csv_rows)
        self.raw_writer = RawSampleWriter(
            config.raw_path, config.raw_samples_enabled, config.raw_retention_days
        )
        self.attempts = AttemptStore(config.attempts_path, config.attempts_retention_days)
        self.pending = PendingResultStore(config.pending_path)

        self.logged_hrid: str | None = None
        self.busy = False
        self.connecting = False
        self.closing = False
        self.cancel_event = threading.Event()
        self.ui_queue: queue.Queue[tuple] = queue.Queue()
        self.history: deque[tuple[str, str, str, float]] = deque(maxlen=3)
        self.session_pass = 0
        self.session_non_pass = 0
        self.last_serial = ""
        self._engineering_presses: list[float] = []
        self._progress_window: tk.Toplevel | None = None
        self._result_window: tk.Toplevel | None = None
        self._progress_started_at = 0.0
        self._settings_dialog: SettingsDialog | None = None

        self._configure_window()
        self._build_ui()
        self._sync_controls()
        self.root.after(100, self._poll_queue)
        self.root.after(250, self.connect_hardware)
        self._schedule_health_check()
        if self.pending.count:
            logger.warning("Odtworzono %s niezapisanych wyników z poprzedniej sesji.", self.pending.count)

    # ------------------------------------------------------------ szkielet

    def _configure_window(self) -> None:
        self.root.title("PSU USB-C Tester — Reconext")
        self.root.geometry("980x700")
        self.root.minsize(900, 660)
        self.root.configure(bg=COLORS["background"])
        self.root.protocol("WM_DELETE_WINDOW", self._close_app)
        self.root.bind("<Control-Shift-D>", self._engineering_key)

        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure(
            "Modern.TEntry",
            fieldbackground=COLORS["entry"],
            bordercolor=COLORS["border"],
            lightcolor=COLORS["border"],
            darkcolor=COLORS["border"],
            padding=8,
        )
        style.map(
            "Modern.TEntry",
            bordercolor=[("focus", COLORS["primary"])],
            lightcolor=[("focus", COLORS["primary"])],
            darkcolor=[("focus", COLORS["primary"])],
        )
        style.configure(
            "Session.Treeview",
            rowheight=31,
            font=("Segoe UI", 10),
            background=COLORS["card"],
            fieldbackground=COLORS["card"],
            foreground=COLORS["text"],
            borderwidth=0,
        )
        style.configure(
            "Session.Treeview.Heading",
            font=("Segoe UI", 9, "bold"),
            background=COLORS["primary_soft"],
            foreground=COLORS["primary"],
            relief=tk.FLAT,
            padding=(6, 7),
        )
        style.map("Session.Treeview", background=[("selected", COLORS["primary_soft"])])
        style.configure(
            "Result.Treeview",
            rowheight=34,
            font=("Segoe UI", 11, "bold"),
            background=COLORS["card"],
            fieldbackground=COLORS["card"],
            foreground=COLORS["text"],
            borderwidth=0,
        )
        style.configure(
            "Result.Treeview.Heading",
            font=("Segoe UI", 10, "bold"),
            background=COLORS["primary_soft"],
            foreground=COLORS["primary"],
            relief=tk.FLAT,
            padding=(8, 9),
        )
        style.configure(
            "Test.Horizontal.TProgressbar",
            troughcolor=COLORS["primary_soft"],
            background=COLORS["primary"],
            bordercolor=COLORS["primary_soft"],
            lightcolor=COLORS["primary"],
            darkcolor=COLORS["primary"],
        )
        style.configure("TNotebook.Tab", padding=(12, 7))

    def _build_ui(self) -> None:
        self._build_header()

        body = tk.Frame(self.root, bg=COLORS["background"])
        body.pack(fill=tk.BOTH, expand=True, padx=22, pady=(18, 12))

        top = tk.Frame(body, bg=COLORS["background"])
        top.pack(fill=tk.X)
        top.columnconfigure(0, weight=1)
        top.columnconfigure(1, weight=2)

        operator_card = self._card(top)
        operator_card.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        self._build_operator_card(operator_card)

        test_card = self._card(top)
        test_card.grid(row=0, column=1, sticky="nsew", padx=(8, 0))
        self._build_test_card(test_card)

        stats_row = tk.Frame(body, bg=COLORS["background"])
        stats_row.pack(fill=tk.X, pady=14)
        for column in range(3):
            stats_row.columnconfigure(column, weight=1)
        self.pass_value = self._stat_card(stats_row, 0, "PASS", COLORS["success"], COLORS["success_soft"])
        self.fail_value = self._stat_card(stats_row, 1, "FAIL / ERROR", COLORS["error"], COLORS["error_soft"])
        self.rate_value = self._stat_card(stats_row, 2, "PASS RATE", COLORS["primary"], COLORS["primary_soft"])

        self.pending_banner = tk.Frame(
            body,
            bg=COLORS["error_soft"],
            highlightbackground="#F1B7B7",
            highlightthickness=1,
        )
        self.pending_message = tk.Label(
            self.pending_banner,
            text="",
            bg=COLORS["error_soft"],
            fg=COLORS["error"],
            font=("Segoe UI", 10, "bold"),
            anchor="w",
        )
        self.pending_message.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=14, pady=10)
        self.pending_retry_button = self._button(
            self.pending_banner,
            "Ponów zapis",
            self.retry_pending_save,
            COLORS["error"],
            COLORS["error_hover"],
            font_size=9,
            padx=14,
            pady=7,
        )
        self.pending_retry_button.pack(side=tk.RIGHT, padx=10, pady=7)

        history_card = self._card(body)
        history_card.pack(fill=tk.X)
        self._build_history(history_card)

        footer = tk.Frame(self.root, bg=COLORS["background"], height=34)
        footer.pack(fill=tk.X)
        footer.pack_propagate(False)
        tk.Label(
            footer,
            text="Autor: Kacper Urbanowicz",
            bg=COLORS["background"],
            fg=COLORS["muted"],
            font=("Segoe UI", 9),
        ).pack(side=tk.LEFT, padx=22, pady=7)

        self._update_connection_indicator()
        self._update_operator_state()
        self._update_stats()
        self._sync_pending_banner()

    def _build_header(self) -> None:
        header = tk.Frame(self.root, bg=COLORS["primary"], height=82)
        header.pack(fill=tk.X)
        header.pack_propagate(False)

        title_area = tk.Frame(header, bg=COLORS["primary"])
        title_area.pack(side=tk.LEFT, fill=tk.Y, padx=24)
        title = "PSU USB-C Tester"
        if self.simulation_mode:
            title += "  ·  SIMULATOR"
        tk.Label(
            title_area,
            text=title,
            bg=COLORS["primary"],
            fg="white",
            font=("Segoe UI", 23, "bold"),
            anchor="w",
        ).pack(anchor="w", pady=(13, 0))
        tk.Label(
            title_area,
            text="Automatyczny test zasilaczy USB Power Delivery",
            bg=COLORS["primary"],
            fg="#DDEAF4",
            font=("Segoe UI", 9),
            anchor="w",
        ).pack(anchor="w", pady=(0, 10))

        controls = tk.Frame(header, bg=COLORS["primary"])
        controls.pack(side=tk.RIGHT, padx=20, pady=20)

        self.connection_label = tk.Label(
            controls,
            text="",
            bg=COLORS["error"],
            fg="white",
            font=("Segoe UI", 9, "bold"),
            padx=12,
            pady=7,
        )
        self.connection_label.pack(side=tk.LEFT, padx=(0, 8))

        self.reconnect_button = self._button(
            controls,
            "Połącz ponownie",
            self.connect_hardware,
            "#FFFFFF",
            "#EAF2F8",
            foreground=COLORS["primary"],
            active_foreground=COLORS["primary"],
            font_size=9,
            padx=12,
            pady=7,
        )
        self.reconnect_button.pack(side=tk.LEFT)

    def _build_operator_card(self, card: tk.Frame) -> None:
        self._card_title(card, "Operator", "Zaloguj osobę wykonująca test")

        content = tk.Frame(card, bg=COLORS["card"])
        content.pack(fill=tk.BOTH, expand=True, padx=16, pady=(4, 16))

        self.operator_state_label = tk.Label(
            content,
            text="",
            bg=COLORS["primary_soft"],
            fg=COLORS["primary"],
            font=("Segoe UI", 10, "bold"),
            anchor="w",
            padx=10,
            pady=7,
        )
        self.operator_state_label.pack(fill=tk.X, pady=(0, 10))

        # Etykieta pola - operator widzi wprost, ze wpisuje numer HRID.
        tk.Label(
            content,
            text="Numer HRID",
            bg=COLORS["card"],
            fg=COLORS["text"],
            font=("Segoe UI", 9, "bold"),
            anchor="w",
        ).pack(fill=tk.X, pady=(0, 3))

        self.hrid_entry = ttk.Entry(
            content,
            style="Modern.TEntry",
            font=("Segoe UI", 12),
            justify="center",
        )
        self.hrid_entry.pack(fill=tk.X)
        self.hrid_entry.bind("<Return>", lambda _event: self.login())

        tk.Label(
            content,
            text="Wpisz lub zeskanuj swój numer HRID i naciśnij Enter.",
            bg=COLORS["card"],
            fg=COLORS["muted"],
            font=("Segoe UI", 8),
            anchor="w",
        ).pack(fill=tk.X, pady=(3, 0))

        button_row = tk.Frame(content, bg=COLORS["card"])
        button_row.pack(fill=tk.X, pady=(10, 0))
        self.login_button = self._button(
            button_row, "Zaloguj", self.login, COLORS["primary"], COLORS["primary_hover"]
        )
        self.login_button.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 5))
        self.logout_button = self._button(
            button_row, "Wyloguj", self.logout, COLORS["error"], COLORS["error_hover"]
        )
        self.logout_button.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(5, 0))

    def _build_test_card(self, card: tk.Frame) -> None:
        self._card_title(card, "Test zasilacza", "Zeskanuj lub wpisz numer seryjny")

        content = tk.Frame(card, bg=COLORS["card"])
        content.pack(fill=tk.BOTH, expand=True, padx=16, pady=(4, 16))
        content.columnconfigure(0, weight=1)

        tk.Label(
            content,
            text="Numer seryjny zasilacza",
            bg=COLORS["card"],
            fg=COLORS["text"],
            font=("Segoe UI", 9, "bold"),
            anchor="w",
        ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 3))

        self.serial_entry = ttk.Entry(
            content,
            style="Modern.TEntry",
            font=("Segoe UI", 17, "bold"),
            justify="center",
        )
        self.serial_entry.grid(row=1, column=0, sticky="ew", padx=(0, 10))
        self.serial_entry.bind("<Return>", lambda _event: self.start_test())

        self.start_button = self._button(
            content,
            "ROZPOCZNIJ TEST",
            self.start_test,
            COLORS["success"],
            COLORS["success_hover"],
            font_size=10,
            padx=22,
            pady=10,
        )
        self.start_button.grid(row=1, column=1, sticky="ns")

        self.blocker_label = tk.Label(
            content,
            text="",
            bg=COLORS["card"],
            fg=COLORS["muted"],
            font=("Segoe UI", 9),
            anchor="w",
            justify=tk.LEFT,
        )
        self.blocker_label.grid(row=2, column=0, columnspan=2, sticky="w", pady=(9, 0))

    def _build_history(self, card: tk.Frame) -> None:
        header = tk.Frame(card, bg=COLORS["card"])
        header.pack(fill=tk.X, padx=16, pady=(12, 7))
        tk.Label(
            header,
            text="Ostatnie testy tej sesji",
            bg=COLORS["card"],
            fg=COLORS["text"],
            font=("Segoe UI", 11, "bold"),
            anchor="w",
        ).pack(side=tk.LEFT)
        tk.Label(
            header,
            text="maks. 3 pozycje",
            bg=COLORS["card"],
            fg=COLORS["muted"],
            font=("Segoe UI", 8),
        ).pack(side=tk.RIGHT)

        columns = ("date", "serial", "result", "duration")
        self.history_tree = ttk.Treeview(
            card,
            columns=columns,
            show="headings",
            height=3,
            style="Session.Treeview",
            selectmode="none",
        )
        self.history_tree.heading("date", text="Data")
        self.history_tree.heading("serial", text="Numer seryjny")
        self.history_tree.heading("result", text="Wynik")
        self.history_tree.heading("duration", text="Czas")
        self.history_tree.column("date", width=155, anchor=tk.CENTER, stretch=False)
        self.history_tree.column("serial", width=450, anchor=tk.W)
        self.history_tree.column("result", width=130, anchor=tk.CENTER, stretch=False)
        self.history_tree.column("duration", width=100, anchor=tk.CENTER, stretch=False)
        self.history_tree.tag_configure("pass", foreground=COLORS["success"], font=("Segoe UI", 10, "bold"))
        self.history_tree.tag_configure("nonpass", foreground=COLORS["error"], font=("Segoe UI", 10, "bold"))
        self.history_tree.pack(fill=tk.X, padx=16, pady=(0, 14))

    # ------------------------------------------------------------ elementy

    def _card(self, parent: tk.Widget) -> tk.Frame:
        return tk.Frame(
            parent, bg=COLORS["card"], highlightbackground=COLORS["border"], highlightthickness=1
        )

    def _card_title(self, parent: tk.Widget, title: str, subtitle: str) -> None:
        area = tk.Frame(parent, bg=COLORS["card"])
        area.pack(fill=tk.X, padx=16, pady=(14, 6))
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
            parent,
            text=text,
            command=command,
            bg=color,
            fg=foreground,
            activebackground=hover_color,
            activeforeground=active_foreground,
            disabledforeground="#AAB4BD",
            relief=tk.FLAT,
            borderwidth=0,
            font=("Segoe UI", font_size, "bold"),
            padx=padx,
            pady=pady,
            cursor="hand2",
        )
        button.bind("<Enter>", lambda _e: button.config(bg=hover_color) if button["state"] == tk.NORMAL else None)
        button.bind("<Leave>", lambda _e: button.config(bg=color) if button["state"] == tk.NORMAL else None)
        return button

    def _stat_card(self, parent: tk.Widget, column: int, title: str, color: str, soft_color: str) -> tk.Label:
        card = tk.Frame(
            parent, bg=COLORS["card"], highlightbackground=COLORS["border"], highlightthickness=1
        )
        card.grid(row=0, column=column, sticky="ew", padx=(0 if column == 0 else 7, 0 if column == 2 else 7))
        tk.Frame(card, bg=color, width=5).pack(side=tk.LEFT, fill=tk.Y)
        content = tk.Frame(card, bg=COLORS["card"])
        content.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=14, pady=9)
        tk.Label(
            content, text=title, bg=COLORS["card"], fg=COLORS["muted"],
            font=("Segoe UI", 8, "bold"), anchor="w",
        ).pack(anchor="w")
        value = tk.Label(
            content, text="0", bg=COLORS["card"], fg=color, font=("Segoe UI", 24, "bold"), anchor="w"
        )
        value.pack(anchor="w", pady=(0, 1))
        return value

    # --------------------------------------------------------------- stany

    def _update_operator_state(self) -> None:
        if self.logged_hrid:
            self.operator_state_label.config(
                text=f"Zalogowano: {self.logged_hrid}",
                bg=COLORS["success_soft"],
                fg=COLORS["success"],
            )
        else:
            self.operator_state_label.config(
                text="Operator niezalogowany",
                bg=COLORS["primary_soft"],
                fg=COLORS["primary"],
            )

    def _update_connection_indicator(self) -> None:
        if self.connecting:
            text, color = "PM125: ŁĄCZENIE...", COLORS["warning"]
        elif self.hardware.connected:
            text, color = "PM125: POŁĄCZONY", COLORS["success"]
        else:
            text, color = "PM125: NIEPOŁĄCZONY", COLORS["error"]
        self.connection_label.config(text=text, bg=color)

    def _blocker_text(self) -> str:
        if self.pending.count:
            return "Test zablokowany: niezapisany wynik. Użyj „Ponów zapis”."
        if self.busy:
            return "Test w toku..."
        if not self.logged_hrid:
            return "Zaloguj operatora numerem HRID, aby odblokować test."
        if not self.hardware.connected:
            return "PM125 niepołączony. Sprawdź kabel USB i użyj „Połącz ponownie”."
        if self._result_window is not None:
            return "Zamknij okno wyniku, aby zeskanować kolejny zasilacz."
        return "Gotowe. Zeskanuj numer seryjny zasilacza."

    def _sync_controls(self) -> None:
        logged = self.logged_hrid is not None
        can_start = (
            logged
            and self.hardware.connected
            and not self.busy
            and self.pending.count == 0
            and self._result_window is None
        )
        self.hrid_entry.config(state=tk.DISABLED if logged or self.busy else tk.NORMAL)
        self.login_button.config(state=tk.DISABLED if logged or self.busy else tk.NORMAL)
        self.logout_button.config(state=tk.NORMAL if logged and not self.busy else tk.DISABLED)
        self.serial_entry.config(state=tk.NORMAL if can_start else tk.DISABLED)
        self.start_button.config(state=tk.NORMAL if can_start else tk.DISABLED)
        self.reconnect_button.config(state=tk.DISABLED if self.connecting or self.busy else tk.NORMAL)
        self.blocker_label.config(text=self._blocker_text())
        if hasattr(self, "pending_retry_button"):
            self.pending_retry_button.config(
                state=tk.NORMAL if self.pending.count and not self.busy else tk.DISABLED
            )

    # ------------------------------------------------------------- sprzęt

    def connect_hardware(self) -> None:
        if self.connecting or self.busy:
            return
        self.connecting = True
        self._update_connection_indicator()
        self._sync_controls()

        def worker() -> None:
            try:
                connected = self.hardware.connect()
                self.ui_queue.put(("connection", connected, self.hardware.last_error))
            except Exception as exc:
                logger.exception("Błąd łączenia z PM125")
                self.ui_queue.put(("connection", False, str(exc)))

        threading.Thread(target=worker, daemon=True, name="pm125-connect").start()

    def _schedule_health_check(self) -> None:
        interval = self.config.health_check_seconds
        if interval <= 0:
            return
        self.root.after(int(interval * 1000), self._health_check)

    def _health_check(self) -> None:
        """Kontrola obecności PM125 między testami - odpięcie USB widać od razu."""
        if self.closing:
            return
        if not (self.busy or self.connecting) and self.hardware.connected:
            def worker() -> None:
                try:
                    alive = self.hardware.ping()
                except Exception:
                    alive = False
                self.ui_queue.put(("health", alive, self.hardware.last_error))

            threading.Thread(target=worker, daemon=True, name="pm125-health").start()
        self._schedule_health_check()

    # ------------------------------------------------------------ operator

    def login(self) -> None:
        hrid = self.hrid_entry.get().strip()
        if hrid not in self.config.valid_hrids:
            messagebox.showwarning("HRID", "Nieprawidłowy HRID.")
            return
        self.logged_hrid = hrid
        self.hrid_entry.delete(0, tk.END)
        self.serial_entry.focus_set()
        logger.info("Operator zalogowany: %s", hrid)
        self._update_operator_state()
        self._sync_controls()

    def logout(self) -> None:
        if self.busy:
            return
        logger.info("Operator wylogowany: %s", self.logged_hrid)
        self.logged_hrid = None
        self.serial_entry.config(state=tk.NORMAL)
        self.serial_entry.delete(0, tk.END)
        self.hrid_entry.focus_set()
        self._update_operator_state()
        self._sync_controls()

    # ---------------------------------------------------------------- test

    def start_test(self, serial_override: str | None = None) -> None:
        if self.busy or self.pending.count or self._result_window is not None:
            return
        if not self.logged_hrid:
            messagebox.showwarning("HRID", "Najpierw zaloguj operatora.")
            return
        if not self.hardware.connected:
            messagebox.showwarning("PM125", "Podłącz PM125 i użyj przycisku „Połącz ponownie”.")
            return

        serial = (serial_override or self.serial_entry.get()).strip().upper()
        minimum = self.config.serial_minimum_length
        if len(serial) < minimum:
            messagebox.showwarning(
                "Numer seryjny", f"Numer seryjny musi mieć co najmniej {minimum} znaków."
            )
            return

        count = self.attempts.count(serial)
        limit = self.config.maximum_serial_attempts
        if count >= limit:
            messagebox.showerror(
                "Numer seryjny",
                f"Ten numer seryjny osiągnął limit prób: {count}/{limit}.\n\n"
                "Zgłoś zasilacz przełożonemu. Licznik można wyzerować w panelu inżynieryjnym.",
            )
            return
        if count and not messagebox.askyesno(
            "Numer seryjny",
            f"Ten numer seryjny był już testowany {count} raz(y) z {limit}. Kontynuować?",
        ):
            return

        self.busy = True
        self.last_serial = serial
        self.cancel_event = threading.Event()
        self._show_progress_window(serial)
        self._sync_controls()
        hrid = self.logged_hrid

        def worker() -> None:
            try:
                detected, voltage, message = self.engine.detect_dut()
                if not detected:
                    self.ui_queue.put(("no_dut", message, voltage))
                    return
                result = self.engine.run(
                    hrid=hrid,
                    serial_number=serial,
                    progress=lambda event: self.ui_queue.put(("progress", event)),
                    cancel_event=self.cancel_event,
                )
                raw_path = self.raw_writer.write(result)
                if raw_path:
                    result.raw_file = raw_path.name
                outcome = self.results.save(result)
                self.ui_queue.put(("test_done", result, outcome))
            except Exception as exc:
                logger.exception("Błąd uruchomienia testu")
                self.ui_queue.put(("test_error", friendly_error(exc), str(exc)))

        threading.Thread(target=worker, daemon=True, name=f"test-{serial}").start()

    # ----------------------------------------------------- okno przebiegu

    def _show_progress_window(self, serial: str) -> None:
        window = tk.Toplevel(self.root)
        window.title("Test w trakcie")
        window.geometry("720x470")
        window.resizable(False, False)
        window.transient(self.root)
        window.protocol("WM_DELETE_WINDOW", lambda: None)
        window.configure(bg=COLORS["background"])
        self._progress_window = window
        self._progress_started_at = time.monotonic()
        self._center_window(window, 720, 470)

        header = tk.Frame(window, bg=COLORS["primary"], height=96)
        header.pack(fill=tk.X)
        header.pack_propagate(False)
        tk.Label(
            header, text="TEST W TRAKCIE", bg=COLORS["primary"], fg="white",
            font=("Segoe UI", 27, "bold"),
        ).pack(pady=(10, 0))
        tk.Label(
            header, text=f"Numer seryjny: {serial}", bg=COLORS["primary"], fg="#DDEAF4",
            font=("Segoe UI", 12, "bold"),
        ).pack(pady=(1, 12))

        content = tk.Frame(window, bg=COLORS["background"])
        content.pack(fill=tk.BOTH, expand=True, padx=22, pady=16)

        self.progress_status = tk.Label(
            content, text="Wykrywanie zasilacza...", bg=COLORS["background"], fg=COLORS["text"],
            font=("Segoe UI", 19, "bold"),
        )
        self.progress_status.pack(pady=(0, 3))

        phase_row = tk.Frame(content, bg=COLORS["background"])
        phase_row.pack(pady=(0, 12))
        self.progress_step = tk.Label(
            phase_row, text="", bg=COLORS["background"], fg=COLORS["muted"],
            font=("Segoe UI", 11, "bold"),
        )
        self.progress_step.pack(side=tk.LEFT, padx=(0, 10))
        self.progress_phase = tk.Label(
            phase_row, text="", bg=COLORS["primary_soft"], fg=COLORS["primary"],
            font=("Segoe UI", 11, "bold"), padx=12, pady=5,
        )
        self.progress_phase.pack(side=tk.LEFT)

        measurements = tk.Frame(content, bg=COLORS["background"])
        measurements.pack(fill=tk.X)
        measurements.columnconfigure(0, weight=1)
        measurements.columnconfigure(1, weight=1)
        self.progress_voltage = self._measurement_card(measurements, 0, "NAPIĘCIE")
        self.progress_current = self._measurement_card(measurements, 1, "PRĄD")

        status_row = tk.Frame(content, bg=COLORS["background"])
        status_row.pack(fill=tk.X, pady=(14, 7))
        self.progress_measurement_status = tk.Label(
            status_row, text="OCZEKIWANIE NA POMIAR", bg=COLORS["primary_soft"], fg=COLORS["primary"],
            font=("Segoe UI", 11, "bold"), padx=12, pady=6,
        )
        self.progress_measurement_status.pack(side=tk.LEFT)
        self.progress_elapsed = tk.Label(
            status_row, text="Czas: 00:00", bg=COLORS["background"], fg=COLORS["muted"],
            font=("Segoe UI", 11, "bold"),
        )
        self.progress_elapsed.pack(side=tk.RIGHT)

        self.progress_bar = ttk.Progressbar(
            content, mode="determinate", maximum=100, length=620, style="Test.Horizontal.TProgressbar"
        )
        self.progress_bar.pack(fill=tk.X, pady=(3, 12))

        self.cancel_button = self._button(
            content, "ANULUJ TEST", self._cancel_test, COLORS["error"], COLORS["error_hover"],
            font_size=10, padx=24, pady=8,
        )
        self.cancel_button.pack()
        self._update_progress_timer()

    def _measurement_card(self, parent: tk.Widget, column: int, title: str) -> tk.Label:
        card = tk.Frame(
            parent, bg=COLORS["card"], highlightbackground=COLORS["border"], highlightthickness=1
        )
        card.grid(row=0, column=column, sticky="ew", padx=(0, 7) if column == 0 else (7, 0))
        tk.Label(
            card, text=title, bg=COLORS["card"], fg=COLORS["muted"], font=("Segoe UI", 10, "bold")
        ).pack(pady=(12, 1))
        value = tk.Label(
            card, text="—", bg=COLORS["card"], fg=COLORS["primary"], font=("Segoe UI", 31, "bold")
        )
        value.pack(pady=(0, 13))
        return value

    def _update_progress_timer(self) -> None:
        if not self._progress_window or not self._progress_window.winfo_exists():
            return
        elapsed = max(0, int(time.monotonic() - self._progress_started_at))
        minutes, seconds = divmod(elapsed, 60)
        self.progress_elapsed.config(text=f"Czas: {minutes:02d}:{seconds:02d}")
        self._progress_window.after(500, self._update_progress_timer)

    def _cancel_test(self) -> None:
        self.cancel_event.set()
        if hasattr(self, "cancel_button"):
            self.cancel_button.config(state=tk.DISABLED, text="ANULOWANIE...")

    # ------------------------------------------------------------ kolejka

    def _poll_queue(self) -> None:
        try:
            while True:
                event = self.ui_queue.get_nowait()
                kind = event[0]
                try:
                    if kind == "connection":
                        self._handle_connection(event[1], event[2])
                    elif kind == "health":
                        self._handle_health(event[1], event[2])
                    elif kind == "progress":
                        self._handle_progress(event[1])
                    elif kind == "test_done":
                        self._handle_test_done(event[1], event[2])
                    elif kind == "test_error":
                        self._handle_test_error(event[1], event[2])
                    elif kind == "no_dut":
                        self._handle_no_dut(event[1], event[2])
                    elif kind == "save_done":
                        self._handle_save_done(event[1], event[2])
                except Exception:
                    logger.exception("Błąd obsługi zdarzenia %s", kind)
        except queue.Empty:
            pass
        finally:
            if not self.closing:
                self.root.after(100, self._poll_queue)

    def _handle_connection(self, connected: bool, error: str) -> None:
        self.connecting = False
        self._update_connection_indicator()
        self._sync_controls()
        if not connected and error:
            logger.warning("Brak połączenia: %s", error)

    def _handle_health(self, alive: bool, error: str) -> None:
        if not alive:
            logger.warning("Kontrola obecności PM125 nieudana: %s", error)
        self._update_connection_indicator()
        self._sync_controls()

    def _handle_progress(self, event: ProgressEvent) -> None:
        if not self._progress_window or not self._progress_window.winfo_exists():
            return
        phase = "Z OBCIĄŻENIEM" if event.phase == PHASE_LOAD else "BEZ OBCIĄŻENIA"
        if event.settling:
            color, soft, status = COLORS["warning"], COLORS["warning_soft"], "STABILIZACJA"
        elif event.measurement_ok:
            color, soft, status = COLORS["success"], COLORS["success_soft"], "POMIAR OK"
        else:
            color, soft, status = COLORS["error"], COLORS["error_soft"], "POZA ZAKRESEM"
        self.progress_status.config(text=event.profile_name)
        self.progress_step.config(text=f"Profil {event.profile_number} z {event.profile_total}")
        self.progress_phase.config(text=phase)
        self.progress_voltage.config(text=f"{event.voltage:.3f} V", fg=color)
        self.progress_current.config(text=f"{event.current:.3f} A", fg=color)
        self.progress_measurement_status.config(text=status, bg=soft, fg=color)
        self.progress_bar["value"] = max(0.0, min(100.0, event.overall_fraction * 100.0))

    def _handle_test_done(self, result: TestResult, outcome: SaveOutcome) -> None:
        self._destroy_progress_window()
        self.busy = False

        # Próbę zużywa tylko test zakończony ocena wyrobu (PASS/FAIL).
        if result.final_status in ATTEMPT_CONSUMING_STATUSES:
            try:
                self.attempts.record(result.serial_number)
            except OSError as exc:
                logger.error("Nie zapisano licznika prób: %s", exc)
                messagebox.showwarning(
                    "Licznik prób",
                    "Wynik testu został zapisany, ale nie udało się zaktualizować licznika prób.\n"
                    "Zgłoś to utrzymaniu ruchu.",
                )

        if result.final_status is FinalStatus.PASS:
            self.session_pass += 1
        else:
            self.session_non_pass += 1
        self.history.appendleft(
            (result.timestamp, result.serial_number, result.final_status.value, result.duration_seconds)
        )
        if not outcome.saved:
            self.pending.add(PendingRecord.from_result(result))
        self._update_stats()
        self._refresh_history()
        self._sync_pending_banner()
        self._update_connection_indicator()
        self._show_result_window(result, outcome)
        self._sync_controls()

    def _handle_test_error(self, message: str, detail: str) -> None:
        self._destroy_progress_window()
        self.busy = False
        self._update_connection_indicator()
        self._sync_controls()
        logger.error("Test zakończony błędem: %s", detail)
        messagebox.showerror("Test", f"{message}\n\nSzczegoly zapisano w logu aplikacji.")
        self._prepare_for_next_serial()

    def _handle_no_dut(self, message: str, voltage: float) -> None:
        """Brak zasilacza to nie wynik testu: bez zapisu, bez FAIL, bez zużycia próby."""
        self._destroy_progress_window()
        self.busy = False
        self._update_connection_indicator()
        self._sync_controls()
        logger.info("Pre-kontrola: %s", message)
        messagebox.showwarning(
            "Brak zasilacza",
            f"{message}\n\nPodlacz zasilacz do stanowiska i zeskanuj numer ponownie.\n"
            "Ta próba nie została policzona.",
        )
        self._prepare_for_next_serial()

    def _destroy_progress_window(self) -> None:
        if self._progress_window and self._progress_window.winfo_exists():
            self._progress_window.destroy()
        self._progress_window = None

    # -------------------------------------------------------- okno wyniku

    def _show_result_window(self, result: TestResult, outcome: SaveOutcome) -> None:
        if self._result_window and self._result_window.winfo_exists():
            self._result_window.destroy()

        window = tk.Toplevel(self.root)
        self._result_window = window
        window.title("Test zakończony")
        window.geometry("820x560")
        window.resizable(False, False)
        window.transient(self.root)
        window.configure(bg=COLORS["background"])
        self._center_window(window, 820, 560)

        is_pass = result.final_status is FinalStatus.PASS
        color = COLORS["success"] if is_pass else COLORS["error"]
        soft = COLORS["success_soft"] if is_pass else COLORS["error_soft"]

        header = tk.Frame(window, bg=color, height=104)
        header.pack(fill=tk.X)
        header.pack_propagate(False)
        tk.Label(
            header, text=result.final_status.value, bg=color, fg="white", font=("Segoe UI", 34, "bold")
        ).pack(pady=(10, 0))
        tk.Label(
            header, text=f"Numer seryjny: {result.serial_number}", bg=color, fg="white",
            font=("Segoe UI", 14, "bold"),
        ).pack(pady=(0, 10))

        summary = tk.Frame(window, bg=soft)
        summary.pack(fill=tk.X)
        tk.Label(
            summary,
            text=f"Czas testu: {result.duration_seconds:.2f} s   ·   "
                 f"próbki: {result.sample_count} ({result.sampling_rate:.1f} Hz)",
            bg=soft, fg=color, font=("Segoe UI", 12, "bold"), pady=9,
        ).pack()

        columns = ("profile", "status", "u_no_load", "u_load", "i_load")
        tree = ttk.Treeview(
            window, columns=columns, show="headings",
            height=min(max(len(result.profiles), 4), 7),
            style="Result.Treeview", selectmode="none",
        )
        headings = ("Profil", "Wynik", "U bez obc. [V]", "U pod obc. [V]", "I pod obc. [A]")
        widths = (110, 130, 200, 200, 140)
        for column, heading, width in zip(columns, headings, widths):
            tree.heading(column, text=heading)
            tree.column(column, width=width, anchor=tk.CENTER)
        tree.tag_configure("pass", foreground=COLORS["success"])
        tree.tag_configure("fail", foreground=COLORS["error"])
        tree.tag_configure("other", foreground=COLORS["warning"])

        for profile_result in result.profiles:
            if profile_result.status is ProfileStatus.PASS:
                tag = "pass"
            elif profile_result.status is ProfileStatus.FAIL:
                tag = "fail"
            else:
                tag = "other"
            tree.insert(
                "", tk.END,
                values=(
                    profile_result.profile.name,
                    profile_result.status.value,
                    _range(profile_result.min_no_load_voltage, profile_result.max_no_load_voltage),
                    _range(profile_result.min_loaded_voltage, profile_result.max_loaded_voltage),
                    _display(profile_result.average_loaded_current, digits=3),
                ),
                tags=(tag,),
            )
        tree.pack(fill=tk.X, padx=18, pady=(16, 8))

        message_area = tk.Frame(window, bg=COLORS["background"])
        message_area.pack(fill=tk.X, padx=18)
        violation = result.first_violation
        if violation:
            tk.Label(
                message_area,
                text=f"Przyczyna: {violation}",
                bg=COLORS["error_soft"], fg=COLORS["error"], font=("Segoe UI", 11, "bold"),
                wraplength=800, justify=tk.LEFT, padx=10, pady=7, anchor="w",
            ).pack(fill=tk.X, pady=(0, 5))
        elif result.message:
            tk.Label(
                message_area,
                text=result.message,
                bg=COLORS["warning_soft"], fg=COLORS["warning"], font=("Segoe UI", 10, "bold"),
                wraplength=800, justify=tk.LEFT, padx=10, pady=7, anchor="w",
            ).pack(fill=tk.X, pady=(0, 5))

        if outcome.state is SaveState.BACKUP:
            tk.Label(
                message_area, text=f"Wynik zapisano awaryjnie: {outcome.path}",
                bg=COLORS["warning_soft"], fg=COLORS["warning"], font=("Segoe UI", 9, "bold"),
                wraplength=800, padx=10, pady=6,
            ).pack(fill=tk.X, pady=(2, 4))
        elif outcome.state is SaveState.FAILED:
            tk.Label(
                message_area,
                text="Nie udało się zapisać wyniku. Wynik jest zabezpieczony na dysku — "
                     "użyj „Ponów zapis” w głównym oknie.",
                bg=COLORS["error_soft"], fg=COLORS["error"], font=("Segoe UI", 9, "bold"),
                wraplength=800, padx=10, pady=6,
            ).pack(fill=tk.X, pady=(2, 4))

        actions = tk.Frame(window, bg=COLORS["background"])
        actions.pack(side=tk.BOTTOM, fill=tk.X, padx=18, pady=(0, 14))

        countdown = tk.Label(
            actions, text="", bg=COLORS["background"], fg=COLORS["muted"], font=("Segoe UI", 9)
        )
        countdown.pack(side=tk.LEFT)

        state = {"closed": False}

        def close_result_window(then_retest: bool = False) -> None:
            if state["closed"]:
                return
            state["closed"] = True
            serial = result.serial_number
            if window.winfo_exists():
                window.destroy()
            if self._result_window is window:
                self._result_window = None
            self._prepare_for_next_serial()
            if then_retest:
                self.root.after(120, lambda: self.start_test(serial_override=serial))

        close_button = self._button(
            actions, "ZAMKNIJ", close_result_window, COLORS["primary"], COLORS["primary_hover"],
            font_size=10, padx=24, pady=9,
        )
        close_button.pack(side=tk.RIGHT)

        remaining_attempts = max(
            0, self.config.maximum_serial_attempts - self.attempts.count(result.serial_number)
        )
        if remaining_attempts > 0:
            retest_button = self._button(
                actions, f"POWTÓRZ TEST ({remaining_attempts})",
                lambda: close_result_window(then_retest=True),
                COLORS["warning"], COLORS["warning_hover"], font_size=10, padx=20, pady=9,
            )
            retest_button.pack(side=tk.RIGHT, padx=(0, 10))

        window.protocol("WM_DELETE_WINDOW", close_result_window)

        seconds = int(
            self.config.result_window_pass_seconds if is_pass else self.config.result_window_fail_seconds
        )

        def update_countdown(left: int) -> None:
            if state["closed"] or not window.winfo_exists():
                return
            if left <= 0:
                close_result_window()
                return
            countdown.config(text=f"Okno zamknie się automatycznie za {left} s")
            window.after(1000, update_countdown, left - 1)

        update_countdown(max(1, seconds))

    def _prepare_for_next_serial(self) -> None:
        """Czyści pole SN i przygotowuje je pod kolejny skan."""
        self.serial_entry.config(state=tk.NORMAL)
        self.serial_entry.delete(0, tk.END)
        self._sync_controls()

        can_scan = (
            self.logged_hrid is not None
            and self.hardware.connected
            and not self.busy
            and self.pending.count == 0
            and self._result_window is None
        )
        if can_scan:
            self.root.after_idle(self.serial_entry.focus_set)
            self.root.after(50, self.serial_entry.focus_force)

    # ------------------------------------------------- wyniki oczekujace

    def retry_pending_save(self) -> None:
        if self.busy or not self.pending.count:
            return
        self.busy = True
        self._sync_controls()
        self._sync_pending_banner()
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
            self.ui_queue.put(("save_done", saved, failure))

        threading.Thread(target=worker, daemon=True, name="retry-save").start()

    def _handle_save_done(self, saved: list[PendingRecord], failure: str) -> None:
        self.busy = False
        for record in saved:
            self.pending.remove(record)
        if failure:
            messagebox.showerror(
                "CSV",
                f"Zapisano {len(saved)} z {len(saved) + self.pending.count} wyników.\n\n{failure}",
            )
        else:
            messagebox.showinfo("CSV", f"Zapisano zaległe wyniki: {len(saved)}.")
        self._sync_pending_banner()
        self._sync_controls()

    def _sync_pending_banner(self) -> None:
        count = self.pending.count
        if count:
            self.pending_message.config(
                text=f"Niezapisane wyniki: {count}. Kolejny test jest zablokowany. "
                     "Wyniki są zabezpieczone na dysku i przetrwają restart aplikacji."
            )
            if not self.pending_banner.winfo_manager():
                self.pending_banner.pack(fill=tk.X, pady=(0, 14), before=self.history_tree.master)
            self.pending_retry_button.config(state=tk.DISABLED if self.busy else tk.NORMAL)
        elif self.pending_banner.winfo_manager():
            self.pending_banner.pack_forget()

    # ------------------------------------------------------------ podsumy

    def _update_stats(self) -> None:
        total = self.session_pass + self.session_non_pass
        rate = self.session_pass / total * 100 if total else 0.0
        self.pass_value.config(text=str(self.session_pass))
        self.fail_value.config(text=str(self.session_non_pass))
        self.rate_value.config(text=f"{rate:.1f}%")

    def _refresh_history(self) -> None:
        for item in self.history_tree.get_children():
            self.history_tree.delete(item)
        for timestamp, serial, status, duration in self.history:
            tag = "pass" if status == FinalStatus.PASS.value else "nonpass"
            self.history_tree.insert(
                "", tk.END, values=(timestamp, serial, status, f"{duration:.1f} s"), tags=(tag,)
            )

    # ------------------------------------------------- panel inżynieryjny

    def _engineering_key(self, _event=None) -> None:
        if self.busy:
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
            logger.info("Wejście do panelu inżynieryjnego (operator=%s).", self.logged_hrid or "-")
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
            is_busy=lambda: self.busy,
            operator_hrid=self.logged_hrid,
        )

    def _apply_new_config(self, new_config: AppConfig) -> None:
        self.config = new_config
        self.engine.config = new_config
        self.results.reconfigure(
            new_config.reports_path, new_config.backup_path, new_config.maximum_csv_rows
        )
        self.raw_writer.reconfigure(
            new_config.raw_path, new_config.raw_samples_enabled, new_config.raw_retention_days
        )
        self.attempts.reconfigure(new_config.attempts_path, new_config.attempts_retention_days)
        self.pending.reconfigure(new_config.pending_path)
        if isinstance(self.hardware, PM125Client):
            self.hardware.reconfigure(
                new_config.console_path, new_config.device_serial, new_config.command_timeout
            )
        configure_logging(new_config, console=self.simulation_mode)
        self._settings_dialog = None
        self._sync_pending_banner()
        self.connect_hardware()

    # -------------------------------------------------------- zamykanie

    def _close_app(self) -> None:
        if self.busy and not messagebox.askyesno(
            "PSU19 Tester", "Test trwa. Zamknąć aplikację i anulować test?"
        ):
            return
        if self.pending.count and not messagebox.askyesno(
            "PSU19 Tester",
            f"Nie zapisano {self.pending.count} wyników.\n\n"
            "Sa zabezpieczone na dysku i zostaną odtworzone po ponownym uruchomieniu, "
            "ale nie ma ich jeszcze w raporcie. Zamknąć mimo to?",
        ):
            return

        self.closing = True
        self.cancel_event.set()

        # Rozlaczenie sprzętu potrafi trwac do kilku sekund - nie blokujemy nim GUI.
        def worker() -> None:
            try:
                self.hardware.disconnect()
            except Exception:
                logger.exception("Błąd zamykania sprzętu")

        threading.Thread(target=worker, daemon=True, name="pm125-disconnect").start()
        self.root.after(1200, self._force_destroy)

    def _force_destroy(self) -> None:
        try:
            self.root.destroy()
        except tk.TclError:
            pass

    def _center_window(self, window: tk.Toplevel, width: int, height: int) -> None:
        self.root.update_idletasks()
        root_x = self.root.winfo_rootx()
        root_y = self.root.winfo_rooty()
        root_width = max(self.root.winfo_width(), width)
        root_height = max(self.root.winfo_height(), height)
        x = root_x + max(0, (root_width - width) // 2)
        y = root_y + max(0, (root_height - height) // 2)
        window.geometry(f"{width}x{height}+{x}+{y}")


def _display(value: float | None, digits: int = 2) -> str:
    return "N/A" if value is None else f"{value:.{digits}f}"


def _range(minimum: float | None, maximum: float | None, digits: int = 2) -> str:
    if minimum is None or maximum is None:
        return "N/A"
    return f"{minimum:.{digits}f} – {maximum:.{digits}f}"
