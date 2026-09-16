"""Warstwa sprzętowa: rzeczywisty PM125 i symulator developerski."""
from __future__ import annotations

import logging
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Protocol

from config import resolve_path
from models import Measurement

logger = logging.getLogger(__name__)

#: Slowa w odpowiedzi konsoli, które oznaczaja odrzucenie komendy mimo kodu 0.
_ERROR_TOKENS = ("ERROR", "FAILED", "FAILURE", "INVALID", "NOT FOUND", "NO DEVICE", "TIMEOUT")


class Hardware(Protocol):
    connected: bool
    last_error: str

    def connect(self) -> bool: ...
    def disconnect(self) -> None: ...
    def set_profile(self, index: int) -> bool: ...
    def set_load(self, current_ma: int) -> bool: ...
    def read_measurement(self) -> Measurement | None: ...
    def safe_reset(self) -> bool: ...
    def ping(self) -> bool: ...


class PM125Client:
    """Interfejs CLI PM125. Utworzenie obiektu nie wymaga podłączonego urządzenia."""

    def __init__(self, console_path: str, device_serial: str | None, command_timeout: float) -> None:
        self.console_path = str(resolve_path(console_path))
        self.device_serial = device_serial or "Any"
        self.command_timeout = command_timeout
        self.connected = False
        self.last_error = ""
        self._lock = threading.RLock()

    def reconfigure(self, console_path: str, device_serial: str | None, command_timeout: float) -> None:
        with self._lock:
            self.disconnect()
            self.console_path = str(resolve_path(console_path))
            self.device_serial = device_serial or "Any"
            self.command_timeout = command_timeout

    # ------------------------------------------------------------ połączenie

    def connect(self) -> bool:
        with self._lock:
            executable = Path(self.console_path)
            if not executable.is_file():
                self._set_error(f"Nie znaleziono USBPDConsole.exe: {executable}", fatal=True)
                return False
            output = self._run("-c")
            self.connected = bool(output and "STATUS:CONNECTED" in output.upper())
            if not self.connected and not self.last_error:
                self.last_error = "PM125 nie zgłosił statusu CONNECTED."
            if self.connected:
                self.last_error = ""
                logger.info("Połączono z PM125, device=%s", self.device_serial)
            return self.connected

    def disconnect(self) -> None:
        with self._lock:
            if self.connected:
                self.safe_reset()
            self.connected = False

    def ping(self) -> bool:
        """Lekka kontrola obecności urządzenia między testami."""
        with self._lock:
            output = self._run("-c")
            alive = bool(output and "STATUS:CONNECTED" in output.upper())
            self.connected = alive
            if not alive and not self.last_error:
                self.last_error = "PM125 nie odpowiada na kontrolę obecności."
            return alive

    # --------------------------------------------------------------- komendy

    def set_profile(self, index: int) -> bool:
        if index < 1:
            self._set_error(f"Nieprawidłowy indeks profilu: {index}")
            return False
        output = self._run("-v", str(index))
        if output is None:
            return False
        if _looks_like_error(output):
            self._set_error(f"PM125 odrzucił ustawienie profilu {index}: {output.strip()[:200]}")
            return False
        return True

    def set_load(self, current_ma: int) -> bool:
        if not 0 <= current_ma <= 5000:
            self._set_error(f"Obciążenie {current_ma} mA jest poza zakresem 0-5000 mA.")
            return False
        output = self._run("-q", str(current_ma))
        if output is None:
            return False
        if _looks_like_error(output):
            self._set_error(f"PM125 odrzucił ustawienie obciążenia {current_ma} mA: {output.strip()[:200]}")
            return False
        return True

    def read_measurement(self) -> Measurement | None:
        output = self._run("-s")
        if output is None:
            return None

        voltage = _extract_voltage(output)
        current = _extract_current(output)
        if voltage is None or current is None:
            self._set_error(f"Nie można odczytać napięcia lub prądu z odpowiedzi: {output!r}")
            return None
        return Measurement(0.0, voltage, current)

    def safe_reset(self) -> bool:
        """Próbuje pozostawić tester w bezpiecznym stanie: 5 V i 0 mA."""
        with self._lock:
            load_ok = self.set_load(0)
            profile_ok = self.set_profile(1)
            if not (load_ok and profile_ok):
                logger.error("Nie udało się przywrócić PM125 do 5 V / 0 mA: %s", self.last_error)
            return load_ok and profile_ok

    # ------------------------------------------------------------ wewnętrzne

    def _run(self, *arguments: str) -> str | None:
        with self._lock:
            command = [self.console_path, "-d", self.device_serial, *arguments]
            startupinfo = None
            creationflags = 0
            if sys.platform == "win32":
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                startupinfo.wShowWindow = subprocess.SW_HIDE
                creationflags = subprocess.CREATE_NO_WINDOW
            try:
                completed = subprocess.run(
                    command,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=self.command_timeout,
                    startupinfo=startupinfo,
                    creationflags=creationflags,
                    check=False,
                )
            except subprocess.TimeoutExpired:
                # Brak odpowiedzi w czasie = realna utrata łączności z urzadzeniem.
                self._set_error(f"Timeout komendy PM125: {' '.join(arguments)}", fatal=True)
                return None
            except OSError as exc:
                self._set_error(f"Nie można uruchomić USBPDConsole.exe: {exc}", fatal=True)
                return None

            stdout = completed.stdout.strip()
            stderr = completed.stderr.strip()
            if completed.returncode != 0:
                # Błąd pojedynczej komendy nie oznacza jeszcze utraty urządzenia.
                self._set_error(
                    f"USBPDConsole zakończył się kodem {completed.returncode}: "
                    f"{stderr or stdout or 'brak opisu'}"
                )
                return None
            self.last_error = ""
            self.connected = True  # udana komenda potwierdza obecność urządzenia
            logger.debug("PM125 %s -> %s", arguments, stdout)
            return stdout

    def _set_error(self, message: str, fatal: bool = False) -> None:
        """fatal=True oznacza utratę łączności; błąd pojedynczej komendy jej nie zrywa."""
        self.last_error = message
        if fatal:
            self.connected = False
        logger.error("%s%s", message, " [utrata połączenia]" if fatal else "")


class SimulatedPM125:
    """Symulator uruchamiany wyłącznie parametrem --simulate."""

    def __init__(self, mode: str = "pass", ramp_seconds: float = 0.4) -> None:
        self.mode = mode
        self.ramp_seconds = ramp_seconds
        self.connected = False
        self.last_error = ""
        self.profile_index = 1
        self.load_ma = 0
        self._lock = threading.RLock()
        self._started = time.monotonic()
        self._load_changed_at = time.monotonic()

    def connect(self) -> bool:
        with self._lock:
            self.connected = self.mode != "disconnected"
            self.last_error = "" if self.connected else "Symulator ustawiony jako odłączony."
            return self.connected

    def disconnect(self) -> None:
        with self._lock:
            self.connected = False
            self.load_ma = 0

    def ping(self) -> bool:
        return self.connected

    def set_profile(self, index: int) -> bool:
        with self._lock:
            if not self.connected:
                self.last_error = "Symulator nie jest połączony."
                return False
            self.profile_index = index
            self._load_changed_at = time.monotonic()
            return True

    def set_load(self, current_ma: int) -> bool:
        with self._lock:
            if not self.connected:
                self.last_error = "Symulator nie jest połączony."
                return False
            if self.mode == "load_error" and current_ma > 0:
                self.last_error = "Symulowany błąd ustawienia obciążenia."
                return False
            self.load_ma = current_ma
            self._load_changed_at = time.monotonic()
            return True

    def read_measurement(self) -> Measurement | None:
        with self._lock:
            if not self.connected or self.mode == "read_error":
                self.last_error = "Symulowany błąd odczytu."
                return None
            if self.mode == "no_dut":
                return Measurement(time.monotonic() - self._started, 0.0, 0.0)
            nominal = {1: 5.0, 2: 9.0, 3: 12.0, 4: 15.0}.get(self.profile_index, 5.0)
            wobble = ((time.monotonic() * 100) % 5 - 2) * 0.002
            voltage = nominal + wobble
            # Rampa prądu po skoku obciążenia - odwzorowuje realny transjent.
            elapsed = time.monotonic() - self._load_changed_at
            factor = min(1.0, elapsed / self.ramp_seconds) if self.ramp_seconds > 0 else 1.0
            current_a = (self.load_ma / 1000.0) * factor
            if self.mode == "voltage_fail":
                voltage *= 0.80
            if self.mode == "current_fail" and self.load_ma:
                current_a = 0.0
            return Measurement(time.monotonic() - self._started, voltage, current_a)

    def safe_reset(self) -> bool:
        with self._lock:
            if not self.connected:
                return False
            self.load_ma = 0
            self.profile_index = 1
            self._load_changed_at = time.monotonic()
            return True


def _looks_like_error(output: str) -> bool:
    upper = output.upper()
    if "STATUS:CONNECTED" in upper:
        return False
    return any(token in upper for token in _ERROR_TOKENS)


def _extract_value(text: str, pattern: str) -> float | None:
    match = re.search(pattern, text, flags=re.IGNORECASE)
    if not match:
        return None
    return float(match.group(1).replace(",", "."))


def _extract_voltage(text: str) -> float | None:
    """Zwraca napięcie w woltach. Akceptuje odpowiedź w mV oraz w V."""
    millivolts = _extract_value(text, r"VOLTAGE\s*[:=]?\s*(-?\d+(?:[.,]\d+)?)\s*mV")
    if millivolts is not None:
        return millivolts / 1000.0
    volts = _extract_value(text, r"VOLTAGE\s*[:=]?\s*(-?\d+(?:[.,]\d+)?)\s*V(?![A-Z])")
    return volts


def _extract_current(text: str) -> float | None:
    """Zwraca prąd w amperach. Akceptuje odpowiedź w mA oraz w A."""
    milliamps = _extract_value(text, r"MEASURED\s+CURRENT\s*[:=]?\s*(-?\d+(?:[.,]\d+)?)\s*mA")
    if milliamps is not None:
        return milliamps / 1000.0
    amps = _extract_value(text, r"MEASURED\s+CURRENT\s*[:=]?\s*(-?\d+(?:[.,]\d+)?)\s*A(?![A-Z])")
    return amps
