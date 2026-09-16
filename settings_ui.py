"""Panel ustawień inżynieryjnych PSU19 Tester."""
from __future__ import annotations

import logging
import tkinter as tk
from dataclasses import replace
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Callable

from config import AppConfig, ConfigError
from models import TestProfile
from storage import AttemptStore
from theme import COLORS

logger = logging.getLogger(__name__)


class SettingsDialog:
    """Panel konfiguracji utrzymany w stylistyce głównego interfejsu."""

    def __init__(
        self,
        parent: tk.Tk,
        config: AppConfig,
        config_path: Path,
        on_saved: Callable[[AppConfig], None],
        attempts: AttemptStore | None = None,
        is_busy: Callable[[], bool] | None = None,
        operator_hrid: str | None = None,
    ) -> None:
        self.config = config
        self.config_path = config_path
        self.on_saved = on_saved
        self.attempts = attempts
        self.is_busy = is_busy or (lambda: False)
        self.operator_hrid = operator_hrid
        self.profiles = [replace(item) for item in config.profiles]

        self.window = tk.Toplevel(parent)
        self.window.title("Panel inżynieryjny — PSU USB-C Tester")
        self.window.geometry("1300x1000")
        self.window.minsize(1040, 700)
        self.window.transient(parent)
        self.window.configure(bg=COLORS["background"])
        self.window.protocol("WM_DELETE_WINDOW", self.window.destroy)

        self._configure_styles()
        self._build_header()
        self._build_content()
        self._build_footer()
        self._center_window(parent)

    def exists(self) -> bool:
        try:
            return bool(self.window.winfo_exists())
        except tk.TclError:
            return False

    def focus(self) -> None:
        self.window.lift()
        self.window.focus_force()

    # --------------------------------------------------------------- style

    def _configure_styles(self) -> None:
        style = ttk.Style(self.window)
        style.theme_use("clam")
        style.configure(
            "Settings.TNotebook", background=COLORS["background"], borderwidth=0, tabmargins=(0, 0, 0, 0)
        )
        style.configure(
            "Settings.TNotebook.Tab",
            background=COLORS["card"], foreground=COLORS["muted"],
            font=("Segoe UI", 10, "bold"), padding=(20, 11), borderwidth=0,
        )
        style.map(
            "Settings.TNotebook.Tab",
            background=[("selected", COLORS["primary_soft"]), ("active", "#F2F6F9")],
            foreground=[("selected", COLORS["primary"]), ("active", COLORS["text"])],
        )
        style.configure("Settings.TFrame", background=COLORS["background"], borderwidth=0)
        style.configure(
            "Settings.TEntry",
            fieldbackground=COLORS["entry"], foreground=COLORS["text"],
            bordercolor=COLORS["border"], lightcolor=COLORS["border"], darkcolor=COLORS["border"],
            insertcolor=COLORS["text"], padding=8,
        )
        style.map(
            "Settings.TEntry",
            bordercolor=[("focus", COLORS["primary"])],
            lightcolor=[("focus", COLORS["primary"])],
            darkcolor=[("focus", COLORS["primary"])],
        )
        style.configure(
            "Settings.Treeview",
            rowheight=32, font=("Segoe UI", 10),
            background=COLORS["card"], fieldbackground=COLORS["card"],
            foreground=COLORS["text"], borderwidth=0,
        )
        style.configure(
            "Settings.Treeview.Heading",
            font=("Segoe UI", 9, "bold"), background=COLORS["primary_soft"],
            foreground=COLORS["primary"], relief=tk.FLAT, padding=(7, 9),
        )
        style.map(
            "Settings.Treeview",
            background=[("selected", COLORS["primary_soft"])],
            foreground=[("selected", COLORS["primary"])],
        )
        style.configure(
            "Settings.Vertical.TScrollbar",
            troughcolor=COLORS["background"], background=COLORS["border"],
            bordercolor=COLORS["background"], arrowcolor=COLORS["muted"],
        )
        style.configure(
            "Settings.TCheckbutton",
            background=COLORS["card"], foreground=COLORS["text"], font=("Segoe UI", 9),
        )

    def _build_header(self) -> None:
        header = tk.Frame(self.window, bg=COLORS["primary"], height=88)
        header.pack(fill=tk.X)
        header.pack_propagate(False)

        title_area = tk.Frame(header, bg=COLORS["primary"])
        title_area.pack(side=tk.LEFT, fill=tk.Y, padx=26)
        tk.Label(
            title_area, text="Panel inżynieryjny", bg=COLORS["primary"], fg="white",
            font=("Segoe UI", 23, "bold"), anchor="w",
        ).pack(anchor="w", pady=(13, 0))
        tk.Label(
            title_area, text="Konfiguracja stanowiska, profili testowych i operatorów",
            bg=COLORS["primary"], fg="#DDEAF4", font=("Segoe UI", 9), anchor="w",
        ).pack(anchor="w", pady=(0, 12))

        tk.Label(
            header, text="TRYB INŻYNIERYJNY", bg="#FFFFFF", fg=COLORS["primary"],
            font=("Segoe UI", 9, "bold"), padx=14, pady=8,
        ).pack(side=tk.RIGHT, padx=26, pady=25)

    def _build_content(self) -> None:
        outer = tk.Frame(self.window, bg=COLORS["background"])
        outer.pack(fill=tk.BOTH, expand=True, padx=22, pady=(18, 12))

        notebook_card = self._card(outer)
        notebook_card.pack(fill=tk.BOTH, expand=True)

        self.notebook = ttk.Notebook(notebook_card, style="Settings.TNotebook")
        self.notebook.pack(fill=tk.BOTH, expand=True, padx=1, pady=1)

        general_tab = ttk.Frame(self.notebook, style="Settings.TFrame")
        profiles_tab = ttk.Frame(self.notebook, style="Settings.TFrame")
        hrid_tab = ttk.Frame(self.notebook, style="Settings.TFrame")
        attempts_tab = ttk.Frame(self.notebook, style="Settings.TFrame")
        logs_tab = ttk.Frame(self.notebook, style="Settings.TFrame")
        self.notebook.add(general_tab, text="  Ogólne  ")
        self.notebook.add(profiles_tab, text="  Profile testowe  ")
        self.notebook.add(hrid_tab, text="  Operatorzy HRID  ")
        self.notebook.add(attempts_tab, text="  Liczniki prób  ")
        self.notebook.add(logs_tab, text="  Logi  ")

        self.general_vars = self._build_general(general_tab)
        self._build_profiles(profiles_tab)
        self._build_hrids(hrid_tab)
        self._build_attempts(attempts_tab)
        self._build_logs(logs_tab)
        self._refresh_estimate()
        self.notebook.bind("<<NotebookTabChanged>>", lambda _event: self._refresh_estimate())

    def _build_footer(self) -> None:
        footer = tk.Frame(self.window, bg=COLORS["background"])
        footer.pack(fill=tk.X, padx=22, pady=(0, 18))

        self.footer_label = tk.Label(
            footer,
            text="Zmiany zostaną zapisane w pliku konfiguracji i zapisane w dzienniku audytu.",
            bg=COLORS["background"], fg=COLORS["muted"], font=("Segoe UI", 9),
        )
        self.footer_label.pack(side=tk.LEFT)

        self._button(
            footer, "ANULUJ", self.window.destroy, COLORS["card"], COLORS["primary_soft"],
            foreground=COLORS["primary"], active_foreground=COLORS["primary"], padx=22, pady=9,
        ).pack(side=tk.RIGHT, padx=(10, 0))
        self._button(
            footer, "ZAPISZ USTAWIENIA", self._save, COLORS["success"], COLORS["success_hover"],
            font_size=10, padx=24, pady=9,
        ).pack(side=tk.RIGHT)

    # ------------------------------------------------------------- Ogólne

    def _build_general(self, parent: ttk.Frame) -> dict[str, tk.Variable]:
        text_values = {
            "console_path": self.config.console_path,
            "device_serial": self.config.device_serial or "",
            "reports_directory": self.config.reports_directory,
            "backup_directory": self.config.backup_directory,
            "logs_directory": self.config.logs_directory,
            "raw_directory": self.config.raw_directory,
            "attempts_file": self.config.attempts_file,
            "measurement_interval": str(self.config.measurement_interval),
            "profile_settle_seconds": str(self.config.profile_settle_seconds),
            "load_settle_seconds": str(self.config.load_settle_seconds),
            "settling_ignore_seconds": str(self.config.settling_ignore_seconds),
            "command_timeout": str(self.config.command_timeout),
            "test_timeout": str(self.config.test_timeout),
            "minimum_samples": str(self.config.minimum_samples),
            "maximum_read_errors": str(self.config.maximum_read_errors),
            "maximum_serial_attempts": str(self.config.maximum_serial_attempts),
            "serial_minimum_length": str(self.config.serial_minimum_length),
            "dut_precheck_min_voltage": str(self.config.dut_precheck_min_voltage),
            "health_check_seconds": str(self.config.health_check_seconds),
            "result_window_pass_seconds": str(self.config.result_window_pass_seconds),
            "result_window_fail_seconds": str(self.config.result_window_fail_seconds),
            "maximum_csv_rows": str(self.config.maximum_csv_rows),
            "raw_retention_days": str(self.config.raw_retention_days),
            "attempts_retention_days": str(self.config.attempts_retention_days),
            "log_retention_days": str(self.config.log_retention_days),
        }
        variables: dict[str, tk.Variable] = {
            key: tk.StringVar(value=value) for key, value in text_values.items()
        }
        variables["dut_precheck_enabled"] = tk.BooleanVar(value=self.config.dut_precheck_enabled)
        variables["raw_samples_enabled"] = tk.BooleanVar(value=self.config.raw_samples_enabled)
        variables["debug_logging"] = tk.BooleanVar(value=self.config.debug_logging)

        body = tk.Frame(parent, bg=COLORS["background"])
        body.pack(fill=tk.BOTH, expand=True, padx=16, pady=16)
        body.columnconfigure(0, weight=1, uniform="general")
        body.columnconfigure(1, weight=1, uniform="general")
        body.rowconfigure(0, weight=1)

        # --- karta lewa: ścieżki + przełączniki ---
        left = tk.Frame(body, bg=COLORS["background"])
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 8))

        paths_card = self._card(left)
        paths_card.pack(fill=tk.BOTH, expand=True)
        self._section_title(paths_card, "Ścieżki i urządzenie", "Lokalizacje plików oraz identyfikacja PM125")
        paths_content = tk.Frame(paths_card, bg=COLORS["card"])
        paths_content.pack(fill=tk.BOTH, expand=True, padx=18, pady=(4, 14))
        paths_content.columnconfigure(0, weight=1)

        path_fields = [
            ("console_path", "USBPDConsole.exe", "exe"),
            ("device_serial", "Serial PM125", None),
            ("reports_directory", "Folder raportów", "directory"),
            ("backup_directory", "Folder backup", "directory"),
            ("logs_directory", "Folder logów", "directory"),
            ("raw_directory", "Folder danych surowych", "directory"),
            ("attempts_file", "Plik liczników seriali", "json"),
        ]
        for row, (key, label, browse_type) in enumerate(path_fields):
            self._labeled_entry(paths_content, row, label, variables[key], browse_type=browse_type)

        switches_card = self._card(left)
        switches_card.pack(fill=tk.X, pady=(12, 0))
        self._section_title(switches_card, "Przełączniki", "Funkcje włączane i wyłączane na stanowisku")
        switches = tk.Frame(switches_card, bg=COLORS["card"])
        switches.pack(fill=tk.X, padx=18, pady=(2, 16))
        for key, label in (
            ("dut_precheck_enabled", "Kontrola obecności zasilacza przed testem"),
            ("raw_samples_enabled", "Zapis surowych próbek każdego testu"),
            ("debug_logging", "Log DEBUG (każda komenda PM125) — tylko do diagnostyki"),
        ):
            ttk.Checkbutton(
                switches, text=label, variable=variables[key], style="Settings.TCheckbutton"
            ).pack(anchor="w", pady=3)

        # --- karta prawa: parametry liczbowe ---
        params_card = self._card(body)
        params_card.grid(row=0, column=1, sticky="nsew", padx=(8, 0))
        self._section_title(params_card, "Parametry systemowe", "Czasy, limity oraz bezpieczeństwo testu")
        params_content = tk.Frame(params_card, bg=COLORS["card"])
        params_content.pack(fill=tk.BOTH, expand=True, padx=18, pady=(4, 18))
        params_content.columnconfigure(1, weight=1)

        parameter_fields = [
            ("measurement_interval", "Interwał pomiarów", "s"),
            ("profile_settle_seconds", "Stabilizacja profilu", "s"),
            ("load_settle_seconds", "Stabilizacja obciążenia", "s"),
            ("settling_ignore_seconds", "Okno nieoceniane po zmianie", "s"),
            ("command_timeout", "Timeout komendy", "s"),
            ("test_timeout", "Timeout całego testu", "s"),
            ("minimum_samples", "Min. liczba próbek", "na fazę"),
            ("maximum_read_errors", "Kolejne błędy odczytu", "limit"),
            ("maximum_serial_attempts", "Maks. liczba prób serialu", "limit"),
            ("serial_minimum_length", "Min. długość numeru seryjnego", "znaków"),
            ("dut_precheck_min_voltage", "Próg wykrycia zasilacza", "V"),
            ("health_check_seconds", "Kontrola obecności PM125", "s"),
            ("result_window_pass_seconds", "Okno wyniku PASS", "s"),
            ("result_window_fail_seconds", "Okno wyniku FAIL", "s"),
            ("maximum_csv_rows", "Maks. liczba wierszy CSV", "wierszy"),
            ("raw_retention_days", "Retencja danych surowych", "dni"),
            ("attempts_retention_days", "Retencja liczników prób", "dni"),
            ("log_retention_days", "Retencja logów", "dni"),
        ]
        for row, (key, label, suffix) in enumerate(parameter_fields):
            tk.Label(
                params_content, text=label, bg=COLORS["card"], fg=COLORS["text"],
                font=("Segoe UI", 9), anchor="w",
            ).grid(row=row, column=0, sticky="w", pady=4)
            ttk.Entry(
                params_content, textvariable=variables[key], style="Settings.TEntry",
                font=("Segoe UI", 10), justify="center", width=14,
            ).grid(row=row, column=1, sticky="e", padx=(10, 8), pady=4)
            tk.Label(
                params_content, text=suffix, bg=COLORS["card"], fg=COLORS["muted"],
                font=("Segoe UI", 8), width=8, anchor="w",
            ).grid(row=row, column=2, sticky="w", pady=4)

        self.estimate_label = tk.Label(
            params_content, text="", bg=COLORS["primary_soft"], fg=COLORS["primary"],
            font=("Segoe UI", 9, "bold"), padx=10, pady=6, anchor="w", justify=tk.LEFT,
        )
        self.estimate_label.grid(row=len(parameter_fields), column=0, columnspan=3, sticky="ew", pady=(10, 0))
        return variables

    def _labeled_entry(
        self,
        parent: tk.Frame,
        row: int,
        label: str,
        variable: tk.Variable,
        *,
        browse_type: str | None = None,
    ) -> None:
        tk.Label(
            parent, text=label, bg=COLORS["card"], fg=COLORS["text"],
            font=("Segoe UI", 9, "bold"), anchor="w",
        ).grid(row=row * 2, column=0, columnspan=2, sticky="w", pady=(6 if row else 0, 3))

        ttk.Entry(
            parent, textvariable=variable, style="Settings.TEntry", font=("Segoe UI", 10)
        ).grid(row=row * 2 + 1, column=0, sticky="ew", pady=(0, 4), padx=(0, 8 if browse_type else 0))

        if browse_type:
            command = {
                "exe": lambda v=variable: self._browse_executable(v),
                "directory": lambda v=variable: self._browse_directory(v),
                "json": lambda v=variable: self._browse_json(v),
            }[browse_type]
            self._button(
                parent, "WYBIERZ", command, COLORS["primary_soft"], "#DCEAF4",
                foreground=COLORS["primary"], active_foreground=COLORS["primary"],
                font_size=8, padx=11, pady=6,
            ).grid(row=row * 2 + 1, column=1, sticky="e", pady=(0, 4))
        parent.columnconfigure(0, weight=1)

    def _refresh_estimate(self) -> None:
        if not hasattr(self, "estimate_label"):
            return
        try:
            preview = self._collect_config()
        except (ValueError, ConfigError, AttributeError, tk.TclError):
            self.estimate_label.config(
                text="Szacowany czas testu: nie można policzyć (sprawdź wartości pól)."
            )
            return
        seconds = preview.estimated_test_seconds()
        self.estimate_label.config(
            text=(
                f"Szacowany czas samego testu: {seconds:.1f} s "
                f"(+ okno wyniku {preview.result_window_pass_seconds:g} s dla PASS).\n"
                f"Timeout całego testu: {preview.test_timeout:g} s."
            )
        )

    # ------------------------------------------------------------- Profile

    def _build_profiles(self, parent: ttk.Frame) -> None:
        body = tk.Frame(parent, bg=COLORS["background"])
        body.pack(fill=tk.BOTH, expand=True, padx=16, pady=16)

        list_card = self._card(body)
        list_card.pack(fill=tk.X)
        self._section_title(
            list_card, "Profile napięciowe",
            "Zakresy zgodne ze specyfikacja wyrobu — zmieniaj tylko po zatwierdzeniu przez inżyniera",
        )

        tree_area = tk.Frame(list_card, bg=COLORS["card"])
        tree_area.pack(fill=tk.X, padx=16, pady=(2, 16))
        columns = ("name", "index", "nominal", "range", "current", "times")
        self.profile_tree = ttk.Treeview(
            tree_area, columns=columns, show="headings", height=5,
            style="Settings.Treeview", selectmode="browse",
        )
        headings = ("Nazwa", "Indeks", "Napięcie", "Zakres", "Obciążenie", "Czasy faz")
        widths = (180, 70, 105, 175, 130, 150)
        for column, heading, width in zip(columns, headings, widths):
            self.profile_tree.heading(column, text=heading)
            self.profile_tree.column(column, width=width, anchor=tk.CENTER)
        self.profile_tree.pack(fill=tk.X)
        self.profile_tree.bind("<<TreeviewSelect>>", self._load_selected_profile)

        editor_card = self._card(body)
        editor_card.pack(fill=tk.BOTH, expand=True, pady=(14, 0))
        self._section_title(
            editor_card, "Edycja profilu", "Zmiany w aktywnym profilu zostaną zapisane po zatwierdzeniu"
        )

        editor = tk.Frame(editor_card, bg=COLORS["card"])
        editor.pack(fill=tk.BOTH, expand=True, padx=18, pady=(2, 10))
        for column in range(4):
            editor.columnconfigure(column, weight=1 if column in (1, 3) else 0)

        fields = [
            ("name", "Nazwa"),
            ("index", "Indeks PM125"),
            ("nominal_voltage", "Napięcie nominalne [V]"),
            ("min_voltage", "Napięcie minimalne [V]"),
            ("max_voltage", "Napięcie maksymalne [V]"),
            ("no_load_seconds", "Czas bez obciążenia [s]"),
            ("load_seconds", "Czas z obciążeniem [s]"),
            ("load_current_ma", "Obciążenie [mA]"),
            ("no_load_current_max_ma", "Maks. prąd bez obciążenia [mA]"),
            ("load_current_tolerance_percent", "Tolerancja prądu [%]"),
        ]
        self.profile_vars: dict[str, tk.StringVar] = {}
        for index, (key, label) in enumerate(fields):
            row, column = divmod(index, 2)
            label_column = column * 2
            tk.Label(
                editor, text=label, bg=COLORS["card"], fg=COLORS["text"],
                font=("Segoe UI", 9), anchor="w",
            ).grid(row=row, column=label_column, sticky="w", padx=(0 if column == 0 else 22, 8), pady=7)
            variable = tk.StringVar()
            self.profile_vars[key] = variable
            ttk.Entry(
                editor, textvariable=variable, style="Settings.TEntry", font=("Segoe UI", 10)
            ).grid(row=row, column=label_column + 1, sticky="ew", pady=7)

        actions = tk.Frame(editor_card, bg=COLORS["card"])
        actions.pack(fill=tk.X, padx=18, pady=(0, 16))
        self._button(
            actions, "ZASTOSUJ ZMIANY", self._apply_profile, COLORS["primary"], COLORS["primary_hover"],
            padx=18, pady=8,
        ).pack(side=tk.LEFT)
        self._button(
            actions, "DODAJ PROFIL", self._add_profile, COLORS["success"], COLORS["success_hover"],
            padx=18, pady=8,
        ).pack(side=tk.LEFT, padx=8)
        self._button(
            actions, "USUŃ PROFIL", self._remove_profile, COLORS["error"], COLORS["error_hover"],
            padx=18, pady=8,
        ).pack(side=tk.RIGHT)

        self._refresh_profile_tree()
        if self.profiles:
            self.profile_tree.selection_set("0")
            self._load_selected_profile()

    def _refresh_profile_tree(self) -> None:
        for item in self.profile_tree.get_children():
            self.profile_tree.delete(item)
        for index, profile in enumerate(self.profiles):
            self.profile_tree.insert(
                "", tk.END, iid=str(index),
                values=(
                    profile.name,
                    profile.index,
                    f"{profile.nominal_voltage:g} V",
                    f"{profile.min_voltage:g} – {profile.max_voltage:g} V",
                    f"{profile.load_current_ma} mA",
                    f"{profile.no_load_seconds:g} / {profile.load_seconds:g} s",
                ),
            )

    def _load_selected_profile(self, _event=None) -> None:
        selection = self.profile_tree.selection()
        if not selection:
            return
        profile = self.profiles[int(selection[0])]
        for key, variable in self.profile_vars.items():
            variable.set(str(getattr(profile, key)))

    def _apply_profile(self, silent: bool = False) -> None:
        selection = self.profile_tree.selection()
        if not selection:
            if not silent:
                messagebox.showwarning("Profile", "Wybierz profil do edycji.", parent=self.window)
            return
        try:
            profile = TestProfile(
                name=self.profile_vars["name"].get().strip(),
                index=int(self.profile_vars["index"].get()),
                nominal_voltage=_float(self.profile_vars["nominal_voltage"].get()),
                min_voltage=_float(self.profile_vars["min_voltage"].get()),
                max_voltage=_float(self.profile_vars["max_voltage"].get()),
                no_load_seconds=_float(self.profile_vars["no_load_seconds"].get()),
                load_seconds=_float(self.profile_vars["load_seconds"].get()),
                load_current_ma=int(self.profile_vars["load_current_ma"].get()),
                no_load_current_max_ma=int(self.profile_vars["no_load_current_max_ma"].get()),
                load_current_tolerance_percent=_float(
                    self.profile_vars["load_current_tolerance_percent"].get()
                ),
            )
        except ValueError as exc:
            if not silent:
                messagebox.showerror("Profile", f"Nieprawidłowa wartość: {exc}", parent=self.window)
            raise
        self.profiles[int(selection[0])] = profile
        selected = selection[0]
        self._refresh_profile_tree()
        self.profile_tree.selection_set(selected)
        self._refresh_estimate()
        if not silent:
            self._show_inline_notice("Profil został zaktualizowany.", COLORS["success"], COLORS["success_soft"])

    def _add_profile(self) -> None:
        next_index = max((item.index for item in self.profiles), default=0) + 1
        self.profiles.append(TestProfile(f"Profil {next_index}", next_index, 5.0, 4.5, 5.5, 2.5, 2.5, 1000))
        self._refresh_profile_tree()
        selected = str(len(self.profiles) - 1)
        self.profile_tree.selection_set(selected)
        self.profile_tree.see(selected)
        self._load_selected_profile()
        self._refresh_estimate()

    def _remove_profile(self) -> None:
        selection = self.profile_tree.selection()
        if not selection:
            messagebox.showwarning("Profile", "Wybierz profil do usunięcia.", parent=self.window)
            return
        index = int(selection[0])
        profile = self.profiles[index]
        if not messagebox.askyesno(
            "Usuń profil",
            f"Usunąć profil „{profile.name}”?\n\n"
            "Zmiana zestawu profili zakłada nowy plik raportu — stary raport pozostaje bez zmian.",
            parent=self.window,
        ):
            return
        del self.profiles[index]
        self._refresh_profile_tree()
        if self.profiles:
            next_selection = str(min(index, len(self.profiles) - 1))
            self.profile_tree.selection_set(next_selection)
            self._load_selected_profile()
        self._refresh_estimate()

    # --------------------------------------------------------------- HRID

    def _build_hrids(self, parent: ttk.Frame) -> None:
        body = tk.Frame(parent, bg=COLORS["background"])
        body.pack(fill=tk.BOTH, expand=True, padx=16, pady=16)

        card = self._card(body)
        card.pack(fill=tk.BOTH, expand=True)
        self._section_title(card, "Dopuszczeni operatorzy", "Jeden numer HRID w każdym wierszu")

        toolbar = tk.Frame(card, bg=COLORS["card"])
        toolbar.pack(fill=tk.X, padx=18, pady=(2, 8))
        self.hrid_count_label = tk.Label(
            toolbar, text="", bg=COLORS["primary_soft"], fg=COLORS["primary"],
            font=("Segoe UI", 9, "bold"), padx=11, pady=6,
        )
        self.hrid_count_label.pack(side=tk.LEFT)
        tk.Label(
            toolbar, text="Puste wiersze są pomijane, a duplikaty usuwane podczas zapisu.",
            bg=COLORS["card"], fg=COLORS["muted"], font=("Segoe UI", 9),
        ).pack(side=tk.RIGHT)

        text_area = tk.Frame(card, bg=COLORS["card"])
        text_area.pack(fill=tk.BOTH, expand=True, padx=18, pady=(0, 18))
        scrollbar = ttk.Scrollbar(text_area, style="Settings.Vertical.TScrollbar")
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.hrid_text = tk.Text(
            text_area, font=("Consolas", 12), bg=COLORS["entry"], fg=COLORS["text"],
            insertbackground=COLORS["text"], selectbackground=COLORS["primary_soft"],
            selectforeground=COLORS["primary"], relief=tk.FLAT, borderwidth=0,
            padx=14, pady=12, yscrollcommand=scrollbar.set,
        )
        self.hrid_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.config(command=self.hrid_text.yview)
        self.hrid_text.insert("1.0", "\n".join(self.config.valid_hrids))
        self.hrid_text.bind("<KeyRelease>", lambda _event: self._update_hrid_count())
        self._update_hrid_count()

    def _update_hrid_count(self) -> None:
        values = {line.strip() for line in self.hrid_text.get("1.0", tk.END).splitlines() if line.strip()}
        self.hrid_count_label.config(text=f"HRID: {len(values)}")

    # ----------------------------------------------------- liczniki prób

    def _build_attempts(self, parent: ttk.Frame) -> None:
        body = tk.Frame(parent, bg=COLORS["background"])
        body.pack(fill=tk.BOTH, expand=True, padx=16, pady=16)

        card = self._card(body)
        card.pack(fill=tk.BOTH, expand=True)
        self._section_title(
            card, "Liczniki prób numerów seryjnych",
            "Zerowanie licznika odblokowuje ponowny test zasilacza — każda operacja trafia do dziennika audytu",
        )

        toolbar = tk.Frame(card, bg=COLORS["card"])
        toolbar.pack(fill=tk.X, padx=18, pady=(2, 8))
        self.attempts_count_label = tk.Label(
            toolbar, text="", bg=COLORS["primary_soft"], fg=COLORS["primary"],
            font=("Segoe UI", 9, "bold"), padx=11, pady=6,
        )
        self.attempts_count_label.pack(side=tk.LEFT)
        self._button(
            toolbar, "ODSWIEZ", self._refresh_attempts, COLORS["primary"], COLORS["primary_hover"],
            padx=16, pady=7,
        ).pack(side=tk.RIGHT)
        self._button(
            toolbar, "ZERUJ WYBRANY", self._reset_attempt, COLORS["error"], COLORS["error_hover"],
            padx=16, pady=7,
        ).pack(side=tk.RIGHT, padx=(0, 8))

        tree_area = tk.Frame(card, bg=COLORS["card"])
        tree_area.pack(fill=tk.BOTH, expand=True, padx=18, pady=(0, 18))
        scrollbar = ttk.Scrollbar(tree_area, style="Settings.Vertical.TScrollbar")
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        columns = ("serial", "count", "last")
        self.attempts_tree = ttk.Treeview(
            tree_area, columns=columns, show="headings", style="Settings.Treeview",
            selectmode="browse", yscrollcommand=scrollbar.set,
        )
        for column, heading, width, anchor in (
            ("serial", "Numer seryjny", 320, tk.W),
            ("count", "Próby", 100, tk.CENTER),
            ("last", "Ostatnia próba", 220, tk.CENTER),
        ):
            self.attempts_tree.heading(column, text=heading)
            self.attempts_tree.column(column, width=width, anchor=anchor)
        self.attempts_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.config(command=self.attempts_tree.yview)
        self._refresh_attempts()

    def _refresh_attempts(self) -> None:
        for item in self.attempts_tree.get_children():
            self.attempts_tree.delete(item)
        if self.attempts is None:
            self.attempts_count_label.config(text="Liczniki niedostępne")
            return
        entries = self.attempts.entries()
        for serial, count, last in entries:
            self.attempts_tree.insert("", tk.END, iid=serial, values=(serial, count, last or "—"))
        self.attempts_count_label.config(text=f"Numery w rejestrze: {len(entries)}")

    def _reset_attempt(self) -> None:
        if self.attempts is None:
            return
        selection = self.attempts_tree.selection()
        if not selection:
            messagebox.showwarning(
                "Liczniki prób", "Wybierz numer seryjny do wyzerowania.", parent=self.window
            )
            return
        serial = selection[0]
        if not messagebox.askyesno(
            "Liczniki prób",
            f"Wyzerować licznik prób dla numeru {serial}?\n\nZasilacz będzie można testować od nowa.",
            parent=self.window,
        ):
            return
        try:
            self.attempts.reset(serial)
        except OSError as exc:
            messagebox.showerror("Liczniki prób", f"Nie zapisano zmiany: {exc}", parent=self.window)
            return
        logger.info("Panel inżynieryjny: wyzerowano licznik prób dla %s (operator=%s).",
                    serial, self.operator_hrid or "-")
        self.config.append_audit(
            f"reset licznika prób: {serial} (operator={self.operator_hrid or '-'})"
        )
        self._refresh_attempts()
        self._show_inline_notice("Licznik wyzerowany.", COLORS["success"], COLORS["success_soft"])

    # ---------------------------------------------------------------- Logi

    def _build_logs(self, parent: ttk.Frame) -> None:
        body = tk.Frame(parent, bg=COLORS["background"])
        body.pack(fill=tk.BOTH, expand=True, padx=16, pady=16)

        card = self._card(body)
        card.pack(fill=tk.BOTH, expand=True)
        self._section_title(card, "Log aplikacji", "Ostatnie 300 wpisów z najnowszego pliku")

        toolbar = tk.Frame(card, bg=COLORS["card"])
        toolbar.pack(fill=tk.X, padx=18, pady=(2, 8))
        self.log_file_label = tk.Label(
            toolbar, text="", bg=COLORS["card"], fg=COLORS["muted"], font=("Segoe UI", 9), anchor="w"
        )
        self.log_file_label.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self._button(
            toolbar, "ODSWIEZ", self._refresh_logs, COLORS["primary"], COLORS["primary_hover"],
            padx=16, pady=7,
        ).pack(side=tk.RIGHT)

        text_area = tk.Frame(card, bg=COLORS["card"])
        text_area.pack(fill=tk.BOTH, expand=True, padx=18, pady=(0, 18))
        y_scroll = ttk.Scrollbar(text_area, style="Settings.Vertical.TScrollbar")
        y_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        x_scroll = ttk.Scrollbar(text_area, orient=tk.HORIZONTAL)
        x_scroll.pack(side=tk.BOTTOM, fill=tk.X)
        self.log_text = tk.Text(
            text_area, font=("Consolas", 9), wrap=tk.NONE, bg="#111820", fg="#DCE7F0",
            insertbackground="white", selectbackground=COLORS["primary"], relief=tk.FLAT,
            borderwidth=0, padx=12, pady=10, yscrollcommand=y_scroll.set, xscrollcommand=x_scroll.set,
        )
        self.log_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        y_scroll.config(command=self.log_text.yview)
        x_scroll.config(command=self.log_text.xview)
        self._refresh_logs()

    def _refresh_logs(self) -> None:
        self.log_text.config(state=tk.NORMAL)
        self.log_text.delete("1.0", tk.END)
        candidates = sorted(self.config.logs_path.glob("psu19_*.log"))
        if not candidates:
            self.log_file_label.config(text="Brak pliku logu")
            self.log_text.insert(tk.END, "Brak pliku logu.")
            self.log_text.config(state=tk.DISABLED)
            return
        latest = candidates[-1]
        self.log_file_label.config(text=str(latest))
        try:
            lines = latest.read_text(encoding="utf-8", errors="replace").splitlines()[-300:]
            self.log_text.insert(tk.END, "\n".join(lines))
            self.log_text.see(tk.END)
        except OSError as exc:
            self.log_text.insert(tk.END, str(exc))
        self.log_text.config(state=tk.DISABLED)

    # --------------------------------------------------------------- zapis

    def _collect_config(self) -> AppConfig:
        v = self.general_vars
        hrids = sorted(
            {line.strip() for line in self.hrid_text.get("1.0", tk.END).splitlines() if line.strip()}
        )
        return replace(
            self.config,
            console_path=str(v["console_path"].get()).strip(),
            device_serial=str(v["device_serial"].get()).strip() or None,
            reports_directory=str(v["reports_directory"].get()).strip(),
            backup_directory=str(v["backup_directory"].get()).strip(),
            logs_directory=str(v["logs_directory"].get()).strip(),
            raw_directory=str(v["raw_directory"].get()).strip(),
            attempts_file=str(v["attempts_file"].get()).strip(),
            measurement_interval=_float(v["measurement_interval"].get()),
            profile_settle_seconds=_float(v["profile_settle_seconds"].get()),
            load_settle_seconds=_float(v["load_settle_seconds"].get()),
            settling_ignore_seconds=_float(v["settling_ignore_seconds"].get()),
            command_timeout=_float(v["command_timeout"].get()),
            test_timeout=_float(v["test_timeout"].get()),
            minimum_samples=int(v["minimum_samples"].get()),
            maximum_read_errors=int(v["maximum_read_errors"].get()),
            maximum_serial_attempts=int(v["maximum_serial_attempts"].get()),
            serial_minimum_length=int(v["serial_minimum_length"].get()),
            dut_precheck_enabled=bool(v["dut_precheck_enabled"].get()),
            dut_precheck_min_voltage=_float(v["dut_precheck_min_voltage"].get()),
            health_check_seconds=_float(v["health_check_seconds"].get()),
            result_window_pass_seconds=_float(v["result_window_pass_seconds"].get()),
            result_window_fail_seconds=_float(v["result_window_fail_seconds"].get()),
            maximum_csv_rows=int(v["maximum_csv_rows"].get()),
            raw_samples_enabled=bool(v["raw_samples_enabled"].get()),
            raw_retention_days=int(v["raw_retention_days"].get()),
            attempts_retention_days=int(v["attempts_retention_days"].get()),
            log_retention_days=int(v["log_retention_days"].get()),
            debug_logging=bool(v["debug_logging"].get()),
            profiles=list(self.profiles),
            valid_hrids=hrids,
        )

    def _save(self) -> None:
        # Zapis w trakcie testu podmienialby konfiguracje uzywana przez trwajacy pomiar.
        if self.is_busy():
            messagebox.showwarning(
                "Ustawienia",
                "Trwa test zasilacza. Zapisz ustawienia po jego zakończeniu.",
                parent=self.window,
            )
            return
        try:
            if self.profile_tree.selection():
                self._apply_profile(silent=True)
            new_config = self._collect_config()
            changes = self.config.diff(new_config)
            note = (
                f"zapis konfiguracji (operator={self.operator_hrid or '-'}): "
                + ("; ".join(changes) if changes else "brak zmian")
            )
            new_config.save(self.config_path, audit_note=note)
            logger.info("Panel inżynieryjny: %s", note)
            self.on_saved(new_config)
            messagebox.showinfo("Ustawienia", "Konfiguracja została zapisana.", parent=self.window)
            self.window.destroy()
        except (ValueError, ConfigError, OSError) as exc:
            messagebox.showerror("Ustawienia", str(exc), parent=self.window)

    # ----------------------------------------------------------- narzędzia

    def _show_inline_notice(self, text: str, color: str, background: str) -> None:
        notice = tk.Label(
            self.window, text=text, bg=background, fg=color,
            font=("Segoe UI", 9, "bold"), padx=14, pady=7,
        )
        notice.place(relx=0.5, rely=0.105, anchor="n")
        self.window.after(1600, notice.destroy)

    def _card(self, parent: tk.Widget) -> tk.Frame:
        return tk.Frame(
            parent, bg=COLORS["card"], highlightbackground=COLORS["border"], highlightthickness=1
        )

    def _section_title(self, parent: tk.Widget, title: str, subtitle: str) -> None:
        area = tk.Frame(parent, bg=COLORS["card"])
        area.pack(fill=tk.X, padx=18, pady=(15, 8))
        tk.Label(
            area, text=title, bg=COLORS["card"], fg=COLORS["text"],
            font=("Segoe UI", 12, "bold"), anchor="w",
        ).pack(anchor="w")
        tk.Label(
            area, text=subtitle, bg=COLORS["card"], fg=COLORS["muted"],
            font=("Segoe UI", 8), anchor="w", justify=tk.LEFT,
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
            relief=tk.FLAT, borderwidth=0, font=("Segoe UI", font_size, "bold"),
            padx=padx, pady=pady, cursor="hand2",
        )
        button.bind("<Enter>", lambda _event: button.config(bg=hover_color))
        button.bind("<Leave>", lambda _event: button.config(bg=color))
        return button

    def _center_window(self, parent: tk.Tk) -> None:
        self.window.update_idletasks()
        width = max(self.window.winfo_width(), 1120)
        height = max(self.window.winfo_height(), 760)
        parent.update_idletasks()
        x = parent.winfo_rootx() + max(0, (parent.winfo_width() - width) // 2)
        y = parent.winfo_rooty() + max(0, (parent.winfo_height() - height) // 2)
        self.window.geometry(f"{width}x{height}+{x}+{y}")

    @staticmethod
    def _browse_executable(variable: tk.Variable) -> None:
        path = filedialog.askopenfilename(filetypes=(("Executable", "*.exe"), ("All files", "*.*")))
        if path:
            variable.set(path)

    @staticmethod
    def _browse_directory(variable: tk.Variable) -> None:
        path = filedialog.askdirectory()
        if path:
            variable.set(path)

    @staticmethod
    def _browse_json(variable: tk.Variable) -> None:
        path = filedialog.asksaveasfilename(defaultextension=".json", filetypes=(("JSON", "*.json"),))
        if path:
            variable.set(path)


def _float(value: str) -> float:
    return float(str(value).replace(",", "."))
