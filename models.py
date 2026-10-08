"""Modele domenowe aplikacji testera zasilaczy USB-C PD."""
from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import Enum
from typing import Iterable


class ProfileStatus(str, Enum):
    PENDING = "PENDING"
    PASS = "PASS"
    FAIL = "FAIL"
    PROFILE_ERROR = "PROFILE_ERROR"
    LOAD_ERROR = "LOAD_ERROR"
    MEASUREMENT_ERROR = "MEASUREMENT_ERROR"
    NO_DATA = "NO_DATA"
    TIMEOUT = "TIMEOUT"
    CANCELLED = "CANCELLED"
    SAFE_RESET_ERROR = "SAFE_RESET_ERROR"


#: Statusy profilu oznaczajace problem stanowiska, a nie wade wyrobu.
STATION_PROBLEM_STATUSES = frozenset(
    {
        ProfileStatus.PROFILE_ERROR,
        ProfileStatus.LOAD_ERROR,
        ProfileStatus.MEASUREMENT_ERROR,
        ProfileStatus.NO_DATA,
        ProfileStatus.TIMEOUT,
        ProfileStatus.CANCELLED,
        ProfileStatus.SAFE_RESET_ERROR,
        ProfileStatus.PENDING,
    }
)


class FinalStatus(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    ERROR = "ERROR"
    TIMEOUT = "TIMEOUT"
    CANCELLED = "CANCELLED"


PHASE_NO_LOAD = "no_load"
PHASE_LOAD = "load"
PHASE_LABELS = {PHASE_NO_LOAD: "bez obciążenia", PHASE_LOAD: "z obciążeniem"}


@dataclass(frozen=True, slots=True)
class TestProfile:
    name: str
    index: int
    nominal_voltage: float
    min_voltage: float
    max_voltage: float
    no_load_seconds: float
    load_seconds: float
    load_current_ma: int
    no_load_current_max_ma: int = 150
    load_current_tolerance_percent: float = 20.0

    @classmethod
    def from_dict(cls, data: dict) -> "TestProfile":
        known = {item for item in cls.__slots__}
        filtered = {key: value for key, value in data.items() if key in known}
        return cls(**filtered)

    def to_dict(self) -> dict:
        return asdict(self)

    def voltage_ok(self, voltage: float) -> bool:
        return self.min_voltage <= voltage <= self.max_voltage

    def current_limits(self, loaded: bool) -> tuple[float, float]:
        """Zwraca dopuszczalny zakres prądu w mA dla danej fazy."""
        if not loaded:
            return 0.0, float(self.no_load_current_max_ma)
        tolerance = self.load_current_ma * self.load_current_tolerance_percent / 100.0
        return max(0.0, self.load_current_ma - tolerance), self.load_current_ma + tolerance

    def current_ok(self, current_a: float, loaded: bool) -> bool:
        measured_ma = abs(current_a) * 1000.0
        minimum, maximum = self.current_limits(loaded)
        return minimum <= measured_ma <= maximum

    def violation(self, voltage: float, current_a: float, loaded: bool) -> str:
        """Opis pierwszego naruszenia lub pusty tekst, gdy pomiar jest poprawny."""
        phase = PHASE_LABELS[PHASE_LOAD if loaded else PHASE_NO_LOAD]
        if not self.voltage_ok(voltage):
            return (
                f"{self.name} {phase}: napięcie {voltage:.3f} V "
                f"poza zakresem {self.min_voltage:.2f}-{self.max_voltage:.2f} V"
            )
        if not self.current_ok(current_a, loaded):
            minimum, maximum = self.current_limits(loaded)
            return (
                f"{self.name} {phase}: prąd {abs(current_a) * 1000.0:.0f} mA "
                f"poza zakresem {minimum:.0f}-{maximum:.0f} mA"
            )
        return ""


@dataclass(frozen=True, slots=True)
class Measurement:
    elapsed_seconds: float
    voltage: float
    current: float
    #: Probka z okna stabilizacji: rejestrowana, ale nieoceniana.
    settling: bool = False


@dataclass(slots=True)
class ProfileResult:
    profile: TestProfile
    no_load: list[Measurement] = field(default_factory=list)
    loaded: list[Measurement] = field(default_factory=list)
    status: ProfileStatus = ProfileStatus.PENDING
    message: str = ""
    #: Opis pierwszego pomiaru poza zakresem (diagnostyka FAIL).
    violation: str = ""

    def add(self, measurement: Measurement, loaded: bool) -> None:
        (self.loaded if loaded else self.no_load).append(measurement)

    # -- próbki oceniane (bez okna stabilizacji) --------------------------

    @property
    def rated_no_load(self) -> list[Measurement]:
        return [item for item in self.no_load if not item.settling]

    @property
    def rated_loaded(self) -> list[Measurement]:
        return [item for item in self.loaded if not item.settling]

    @property
    def sample_count(self) -> int:
        return len(self.no_load) + len(self.loaded)

    def finalize(self, minimum_samples: int) -> None:
        rated_no_load = self.rated_no_load
        rated_loaded = self.rated_loaded
        if len(rated_no_load) < minimum_samples or len(rated_loaded) < minimum_samples:
            self.status = ProfileStatus.NO_DATA
            self.message = (
                f"{self.profile.name}: za mało ocenianych pomiarów "
                f"(bez obciążenia {len(rated_no_load)}, z obciążeniem {len(rated_loaded)}, "
                f"wymagane {minimum_samples}). Sprawdź stanowisko."
            )
            return

        for loaded, samples in ((False, rated_no_load), (True, rated_loaded)):
            for item in samples:
                violation = self.profile.violation(item.voltage, item.current, loaded)
                if violation:
                    self.status = ProfileStatus.FAIL
                    self.violation = violation
                    self.message = violation
                    return

        self.status = ProfileStatus.PASS

    # -- statystyki -------------------------------------------------------

    def _values(self, phase: str, attribute: str, rated_only: bool = True) -> list[float]:
        source = self.rated_loaded if phase == PHASE_LOAD else self.rated_no_load
        if not rated_only:
            source = self.loaded if phase == PHASE_LOAD else self.no_load
        return [getattr(item, attribute) for item in source]

    def minimum(self, phase: str, attribute: str = "voltage") -> float | None:
        values = self._values(phase, attribute)
        return min(values) if values else None

    def maximum(self, phase: str, attribute: str = "voltage") -> float | None:
        values = self._values(phase, attribute)
        return max(values) if values else None

    def average(self, phase: str, attribute: str = "voltage") -> float | None:
        values = self._values(phase, attribute)
        return sum(values) / len(values) if values else None

    # skroty uzywane przez interfejs
    @property
    def min_loaded_voltage(self) -> float | None:
        return self.minimum(PHASE_LOAD)

    @property
    def max_loaded_voltage(self) -> float | None:
        return self.maximum(PHASE_LOAD)

    @property
    def min_no_load_voltage(self) -> float | None:
        return self.minimum(PHASE_NO_LOAD)

    @property
    def max_no_load_voltage(self) -> float | None:
        return self.maximum(PHASE_NO_LOAD)

    @property
    def average_loaded_current(self) -> float | None:
        return self.average(PHASE_LOAD, "current")


@dataclass(slots=True)
class TestResult:
    hrid: str
    serial_number: str
    profiles: list[ProfileResult]
    final_status: FinalStatus
    duration_seconds: float
    timestamp: str = field(default_factory=lambda: datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    message: str = ""
    raw_file: str = ""
    #: Nazwa kanału pomiarowego (np. LEWY/PRAWY). Pusta przy stanowisku jednokanałowym.
    station: str = ""

    @property
    def sample_count(self) -> int:
        return sum(item.sample_count for item in self.profiles)

    @property
    def sampling_rate(self) -> float:
        return self.sample_count / self.duration_seconds if self.duration_seconds > 0 else 0.0

    @property
    def first_violation(self) -> str:
        for item in self.profiles:
            if item.violation:
                return item.violation
        return ""

    @property
    def profile_names(self) -> list[str]:
        return [item.profile.name for item in self.profiles]

    @staticmethod
    def csv_headers(profile_names: Iterable[str]) -> list[str]:
        headers = ["Data i godzina", "Stanowisko", "HRID", "Numer seryjny"]
        for name in profile_names:
            headers.extend(
                [
                    f"{name} - Wynik",
                    f"{name} - Min U bez obc. [V]",
                    f"{name} - Max U bez obc. [V]",
                    f"{name} - Min U obc. [V]",
                    f"{name} - Max U obc. [V]",
                    f"{name} - Średni prąd obc. [A]",
                    f"{name} - Naruszenie",
                ]
            )
        headers.extend(
            [
                "Wynik końcowy",
                "Czas testu [s]",
                "Próbki",
                "Próbkowanie [Hz]",
                "Plik surowy",
                "Komunikat",
            ]
        )
        return headers

    @staticmethod
    def profiles_signature(profile_names: Iterable[str]) -> str:
        """Krótki odcisk zestawu profili - chroni raport przed rozjechaniem kolumn."""
        joined = "|".join(profile_names)
        return hashlib.sha1(joined.encode("utf-8")).hexdigest()[:8]

    def to_csv_row(self) -> list[str]:
        row = [
            self.timestamp,
            _excel_safe(self.station or "-"),
            _excel_safe(self.hrid),
            _excel_safe(self.serial_number),
        ]
        for result in self.profiles:
            row.extend(
                [
                    result.status.value,
                    _decimal(result.min_no_load_voltage, digits=3),
                    _decimal(result.max_no_load_voltage, digits=3),
                    _decimal(result.min_loaded_voltage, digits=3),
                    _decimal(result.max_loaded_voltage, digits=3),
                    _decimal(result.average_loaded_current, digits=3),
                    _excel_safe(result.violation),
                ]
            )
        row.extend(
            [
                self.final_status.value,
                _decimal(self.duration_seconds),
                str(self.sample_count),
                _decimal(self.sampling_rate, digits=1),
                _excel_safe(self.raw_file),
                _excel_safe(self.message),
            ]
        )
        return row


def _decimal(value: float | None, digits: int = 2) -> str:
    if value is None:
        return "N/A"
    return f"{value:.{digits}f}".replace(".", ",")


def _excel_safe(value: str) -> str:
    text = str(value)
    return "'" + text if text.startswith(("=", "+", "-", "@", "\t", "\r")) else text
