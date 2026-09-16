"""Punkt startowy PSU19 Tester v4."""
from __future__ import annotations

import argparse
import logging
import sys
import tkinter as tk
from pathlib import Path
from tkinter import messagebox

from config import AppConfig, ConfigError, app_directory
from hardware import PM125Client, SimulatedPM125
from logging_utils import configure_logging
from ui import TesterApp

logger = logging.getLogger(__name__)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="PSU19 Tester")
    parser.add_argument("--config", default="test_config.json", help="Ścieżka do konfiguracji JSON")
    parser.add_argument("--simulate", action="store_true", help="Uruchom bez PM125 w trybie symulatora")
    parser.add_argument(
        "--simulate-mode",
        choices=("pass", "voltage_fail", "current_fail", "read_error", "load_error", "disconnected", "no_dut"),
        default="pass",
    )
    parser.add_argument("--debug", action="store_true", help="Log DEBUG (każda komenda PM125)")
    return parser.parse_args()


def load_config(config_path: Path, root: tk.Tk) -> AppConfig | None:
    """Wczytuje konfiguracje; przy błędzie proponuje start na ustawieniach domyślnych."""
    try:
        config, warnings = AppConfig.load(config_path)
        for warning in warnings:
            logger.warning("Konfiguracja: %s", warning)
        return config
    except ConfigError as exc:
        use_defaults = messagebox.askyesno(
            "PSU19 Tester — konfiguracja",
            f"{exc}\n\nUruchomic aplikację na ustawieniach domyślnych?\n"
            "Plik konfiguracji nie zostanie nadpisany, dopóki nie zapiszesz ustawień w panelu.",
            parent=root,
        )
        if not use_defaults:
            return None
        logger.error("Start na konfiguracji domyślnej po błędzie pliku: %s", exc)
        return AppConfig()


def main() -> int:
    args = parse_args()
    config_path = Path(args.config)
    if not config_path.is_absolute():
        config_path = (app_directory() / config_path).resolve()

    root = tk.Tk()
    root.withdraw()

    config = load_config(config_path, root)
    if config is None:
        root.destroy()
        return 2

    try:
        configure_logging(config, console=args.simulate, force_debug=args.debug)
    except OSError as exc:
        messagebox.showerror(
            "PSU19 Tester — logi", f"Nie można uruchomić logowania:\n{exc}", parent=root
        )
        root.destroy()
        return 3

    hardware = (
        SimulatedPM125(args.simulate_mode)
        if args.simulate
        else PM125Client(config.console_path, config.device_serial, config.command_timeout)
    )

    root.deiconify()
    TesterApp(root, config, hardware, config_path, simulation_mode=args.simulate)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
