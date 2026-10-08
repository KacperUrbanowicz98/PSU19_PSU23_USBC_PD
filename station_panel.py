"""Panel jednego kanału pomiarowego (jedno gniazdo z własnym testerem PM125).

Przy dwóch gniazdach okna modalne byłyby nie do opanowania — dwa testy biegną
jednocześnie i każdy musi pokazywać swój przebieg oraz swój wynik. Dlatego postęp
i wynik są wyświetlane w kolumnie kanału, bez osobnych okien.
"""
from __future__ import annotations

import logging
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk
from typing import Callable

from config import AppConfig, StationConfig
from engine import ProgressEvent, TestEngine
from hardware import Hardware, PM125Client
from models import PHASE_LOAD, FinalStatus, ProfileStatus, TestResult
from storage import PendingRecord, SaveState
from theme import COLORS

logger = logging.getLogger(__name__)

#: Statusy, które zużywają próbę numeru seryjnego. Błąd stanowiska jej nie zużywa.
ATTEMPT_CONSUMING_STATUSES = {FinalStatus.PASS, FinalStatus.FAIL}


def friendly_error(exc: BaseException) -> str:
    """Komunikat dla operatora zamiast surowej treści wyjątku Pythona."""
    if isinstance(exc, ConnectionError):
        return "Brak połączenia z testerem. Sprawdź kabel USB i użyj „Połącz ponownie”."
    if isinstance(exc, TimeoutError):
        return "Tester nie odpowiedział w wymaganym czasie. Powtórz test."
    if isinstance(exc, PermissionError):
        return "Brak uprawnień do pliku na stanowisku. Zgłoś to do IT."
    if isinstance(exc, OSError):
        return "Błąd dostępu do pliku lub urządzenia. Zgłoś to do utrzymania ruchu."
    return "Błąd stanowiska. Powtórz test; jeśli się powtórzy, zgłoś utrzymaniu ruchu."


class StationPanel:
    """Kolumna jednego kanału: skan numeru, przebieg testu i wynik w jednym miejscu."""

    def __init__(
        self,
        parent: tk.Widget,
        station: StationConfig,
        config: AppConfig,
        hardware: Hardware,
        services,
        compact: bool = False,
    ) -> None:
        self.station = station
        self.config = config
        self.hardware = hardware
        self.engine = TestEngine(config, hardware)
        self.services = services          # wspólne: raport, liczniki, dane surowe, kolejka GUI
        self.compact = compact

        self.busy = False
        self.connecting = False
        self.cancel_event = threading.Event()
        self.current_serial = ""
        self.session_pass = 0
        self.session_non_pass = 0
        self.last_result: TestResult | None = None
        self._result_clear_job: str | None = None
        self._activity_step = 0

        self.frame = tk.Frame(
            parent, bg=COLORS["card"], highlightbackground=COLORS["border"], highlightthickness=1
        )
        self._build()
        self._set_idle()

    # ------------------------------------------------------------------ budowa

    def _build(self) -> None:
        header = tk.Frame(self.frame, bg=COLORS["primary"], height=56)
        header.pack(fill=tk.X)
        header.pack_propagate(False)
        tk.Label(
            header, text=self.station.name, bg=COLORS["primary"], fg="white",
            font=("Segoe UI", 16, "bold"),
        ).pack(side=tk.LEFT, padx=16)
        self.connection_label = tk.Label(
            header, text="", bg=COLORS["error"], fg="white",
            font=("Segoe UI", 8, "bold"), padx=10, pady=5,
        )
        self.connection_label.pack(side=tk.RIGHT, padx=(0, 10))
        self.reconnect_button = self._button(
            header, "Połącz ponownie", self.connect_hardware, "#FFFFFF", "#EAF2F8",
            foreground=COLORS["primary"], active_foreground=COLORS["primary"],
            font_size=8, padx=10, pady=5,
        )
        self.reconnect_button.pack(side=tk.RIGHT, padx=(0, 8))

        body = tk.Frame(self.frame, bg=COLORS["card"])
        body.pack(fill=tk.BOTH, expand=True, padx=14, pady=12)
        body.columnconfigure(0, weight=1)

        tk.Label(
            body, text="Numer seryjny zasilacza", bg=COLORS["card"], fg=COLORS["text"],
            font=("Segoe UI", 9, "bold"), anchor="w",
        ).grid(row=0, column=0, sticky="w", pady=(0, 3))
        self.serial_entry = ttk.Entry(
            body, style="Modern.TEntry", font=("Segoe UI", 16, "bold"), justify="center"
        )
        self.serial_entry.grid(row=1, column=0, sticky="ew")
        self.serial_entry.bind("<Return>", lambda _event: self.start_test())

        self.start_button = self._button(
            body, "ROZPOCZNIJ TEST", self.start_test, COLORS["success"], COLORS["success_hover"],
            font_size=10, padx=18, pady=9,
        )
        self.start_button.grid(row=2, column=0, sticky="ew", pady=(8, 0))

        self.blocker_label = tk.Label(
            body, text="", bg=COLORS["card"], fg=COLORS["muted"], font=("Segoe UI", 8),
            anchor="w", justify=tk.LEFT, wraplength=360,
        )
        self.blocker_label.grid(row=3, column=0, sticky="ew", pady=(6, 0))

        # --- obszar wymienny: spoczynek / przebieg / wynik ---
        self.stage = tk.Frame(body, bg=COLORS["card"])
        self.stage.grid(row=4, column=0, sticky="nsew", pady=(10, 0))
        body.rowconfigure(4, weight=1)
        self._build_idle_area()
        self._build_progress_area()
        self._build_result_area()

    def _build_idle_area(self) -> None:
        """Widok spoczynkowy — gniazdo gotowe, z licznikiem tego kanału."""
        self.idle_frame = tk.Frame(self.stage, bg=COLORS["card"])

        self.idle_headline = tk.Label(
            self.idle_frame, text="GOTOWE", bg=COLORS["card"], fg=COLORS["primary_soft"],
            font=("Segoe UI", 34, "bold"),
        )
        self.idle_headline.pack(pady=(18, 0))
        self.idle_hint = tk.Label(
            self.idle_frame, text="Zeskanuj numer seryjny zasilacza", bg=COLORS["card"],
            fg=COLORS["muted"], font=("Segoe UI", 10),
        )
        self.idle_hint.pack(pady=(0, 14))

        counters = tk.Frame(self.idle_frame, bg=COLORS["card"])
        counters.pack()
        self.idle_pass = self._counter_cell(counters, 0, "PASS", COLORS["success"])
        self.idle_fail = self._counter_cell(counters, 1, "FAIL / ERROR", COLORS["error"])

    def _counter_cell(self, parent: tk.Widget, column: int, title: str, color: str) -> tk.Label:
        cell = tk.Frame(parent, bg=COLORS["card"])
        cell.grid(row=0, column=column, padx=18)
        tk.Label(
            cell, text=title, bg=COLORS["card"], fg=COLORS["muted"], font=("Segoe UI", 8, "bold")
        ).pack()
        value = tk.Label(
            cell, text="0", bg=COLORS["card"], fg=color, font=("Segoe UI", 20, "bold")
        )
        value.pack()
        return value

    def _refresh_idle(self) -> None:
        self.idle_pass.config(text=str(self.session_pass))
        self.idle_fail.config(text=str(self.session_non_pass))
        if not self.hardware.connected:
            self.idle_headline.config(text="BRAK TESTERA", fg=COLORS["error_soft"])
            self.idle_hint.config(text=f"Tester {self.station.address} nie odpowiada")
        elif not self.services.operator_logged:
            self.idle_headline.config(text="ZALOGUJ", fg=COLORS["primary_soft"])
            self.idle_hint.config(text="Operator musi się zalogować numerem HRID")
        else:
            self.idle_headline.config(text="GOTOWE", fg=COLORS["success_soft"])
            self.idle_hint.config(text="Zeskanuj numer seryjny zasilacza")

    def _build_progress_area(self) -> None:
        self.progress_frame = tk.Frame(self.stage, bg=COLORS["card"])

        self.progress_status = tk.Label(
            self.progress_frame, text="", bg=COLORS["card"], fg=COLORS["text"],
            font=("Segoe UI", 15, "bold"),
        )
        self.progress_status.pack(pady=(2, 2))
        step_row = tk.Frame(self.progress_frame, bg=COLORS["card"])
        step_row.pack(pady=(0, 8))
        self.progress_step = tk.Label(
            step_row, text="", bg=COLORS["card"], fg=COLORS["muted"], font=("Segoe UI", 9, "bold")
        )
        self.progress_step.pack(side=tk.LEFT, padx=(0, 8))
        self.progress_phase = tk.Label(
            step_row, text="", bg=COLORS["primary_soft"], fg=COLORS["primary"],
            font=("Segoe UI", 9, "bold"), padx=8, pady=3,
        )
        self.progress_phase.pack(side=tk.LEFT)

        values = tk.Frame(self.progress_frame, bg=COLORS["card"])
        values.pack(fill=tk.X)
        values.columnconfigure(0, weight=1)
        values.columnconfigure(1, weight=1)
        self.voltage_label = self._measurement_cell(values, 0, "NAPIĘCIE")
        self.current_label = self._measurement_cell(values, 1, "PRĄD")

        status_row = tk.Frame(self.progress_frame, bg=COLORS["card"])
        status_row.pack(fill=tk.X, pady=(8, 4))
        self.measurement_status = tk.Label(
            status_row, text="", bg=COLORS["primary_soft"], fg=COLORS["primary"],
            font=("Segoe UI", 9, "bold"), padx=8, pady=4,
        )
        self.measurement_status.pack(side=tk.LEFT)
        self.elapsed_label = tk.Label(
            status_row, text="", bg=COLORS["card"], fg=COLORS["muted"], font=("Segoe UI", 9, "bold")
        )
        self.elapsed_label.pack(side=tk.RIGHT)

        self.progress_bar = ttk.Progressbar(
            self.progress_frame, mode="determinate", maximum=100,
            style="Test.Horizontal.TProgressbar",
        )
        self.progress_bar.pack(fill=tk.X, pady=(2, 3))
        self.activity_label = tk.Label(
            self.progress_frame, text="", bg=COLORS["card"], fg=COLORS["muted"], font=("Segoe UI", 8)
        )
        self.activity_label.pack()
        self.cancel_button = self._button(
            self.progress_frame, "ANULUJ", self._cancel_test, COLORS["error"],
            COLORS["error_hover"], font_size=9, padx=16, pady=6,
        )
        self.cancel_button.pack(pady=(6, 0))

    def _build_result_area(self) -> None:
        self.result_frame = tk.Frame(self.stage, bg=COLORS["card"])

        self.result_banner = tk.Label(
            self.result_frame, text="", bg=COLORS["success"], fg="white",
            font=("Segoe UI", 28, "bold"), pady=10,
        )
        self.result_banner.pack(fill=tk.X)
        self.result_serial = tk.Label(
            self.result_frame, text="", bg=COLORS["card"], fg=COLORS["text"],
            font=("Segoe UI", 11, "bold"), pady=4,
        )
        self.result_serial.pack(fill=tk.X)

        columns = ("profile", "status", "u_load")
        self.result_tree = ttk.Treeview(
            self.result_frame, columns=columns, show="headings", height=4,
            style="Result.Treeview", selectmode="none",
        )
        for column, heading, width in zip(
            columns, ("Profil", "Wynik", "U pod obc. [V]"), (80, 110, 170)
        ):
            self.result_tree.heading(column, text=heading)
            self.result_tree.column(column, width=width, anchor=tk.CENTER)
        self.result_tree.tag_configure("pass", foreground=COLORS["success"])
        self.result_tree.tag_configure("fail", foreground=COLORS["error"])
        self.result_tree.tag_configure("other", foreground=COLORS["warning"])
        self.result_tree.pack(fill=tk.X, pady=(6, 4))

        self.result_message = tk.Label(
            self.result_frame, text="", bg=COLORS["card"], fg=COLORS["error"],
            font=("Segoe UI", 9, "bold"), wraplength=380, justify=tk.LEFT, anchor="w",
        )
        self.result_message.pack(fill=tk.X)

        actions = tk.Frame(self.result_frame, bg=COLORS["card"])
        actions.pack(fill=tk.X, pady=(6, 0))
        self.retest_button = self._button(
            actions, "POWTÓRZ", self._retest, COLORS["warning"], COLORS["warning_hover"],
            font_size=9, padx=14, pady=6,
        )
        self.retest_button.pack(side=tk.LEFT)
        self.result_countdown = tk.Label(
            actions, text="", bg=COLORS["card"], fg=COLORS["muted"], font=("Segoe UI", 8)
        )
        self.result_countdown.pack(side=tk.RIGHT)

    def _measurement_cell(self, parent: tk.Widget, column: int, title: str) -> tk.Label:
        cell = tk.Frame(
            parent, bg=COLORS["card"], highlightbackground=COLORS["border"], highlightthickness=1
        )
        cell.grid(row=0, column=column, sticky="ew", padx=(0, 5) if column == 0 else (5, 0))
        tk.Label(
            cell, text=title, bg=COLORS["card"], fg=COLORS["muted"], font=("Segoe UI", 8, "bold")
        ).pack(pady=(6, 0))
        value = tk.Label(
            cell, text="—", bg=COLORS["card"], fg=COLORS["primary"],
            font=("Segoe UI", 24 if not self.compact else 20, "bold"),
        )
        value.pack(pady=(0, 7))
        return value

    # ------------------------------------------------------------------ stany

    def _show(self, frame: tk.Frame | None) -> None:
        target = frame if frame is not None else self.idle_frame
        for candidate in (self.idle_frame, self.progress_frame, self.result_frame):
            if candidate is not target and candidate.winfo_manager():
                candidate.pack_forget()
        if not target.winfo_manager():
            target.pack(fill=tk.BOTH, expand=True)
        if target is self.idle_frame:
            self._refresh_idle()

    def _set_idle(self) -> None:
        self._show(None)
        self.sync_controls()

    @property
    def can_start(self) -> bool:
        return (
            self.services.operator_logged
            and self.hardware.connected
            and not self.busy
            and not self.services.blocked
            and self.last_result is None
        )

    def _blocker_text(self) -> str:
        if self.busy:
            return "Test w toku..."
        if self.services.blocked:
            return self.services.blocked_reason
        if not self.services.operator_logged:
            return "Zaloguj operatora, aby odblokować test."
        if not self.hardware.connected:
            return f"Tester {self.station.address} niedostępny — sprawdź USB."
        if self.last_result is not None:
            return "Zamknij wynik, aby zeskanować kolejny zasilacz."
        return "Gotowe. Zeskanuj numer seryjny."

    def sync_controls(self) -> None:
        start = self.can_start
        self.serial_entry.config(state=tk.NORMAL if start else tk.DISABLED)
        self.start_button.config(state=tk.NORMAL if start else tk.DISABLED)
        self.reconnect_button.config(
            state=tk.DISABLED if (self.connecting or self.busy) else tk.NORMAL
        )
        self.blocker_label.config(text=self._blocker_text())
        self.update_connection_indicator()
        if self.idle_frame.winfo_manager():
            self._refresh_idle()

    def update_connection_indicator(self) -> None:
        if self.connecting:
            text, color = "ŁĄCZENIE...", COLORS["warning"]
        elif self.hardware.connected:
            text, color = "TESTER OK", COLORS["success"]
        else:
            text, color = "BRAK TESTERA", COLORS["error"]
        self.connection_label.config(text=text, bg=color)

    def focus_serial(self) -> None:
        if self.can_start:
            self.serial_entry.focus_set()

    # ------------------------------------------------------------------ sprzęt

    def connect_hardware(self) -> None:
        if self.connecting or self.busy:
            return
        self.connecting = True
        self.sync_controls()

        def worker() -> None:
            try:
                connected = self.hardware.connect()
                self.services.post(("connection", self, connected, self.hardware.last_error))
            except Exception as exc:
                logger.exception("[%s] Błąd łączenia z testerem", self.station.name)
                self.services.post(("connection", self, False, str(exc)))

        threading.Thread(target=worker, daemon=True, name=f"connect-{self.station.name}").start()

    def health_check(self) -> None:
        if self.busy or self.connecting or not self.hardware.connected:
            return

        def worker() -> None:
            try:
                alive = self.hardware.ping()
            except Exception:
                alive = False
            self.services.post(("health", self, alive, self.hardware.last_error))

        threading.Thread(target=worker, daemon=True, name=f"health-{self.station.name}").start()

    # -------------------------------------------------------------------- test

    def start_test(self, serial_override: str | None = None) -> None:
        if not self.can_start and serial_override is None:
            return
        if self.busy or self.services.blocked:
            return
        if not self.services.operator_logged:
            messagebox.showwarning("HRID", "Najpierw zaloguj operatora.")
            return
        if not self.hardware.connected:
            messagebox.showwarning("Tester", f"Tester {self.station.address} nie odpowiada.")
            return

        serial = (serial_override or self.serial_entry.get()).strip().upper()
        minimum = self.config.serial_minimum_length
        if len(serial) < minimum:
            messagebox.showwarning(
                "Numer seryjny", f"Numer seryjny musi mieć co najmniej {minimum} znaków."
            )
            return

        holder = self.services.attempts.reserve(serial, self.station.name)
        if holder is not None:
            messagebox.showwarning(
                "Numer seryjny",
                f"Ten numer jest właśnie testowany na stanowisku {holder}.\n"
                "Nie testuj tej samej sztuki na dwóch gniazdach jednocześnie.",
            )
            return

        count = self.services.attempts.count(serial)
        limit = self.config.maximum_serial_attempts
        if count >= limit:
            self.services.attempts.release(serial, self.station.name)
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
            self.services.attempts.release(serial, self.station.name)
            return

        self.busy = True
        self.current_serial = serial
        self.cancel_event = threading.Event()
        self._start_progress(serial)
        self.sync_controls()
        hrid = self.services.operator_hrid

        def worker() -> None:
            try:
                detected, voltage, message = self.engine.detect_dut()
                if not detected:
                    self.services.post(("no_dut", self, message, voltage))
                    return
                result = self.engine.run(
                    hrid=hrid,
                    serial_number=serial,
                    progress=lambda event: self.services.post(("progress", self, event)),
                    cancel_event=self.cancel_event,
                    station=self.station.name,
                )
                raw_path = self.services.raw_writer.write(result)
                if raw_path:
                    result.raw_file = raw_path.name
                outcome = self.services.results.save(result)
                self.services.post(("test_done", self, result, outcome))
            except Exception as exc:
                logger.exception("[%s] Błąd uruchomienia testu", self.station.name)
                self.services.post(("test_error", self, friendly_error(exc), str(exc)))

        threading.Thread(target=worker, daemon=True, name=f"test-{self.station.name}").start()

    def _start_progress(self, serial: str) -> None:
        self._cancel_result_timer()
        self.last_result = None
        self._started_at = time.monotonic()
        self.progress_status.config(text="Wykrywanie zasilacza...")
        self.progress_step.config(text="")
        self.progress_phase.config(text="")
        self.voltage_label.config(text="—", fg=COLORS["primary"])
        self.current_label.config(text="—", fg=COLORS["primary"])
        self.measurement_status.config(
            text="OCZEKIWANIE", bg=COLORS["primary_soft"], fg=COLORS["primary"]
        )
        self.progress_bar["value"] = 0
        self.cancel_button.config(state=tk.NORMAL, text="ANULUJ")
        self._show(self.progress_frame)
        self._tick()

    def _tick(self) -> None:
        if not self.busy:
            return
        elapsed = max(0, int(time.monotonic() - self._started_at))
        minutes, seconds = divmod(elapsed, 60)
        self._activity_step = (self._activity_step + 1) % 4
        self.elapsed_label.config(text=f"{minutes:02d}:{seconds:02d}")
        self.activity_label.config(text="odczyt z testera" + "." * self._activity_step)
        self.frame.after(250, self._tick)

    def _cancel_test(self) -> None:
        self.cancel_event.set()
        self.cancel_button.config(state=tk.DISABLED, text="ANULOWANIE...")

    def _retest(self) -> None:
        serial = self.current_serial
        self._clear_result()
        if serial:
            self.frame.after(100, lambda: self.start_test(serial_override=serial))

    # ------------------------------------------------------------- zdarzenia

    def handle_progress(self, event: ProgressEvent) -> None:
        if not self.busy:
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
        self.voltage_label.config(text=f"{event.voltage:.3f} V", fg=color)
        self.current_label.config(text=f"{event.current:.3f} A", fg=color)
        self.measurement_status.config(text=status, bg=soft, fg=color)
        self.progress_bar["value"] = max(0.0, min(1.0, event.overall_fraction)) * 97.0

    def handle_test_done(self, result: TestResult, outcome) -> None:
        self.busy = False
        self.services.attempts.release(result.serial_number, self.station.name)

        if result.final_status in ATTEMPT_CONSUMING_STATUSES:
            try:
                self.services.attempts.record(result.serial_number)
            except OSError as exc:
                logger.error("[%s] Nie zapisano licznika prób: %s", self.station.name, exc)

        if result.final_status is FinalStatus.PASS:
            self.session_pass += 1
        else:
            self.session_non_pass += 1
        if not outcome.saved:
            self.services.add_pending(PendingRecord.from_result(result))
        self.services.register_result(self, result)
        self._show_result(result, outcome)
        self.sync_controls()

    def handle_test_error(self, message: str, detail: str) -> None:
        self.busy = False
        if self.current_serial:
            self.services.attempts.release(self.current_serial, self.station.name)
        logger.error("[%s] Test zakończony błędem: %s", self.station.name, detail)
        self._show(None)
        self.sync_controls()
        messagebox.showerror(
            f"Test — {self.station.name}", f"{message}\n\nSzczegóły zapisano w logu aplikacji."
        )
        self.focus_serial()

    def handle_no_dut(self, message: str) -> None:
        self.busy = False
        if self.current_serial:
            self.services.attempts.release(self.current_serial, self.station.name)
        logger.info("[%s] Pre-kontrola: %s", self.station.name, message)
        self._show(None)
        self.sync_controls()
        messagebox.showwarning(
            f"Brak zasilacza — {self.station.name}",
            f"{message}\n\nPodłącz zasilacz i zeskanuj numer ponownie.\n"
            "Ta próba nie została policzona.",
        )
        self._clear_serial()
        self.focus_serial()

    # ---------------------------------------------------------------- wynik

    def _show_result(self, result: TestResult, outcome) -> None:
        self.last_result = result
        is_pass = result.final_status is FinalStatus.PASS
        color = COLORS["success"] if is_pass else COLORS["error"]
        self.result_banner.config(text=result.final_status.value, bg=color)
        self.result_serial.config(
            text=f"{result.serial_number}   ·   {result.duration_seconds:.1f} s   ·   "
                 f"{result.sample_count} próbek"
        )
        for item in self.result_tree.get_children():
            self.result_tree.delete(item)
        for profile_result in result.profiles:
            if profile_result.status is ProfileStatus.PASS:
                tag = "pass"
            elif profile_result.status is ProfileStatus.FAIL:
                tag = "fail"
            else:
                tag = "other"
            minimum = profile_result.min_loaded_voltage
            maximum = profile_result.max_loaded_voltage
            span = "N/A" if minimum is None or maximum is None else f"{minimum:.2f} – {maximum:.2f}"
            self.result_tree.insert(
                "", tk.END,
                values=(profile_result.profile.name, profile_result.status.value, span),
                tags=(tag,),
            )
        message = result.first_violation or result.message
        if outcome.state is SaveState.FAILED:
            message = (message + "  " if message else "") + "Wynik niezapisany — użyj „Ponów zapis”."
        elif outcome.state is SaveState.BACKUP:
            message = (message + "  " if message else "") + "Zapisano kopię awaryjną."
        self.result_message.config(text=message, fg=color if message else COLORS["muted"])
        self._show(self.result_frame)

        seconds = int(
            self.config.result_window_pass_seconds if is_pass
            else self.config.result_window_fail_seconds
        )
        self._countdown(max(1, seconds))

    def _countdown(self, left: int) -> None:
        if self.last_result is None:
            return
        if left <= 0:
            self._clear_result()
            return
        self.result_countdown.config(text=f"zniknie za {left} s")
        self._result_clear_job = self.frame.after(1000, self._countdown, left - 1)

    def _cancel_result_timer(self) -> None:
        if self._result_clear_job is not None:
            try:
                self.frame.after_cancel(self._result_clear_job)
            except (tk.TclError, ValueError):
                pass
            self._result_clear_job = None

    def _clear_result(self) -> None:
        self._cancel_result_timer()
        self.last_result = None
        self._show(None)
        self._clear_serial()
        self.sync_controls()
        self.focus_serial()

    def _clear_serial(self) -> None:
        self.serial_entry.config(state=tk.NORMAL)
        self.serial_entry.delete(0, tk.END)

    # ------------------------------------------------------------- narzędzia

    def reconfigure(self, config: AppConfig) -> None:
        self.config = config
        self.engine.config = config
        if isinstance(self.hardware, PM125Client):
            self.hardware.reconfigure(
                config.console_path, self.station.device_serial, config.command_timeout
            )

    def shutdown(self) -> None:
        self.cancel_event.set()

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
