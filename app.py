"""Punkt startowy PSU19 Tester v5 (jedno lub kilka stanowisk)."""
from __future__ import annotations

import argparse
import logging
import sys
import tkinter as tk
from pathlib import Path
from tkinter import messagebox

from config import AppConfig, ConfigError, app_directory, stations_from_devices
from hardware import find_devices
from logging_utils import configure_logging
from ui import TesterApp, build_hardware

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
    parser.add_argument(
        "--simulate-delay",
        type=float,
        default=0.0,
        help="Opóźnienie jednej komendy w symulatorze [s] — odwzorowuje wolną konsolę PM125",
    )
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

    stations = config.active_stations()
    logger.info(
        "Stanowiska: %s", ", ".join(f"{item.name}={item.address}" for item in stations)
    )
    if not args.simulate:
        detected = find_devices(config.console_path)
        logger.info("Testery widoczne dla konsoli: %s", detected or "brak")
        config = _offer_detected_stations(config, detected, config_path, root)
        stations = config.active_stations()
        _warn_about_missing_devices(stations, detected, root)

    hardware = build_hardware(config, args.simulate, args.simulate_mode, args.simulate_delay)

    root.deiconify()
    TesterApp(root, config, hardware, config_path, simulation_mode=args.simulate)
    root.mainloop()
    return 0


def _offer_detected_stations(
    config: AppConfig, detected: list[tuple[int, str]], config_path: Path, root: tk.Tk
) -> AppConfig:
    """Proponuje konfigurację kanałów, gdy podłączono więcej testerów niż skonfigurowano.

    Dzięki temu dołożenie drugiego testera wystarczy potwierdzić przy starcie — bez
    wchodzenia do panelu inżynieryjnego i bez ponownego uruchamiania aplikacji.
    """
    stations = config.active_stations()
    if len(detected) <= len(stations):
        return config
    configured = {
        station.device_serial.upper() for station in stations if station.device_serial
    }
    proposal = stations_from_devices(detected)
    listing = "\n".join(f"  • {item.name} — {item.device_serial}" for item in proposal)
    answer = messagebox.askyesno(
        "PSU19 Tester — wykryto testery",
        f"Podłączonych testerów: {len(detected)}, a skonfigurowanych stanowisk: "
        f"{len(stations)}.\n\nSkonfigurować wszystkie wykryte testery jako osobne "
        f"stanowiska?\n\n{listing}\n\n"
        "Nazwy i przypisanie można zmienić w panelu inżynieryjnym (zakładka Stanowiska).",
        parent=root,
    )
    if not answer:
        logger.info("Operator odrzucił propozycję konfiguracji wykrytych testerów.")
        return config
    config.stations = proposal
    try:
        config.save(
            config_path,
            audit_note=(
                "automatyczna konfiguracja stanowisk z wykrytych testerów: "
                + ", ".join(f"{item.name}={item.device_serial}" for item in proposal)
            ),
        )
        logger.info("Zapisano konfigurację %s stanowisk.", len(proposal))
    except ConfigError as exc:
        logger.error("Nie zapisano konfiguracji stanowisk: %s", exc)
        messagebox.showerror(
            "PSU19 Tester — stanowiska",
            f"Nie udało się zapisać konfiguracji:\n{exc}",
            parent=root,
        )
    if configured:
        logger.info("Poprzednio skonfigurowane numery seryjne: %s", ", ".join(sorted(configured)))
    return config


def _warn_about_missing_devices(stations, detected, root: tk.Tk) -> None:
    """Ostrzega, gdy skonfigurowany numer seryjny testera nie jest widoczny w systemie."""
    if not detected:
        return
    available = {serial.upper() for _index, serial in detected}
    missing = [
        station.name
        for station in stations
        if station.device_serial and station.device_serial.upper() not in available
    ]
    if not missing:
        return
    logger.warning("Nie znaleziono testerów dla stanowisk: %s", ", ".join(missing))
    messagebox.showwarning(
        "PSU19 Tester — testery",
        "Nie znaleziono testera dla stanowiska: " + ", ".join(missing) + ".\n\n"
        "Widoczne numery seryjne: " + ", ".join(sorted(available)) + ".\n"
        "Sprawdź listę stanowisk w panelu inżynieryjnym.",
        parent=root,
    )


if __name__ == "__main__":
    sys.exit(main())
