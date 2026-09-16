"""Konfiguracja logowania do pliku i konsoli developerskiej."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from pathlib import Path

from config import AppConfig


def configure_logging(config: AppConfig, console: bool = False, force_debug: bool = False) -> Path:
    """Domyślnie plik logu dostaje poziom INFO.

    DEBUG (log każdej komendy PM125) włącza się polem `debug_logging` w konfiguracji
    albo parametrem --debug. Przy 20 Hz próbkowania DEBUG generuje setki wierszy na
    test, więc nie jest ustawieniem produkcyjnym.
    """
    config.logs_path.mkdir(parents=True, exist_ok=True)
    _remove_old_logs(config.logs_path, config.log_retention_days)
    log_path = config.logs_path / f"psu19_{datetime.now():%Y%m%d}.log"

    level = logging.DEBUG if (force_debug or config.debug_logging) else logging.INFO
    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    new_handlers: list[logging.Handler] = []
    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(formatter)
    file_handler.setLevel(level)
    new_handlers.append(file_handler)

    if console:
        stream = logging.StreamHandler()
        stream.setFormatter(formatter)
        stream.setLevel(logging.INFO)
        new_handlers.append(stream)

    root = logging.getLogger()
    root.setLevel(level)
    old_handlers = list(root.handlers)
    for handler in old_handlers:
        root.removeHandler(handler)
    for handler in new_handlers:
        root.addHandler(handler)
    for handler in old_handlers:
        handler.close()
    return log_path


def _remove_old_logs(directory: Path, retention_days: int) -> None:
    cutoff = datetime.now() - timedelta(days=retention_days)
    for path in directory.glob("psu19_*.log"):
        try:
            modified = datetime.fromtimestamp(path.stat().st_mtime)
            if modified < cutoff:
                path.unlink()
        except OSError:
            pass
