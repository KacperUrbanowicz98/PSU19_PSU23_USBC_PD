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

#: Kody powrotu USBPDConsole V1.0.1016 (sekcja "Return Codes" w pomocy konsoli).
RETURN_CODES = {
    0: "OK",
    1: "nieprawidłowa komenda",
    2: "nie wykryto urządzenia",
    3: "nie udało się połączyć z testerem",
    4: "nieprawidłowy profil",
    5: "komenda nieobsługiwana przez ten model testera",
    6: "wymagana aktualizacja firmware testera",
}
#: Kody oznaczające realną utratę testera — tylko one zrywają połączenie.
DEVICE_LOST_CODES = frozenset({2, 3})


def describe_code(code: int) -> str:
    return RETURN_CODES.get(code, f"kod {code}")


def _hidden_window_flags():
    """Parametry ukrywające okno konsoli na Windows."""
    if sys.platform != "win32":
        return None, 0
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = subprocess.SW_HIDE
    return startupinfo, subprocess.CREATE_NO_WINDOW


def find_devices(console_path: str, timeout: float = 15.0) -> list[tuple[int, str]]:
    """Zwraca listę (indeks, numer seryjny) testerów widocznych dla konsoli (komenda -f).

    Format odpowiedzi USBPDConsole V1.0.1016 to wiersze postaci "0=PMPD80LLAD".
    """
    executable = Path(str(resolve_path(console_path)))
    if not executable.is_file():
        logger.error("Nie znaleziono USBPDConsole.exe: %s", executable)
        return []
    startupinfo, creationflags = _hidden_window_flags()
    try:
        completed = subprocess.run(
            [str(executable), "-f"], capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=timeout, startupinfo=startupinfo,
            creationflags=creationflags, check=False,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.error("Nie udało się wykryć testerów: %s", exc)
        return []
    devices: list[tuple[int, str]] = []
    for line in (completed.stdout or "").splitlines():
        match = re.match(r"\s*(\d+)\s*=\s*([A-Za-z0-9]+)\s*$", line)
        if match:
            devices.append((int(match.group(1)), match.group(2).upper()))
    return devices


class Hardware(Protocol):
    connected: bool
    last_error: str

    def connect(self) -> bool: ...
    def disconnect(self) -> None: ...
    def set_profile(self, index: int, force: bool = False) -> bool: ...
    def set_load(self, current_ma: int, force: bool = False) -> bool: ...
    def read_measurement(self) -> Measurement | None: ...
    def safe_reset(self) -> bool: ...
    def ping(self) -> bool: ...
    def forget_state(self) -> None: ...
    def dut_profile_count(self) -> int | None: ...
    def read_configuration(self) -> str | None: ...

    average_command_seconds: float


class PM125Client:
    """Interfejs CLI PM125. Utworzenie obiektu nie wymaga podłączonego urządzenia."""

    def __init__(self, console_path: str, device_serial: str | None, command_timeout: float) -> None:
        self.console_path = str(resolve_path(console_path))
        self.device_serial = device_serial or "Any"
        self.command_timeout = command_timeout
        self.connected = False
        self.last_error = ""
        self._lock = threading.RLock()
        self._known_profile: int | None = None
        self._known_load: int | None = None
        self._command_total_seconds = 0.0
        self._command_count = 0

    def reconfigure(self, console_path: str, device_serial: str | None, command_timeout: float) -> None:
        with self._lock:
            self.disconnect()
            self.forget_state()
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
            # Konsola zwraca na -c status DUT: "STATUS:NOT CONNECTED" z kodem 0 oznacza
            # sprawny tester z pustym gniazdem. O dostępności testera świadczy kod powrotu.
            self.connected = output is not None and "STATUS:" in output.upper()
            if not self.connected and not self.last_error:
                self.last_error = "Tester PM125 nie odpowiedział na zapytanie o status."
            if self.connected:
                self.last_error = ""
                logger.info(
                    "Tester PM125 dostępny (device=%s), zasilacz w gnieździe: %s",
                    self.device_serial, "tak" if self._dut_in_output(output) else "nie",
                )
            return self.connected

    @staticmethod
    def _dut_in_output(output: str | None) -> bool:
        upper = (output or "").upper()
        return "STATUS:" in upper and "NOT CONNECTED" not in upper and "CONNECTED" in upper

    def dut_present(self) -> bool | None:
        """True/False gdy tester odpowiedział, None gdy nie udało się zapytać."""
        output = self._run("-c")
        if output is None:
            return None
        return self._dut_in_output(output)

    def dut_profile_count(self) -> int | None:
        """Liczba profili PD deklarowanych przez zasilacz (-p). 0 = brak wyrobu w gnieździe."""
        output = self._run("-p")
        if output is None:
            return None
        match = re.search(r"NUM\s+PROFILES\s*[:=]?\s*(\d+)", output, flags=re.IGNORECASE)
        if match is None:
            self._set_error(f"Nie można odczytać liczby profili z odpowiedzi: {output!r}")
            return None
        return int(match.group(1))

    def read_configuration(self) -> str | None:
        """Konfiguracja testera (-r) — do porównania kanałów między sobą."""
        return self._run("-r")

    def disconnect(self) -> None:
        with self._lock:
            if self.connected:
                self.safe_reset()
            self.connected = False
            self._known_profile = None
            self._known_load = None

    def ping(self) -> bool:
        """Lekka kontrola obecności TESTERA między testami (nie zasilacza)."""
        with self._lock:
            output = self._run("-c")
            alive = output is not None and "STATUS:" in output.upper()
            self.connected = alive
            if not alive and not self.last_error:
                self.last_error = "Tester PM125 nie odpowiada na kontrolę obecności."
            return alive

    # --------------------------------------------------------------- komendy

    def set_profile(self, index: int, force: bool = False) -> bool:
        if index < 1:
            self._set_error(f"Nieprawidłowy indeks profilu: {index}")
            return False
        # Kazde wywolanie konsoli kosztuje kilkaset ms - nie powtarzamy stanu, ktory juz jest ustawiony.
        if not force and self._known_profile == index:
            return True
        output = self._run("-v", str(index))
        if output is None:
            self._known_profile = None
            return False
        if _looks_like_error(output):
            self._known_profile = None
            self._set_error(f"PM125 odrzucił ustawienie profilu {index}: {output.strip()[:200]}")
            return False
        self._known_profile = index
        return True

    def set_load(self, current_ma: int, force: bool = False) -> bool:
        if not 0 <= current_ma <= 5000:
            self._set_error(f"Obciążenie {current_ma} mA jest poza zakresem 0-5000 mA.")
            return False
        if not force and self._known_load == current_ma:
            return True
        output = self._run("-q", str(current_ma))
        if output is None:
            self._known_load = None
            return False
        if _looks_like_error(output):
            self._known_load = None
            self._set_error(f"PM125 odrzucił ustawienie obciążenia {current_ma} mA: {output.strip()[:200]}")
            return False
        self._known_load = current_ma
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
        """Próbuje pozostawić tester w bezpiecznym stanie: 5 V i 0 mA.

        Zawsze wysyła obie komendy (force), nawet gdy stan wydaje się już poprawny —
        bezpieczeństwo stanowiska nie może opierać się na zapamiętanym stanie.
        """
        with self._lock:
            load_ok = self.set_load(0, force=True)
            profile_ok = self.set_profile(1, force=True)
            if not (load_ok and profile_ok):
                logger.error("Nie udało się przywrócić PM125 do 5 V / 0 mA: %s", self.last_error)
            return load_ok and profile_ok

    @property
    def average_command_seconds(self) -> float:
        """Średni czas jednego wywołania konsoli — podstawa planowania czasu testu."""
        with self._lock:
            if not self._command_count:
                return 0.0
            return self._command_total_seconds / self._command_count

    def reset_command_statistics(self) -> None:
        with self._lock:
            self._command_total_seconds = 0.0
            self._command_count = 0

    def forget_state(self) -> None:
        """Kasuje zapamiętany stan urządzenia (po rekonfiguracji lub utracie łączności)."""
        with self._lock:
            self._known_profile = None
            self._known_load = None

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
            started = time.monotonic()
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
                # Brak odpowiedzi w czasie = realna utrata łączności z urządzeniem.
                self._set_error(f"Timeout komendy PM125: {' '.join(arguments)}", fatal=True)
                return None
            except OSError as exc:
                self._set_error(f"Nie można uruchomić USBPDConsole.exe: {exc}", fatal=True)
                return None
            finally:
                self._command_total_seconds += time.monotonic() - started
                self._command_count += 1

            stdout = completed.stdout.strip()
            stderr = completed.stderr.strip()
            if completed.returncode != 0:
                # Kody 2 i 3 to realna utrata testera; pozostałe dotyczą samej komendy.
                fatal = completed.returncode in DEVICE_LOST_CODES
                self._set_error(
                    f"USBPDConsole: {describe_code(completed.returncode)} "
                    f"(kod {completed.returncode}) — {stderr or stdout or 'brak opisu'}",
                    fatal=fatal,
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
            self._known_profile = None
            self._known_load = None
        logger.error("%s%s", message, " [utrata połączenia]" if fatal else "")


class SimulatedPM125:
    """Symulator uruchamiany wyłącznie parametrem --simulate."""

    def __init__(
        self, mode: str = "pass", ramp_seconds: float = 0.4, command_delay: float = 0.0
    ) -> None:
        self.mode = mode
        self.ramp_seconds = ramp_seconds
        #: Opóźnienie jednej komendy — odwzorowuje koszt uruchomienia USBPDConsole.exe.
        self.command_delay = command_delay
        self.connected = False
        self.last_error = ""
        self.profile_index = 1
        self.load_ma = 0
        self._lock = threading.RLock()
        self._started = time.monotonic()
        self._load_changed_at = time.monotonic()
        self._known_profile: int | None = None
        self._known_load: int | None = None
        self._command_total_seconds = 0.0
        self._command_count = 0

    def _spend_command(self) -> None:
        if self.command_delay > 0:
            time.sleep(self.command_delay)
        self._command_total_seconds += self.command_delay
        self._command_count += 1

    @property
    def average_command_seconds(self) -> float:
        if not self._command_count:
            return 0.0
        return self._command_total_seconds / self._command_count

    def reset_command_statistics(self) -> None:
        self._command_total_seconds = 0.0
        self._command_count = 0

    def forget_state(self) -> None:
        self._known_profile = None
        self._known_load = None

    def connect(self) -> bool:
        with self._lock:
            self.connected = self.mode != "disconnected"
            self.last_error = "" if self.connected else "Symulator ustawiony jako odłączony."
            return self.connected

    def disconnect(self) -> None:
        with self._lock:
            self.connected = False
            self.load_ma = 0
            self.forget_state()

    def ping(self) -> bool:
        return self.connected

    def dut_present(self) -> bool | None:
        if not self.connected:
            return None
        return self.mode != "no_dut"

    def dut_profile_count(self) -> int | None:
        if not self.connected:
            return None
        self._spend_command()
        return 0 if self.mode == "no_dut" else 4

    def read_configuration(self) -> str | None:
        if not self.connected:
            return None
        return (
            "LOOPBACK PORT: ENABLED\nMAX SDP CURRENT: 500mA\nESTIMATE VBUS: DISABLED\n"
            "CABLE RESISTANCE: 80 Ohms\nDEFAULT VOLTAGE: 5000mV\nDEFAULT LOAD: 0mA\n"
            "DEFAULT PROFILE INDEX: AUTO\nOPERATING CURRENT: MAX\nSINK CAPABILITY: NONE\n"
            "HOLD LOAD ON VOLTAGE CHANGES: DISABLED"
        )

    def set_profile(self, index: int, force: bool = False) -> bool:
        with self._lock:
            if not self.connected:
                self.last_error = "Symulator nie jest połączony."
                return False
            if not force and self._known_profile == index:
                return True
            self._spend_command()
            self.profile_index = index
            self._known_profile = index
            self._load_changed_at = time.monotonic()
            return True

    def set_load(self, current_ma: int, force: bool = False) -> bool:
        with self._lock:
            if not self.connected:
                self.last_error = "Symulator nie jest połączony."
                return False
            if not force and self._known_load == current_ma:
                return True
            if self.mode == "load_error" and current_ma > 0:
                self.last_error = "Symulowany błąd ustawienia obciążenia."
                self._known_load = None
                return False
            self._spend_command()
            self.load_ma = current_ma
            self._known_load = current_ma
            self._load_changed_at = time.monotonic()
            return True

    def read_measurement(self) -> Measurement | None:
        with self._lock:
            if not self.connected or self.mode == "read_error":
                self.last_error = "Symulowany błąd odczytu."
                return None
            self._spend_command()
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
            self._spend_command()
            self._spend_command()
            self.load_ma = 0
            self.profile_index = 1
            self._known_load = 0
            self._known_profile = 1
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
