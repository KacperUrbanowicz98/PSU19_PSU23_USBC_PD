"""Odczyt, zapis i walidacja konfiguracji stanowiska."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import sys
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime
from pathlib import Path

from models import TestProfile

logger = logging.getLogger(__name__)

#: Jedyne miejsce z numerem wersji. Builder EXE czyta go stąd, raporty też —
#: dzięki temu wersja w raporcie nie może rozjechać się z wersją binarki.
APP_VERSION = "5.5.0"


class ConfigError(RuntimeError):
    pass


def app_directory() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def resolve_path(value: str) -> Path:
    path = Path(os.path.expandvars(os.path.expanduser(str(value))))
    return path.resolve() if path.is_absolute() else (app_directory() / path).resolve()


def _default_profiles() -> list[TestProfile]:
    """Zakresy zgodne ze specyfikacja SR213 PSU (SPBV36UK-W / SPBV36EU-W)."""
    return [
        TestProfile("5V", 1, 5.0, 4.75, 5.50, 2.5, 2.5, 3000),
        TestProfile("9V", 2, 9.0, 8.55, 9.45, 2.5, 2.5, 3000),
        TestProfile("12V", 3, 12.0, 11.40, 12.60, 2.5, 2.5, 3000),
        TestProfile("15V", 4, 15.0, 14.25, 15.75, 2.5, 2.5, 2400),
    ]


def _default_hrids() -> list[str]:
    return [
        "44963", "12100667", "81705", "45216", "45061", "12100171",
        "12100741", "81560", "81563", "81564", "45233", "12101333",
        "12101111", "12100174", "12100475", "12101090", "12100587",
        "12101094", "45016", "TEST", "12100524", "12101639",
        "12101644", "45466", "12100269", "12101487", "45518", "12101673",
    ]


@dataclass(frozen=True, slots=True)
class StationConfig:
    """Jeden kanał pomiarowy: nazwa widoczna dla operatora i adres testera PM125."""

    name: str = "STANOWISKO"
    #: Numer seryjny PM125 (np. PMPD80LLAD). Pusty albo "Any" = pierwsze wykryte urządzenie.
    device_serial: str | None = None
    enabled: bool = True

    @classmethod
    def from_dict(cls, data: dict) -> "StationConfig":
        known = {item for item in cls.__slots__}
        return cls(**{key: value for key, value in data.items() if key in known})

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def address(self) -> str:
        return self.device_serial or "Any"


def _default_stations() -> list[StationConfig]:
    return [StationConfig("STANOWISKO", None, True)]


def station_names_for(count: int) -> list[str]:
    """Domyślne nazwy kanałów: jeden kanał bez nazwy własnej, dwa jako LEWY/PRAWY."""
    if count <= 1:
        return ["STANOWISKO"]
    if count == 2:
        return ["LEWY", "PRAWY"]
    return [f"STANOWISKO {index}" for index in range(1, count + 1)]


def stations_from_devices(devices: list[tuple[int, str]]) -> list[StationConfig]:
    """Buduje listę stanowisk z wyniku komendy -f (indeks, numer seryjny)."""
    names = station_names_for(len(devices))
    return [
        StationConfig(names[position], serial, True)
        for position, (_index, serial) in enumerate(devices)
    ]


def password_hash(password: str) -> str:
    return hashlib.sha256(password.encode("utf-8")).hexdigest()


#: SHA-256 standardowego hasła inżynieryjnego stanowiska. Wartość, nie wywołanie —
#: patrz komentarz przy polu `engineering_password_hash`.
DEFAULT_ENGINEERING_PASSWORD_HASH = (
    "8d9748d5a14e921cbd3c4029ffde98fbf7e59c8b828e04c2b70dae67e81d7070"
)


@dataclass(slots=True)
class AppConfig:
    # --- ścieżki i urządzenie ---
    console_path: str = r"C:\USBPDAPI\USBPDConsole.exe"
    #: Zachowane dla zgodności ze starą konfiguracją jednokanałową — przy wczytaniu
    #: zamieniane na pojedynczy wpis w `stations`.
    device_serial: str | None = None
    reports_directory: str = "reports"
    backup_directory: str = "backup"
    logs_directory: str = "logs"
    raw_directory: str = "raw"
    validation_directory: str = "validation"
    attempts_file: str = "serial_attempts.json"
    pending_file: str = "pending_results.json"
    audit_file: str = "config_audit.log"

    # --- przebieg testu ---
    measurement_interval: float = 0.05
    #: Pauza po przełączeniu profilu. Samo wywołanie konsoli trwa kilkaset ms, a
    #: pierwsze próbki fazy i tak są nieoceniane, więc pauza może być krótka.
    profile_settle_seconds: float = 0.3
    #: Pauza po zmianie obciążenia — zastąpiona oknem próbek nieocenianych.
    load_settle_seconds: float = 0.0
    #: Okno po zmianie profilu/obciążenia: próbki są rejestrowane, ale NIE są oceniane.
    settling_ignore_seconds: float = 0.6
    #: Ile pierwszych próbek fazy jest zawsze nieocenianych. Zabezpiecza przy wolnej
    #: konsoli PM125, gdy jedna próbka trwa dłużej niż całe okno czasowe.
    settling_ignore_samples: int = 1
    #: Szacowany czas jednego wywołania USBPDConsole.exe — używany do planowania
    #: czasu testu i walidacji timeoutu. Rzeczywista wartość trafia do logu po teście.
    command_overhead_seconds: float = 0.6
    #: Odwołanie do nieobecnego testera trwa ~1,7 s, a pojedyncze komendy potrafią
    #: skoczyć powyżej 1,2 s — stąd zapas.
    command_timeout: float = 8.0
    test_timeout: float = 60.0
    minimum_samples: int = 2
    maximum_read_errors: int = 5

    # --- kontrola wyrobu i operatora ---
    maximum_serial_attempts: int = 2
    serial_minimum_length: int = 5
    dut_precheck_enabled: bool = True
    dut_precheck_min_voltage: float = 3.0
    health_check_seconds: float = 20.0
    result_window_pass_seconds: float = 2.0
    result_window_fail_seconds: float = 8.0

    # --- tryb walidacyjny (dostępny tylko z panelu inżynieryjnego) ---
    loop_default_cycles: int = 20
    loop_pause_seconds: float = 2.0

    # --- dane i logi ---
    maximum_csv_rows: int = 200_000
    raw_samples_enabled: bool = True
    raw_retention_days: int = 30
    attempts_retention_days: int = 365
    log_retention_days: int = 14
    debug_logging: bool = False

    #: Kontrola, czy wszystkie testery mają identyczną konfigurację (-r). Różnica
    #: oznacza, że ta sama sztuka dostanie inny wynik zależnie od gniazda.
    verify_station_configuration: bool = True

    stations: list[StationConfig] = field(default_factory=_default_stations)
    profiles: list[TestProfile] = field(default_factory=_default_profiles)
    valid_hrids: list[str] = field(default_factory=_default_hrids)
    #: Hasło inżynieryjne jest statyczne i niezmienne z poziomu aplikacji (wymaganie
    #: właściciela). Trzymamy tu gotowy skrót zamiast `password_hash("...")`, żeby samo
    #: hasło nie trafiło do binarki jako jawny napis — build nie jest podpisany, więc
    #: literal dałoby się wyciągnąć poleceniem `strings`. Hasło pozostaje to samo.
    engineering_password_hash: str = DEFAULT_ENGINEERING_PASSWORD_HASH

    # ------------------------------------------------------------------ IO

    @classmethod
    def default_path(cls) -> Path:
        return app_directory() / "test_config.json"

    @classmethod
    def field_names(cls) -> set[str]:
        return {item.name for item in fields(cls)}

    @classmethod
    def _from_raw(cls, raw: dict) -> tuple["AppConfig", list[str]]:
        warnings: list[str] = []
        known = cls.field_names()
        unknown = sorted(set(raw) - known)
        for key in unknown:
            warnings.append(f"Nieznany klucz konfiguracji pominięty: {key}")
        data = {key: value for key, value in raw.items() if key in known}
        raw_profiles = data.pop("profiles", None)
        raw_stations = data.pop("stations", None)
        try:
            config = cls(**data)
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"Nieprawidłowa struktura konfiguracji: {exc}") from exc
        if raw_profiles is not None:
            try:
                config.profiles = [TestProfile.from_dict(item) for item in raw_profiles]
            except (TypeError, ValueError, AttributeError) as exc:
                raise ConfigError(f"Nieprawidłowa definicja profilu: {exc}") from exc
        if raw_stations:
            try:
                config.stations = [StationConfig.from_dict(item) for item in raw_stations]
            except (TypeError, ValueError, AttributeError) as exc:
                raise ConfigError(f"Nieprawidłowa definicja stanowiska: {exc}") from exc
        elif config.device_serial:
            # Migracja konfiguracji jednokanałowej: device_serial -> jeden wpis stations.
            config.stations = [StationConfig("STANOWISKO", config.device_serial, True)]
            warnings.append("Konfiguracja jednokanałowa — utworzono jedno stanowisko z device_serial.")
        config.valid_hrids = sorted({str(item).strip() for item in config.valid_hrids if str(item).strip()})
        return config, warnings

    @classmethod
    def load(cls, path: str | Path | None = None) -> tuple["AppConfig", list[str]]:
        """Zwraca konfiguracje i listę ostrzeżeń. Uszkodzony plik próbuje odtworzyć z .bak."""
        config_path = Path(path) if path else cls.default_path()
        if not config_path.is_absolute():
            config_path = (app_directory() / config_path).resolve()

        if not config_path.exists():
            config = cls()
            config.save(config_path)
            return config, [f"Utworzono domyślna konfiguracje: {config_path}"]

        warnings: list[str] = []
        try:
            raw = json.loads(config_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            backup = config_path.with_suffix(config_path.suffix + ".bak")
            if not backup.exists():
                raise ConfigError(f"Nie można odczytać konfiguracji {config_path}: {exc}") from exc
            try:
                raw = json.loads(backup.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as backup_exc:
                raise ConfigError(
                    f"Nie można odczytać konfiguracji ani kopii zapasowej: {exc} / {backup_exc}"
                ) from backup_exc
            warnings.append(f"Plik konfiguracji był uszkodzony - odtworzono z kopii {backup.name}.")

        if not isinstance(raw, dict):
            raise ConfigError("Główny element konfiguracji nie jest obiektem JSON.")

        config, structure_warnings = cls._from_raw(raw)
        warnings.extend(structure_warnings)

        errors = config.validate()
        if errors:
            raise ConfigError("Błędy konfiguracji:\n- " + "\n- ".join(errors))
        return config, warnings

    def to_payload(self) -> dict:
        """Słownik zapisywany do pliku JSON."""
        return asdict(self)

    def save(self, path: str | Path | None = None, audit_note: str = "") -> Path:
        config_path = Path(path) if path else self.default_path()
        if not config_path.is_absolute():
            config_path = (app_directory() / config_path).resolve()
        errors = self.validate()
        if errors:
            raise ConfigError("Błędy konfiguracji:\n- " + "\n- ".join(errors))

        payload = self.to_payload()
        temporary = config_path.with_suffix(config_path.suffix + ".tmp")
        backup = config_path.with_suffix(config_path.suffix + ".bak")
        config_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with temporary.open("w", encoding="utf-8", newline="\n") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            if config_path.exists():
                shutil.copy2(config_path, backup)
            os.replace(temporary, config_path)
        except OSError as exc:
            temporary.unlink(missing_ok=True)
            raise ConfigError(f"Nie można zapisać konfiguracji: {exc}") from exc

        if audit_note:
            self.append_audit(audit_note)
        return config_path

    def append_audit(self, note: str) -> None:
        """Dopisuje wpis do dziennika audytu zmian konfiguracji."""
        try:
            path = self.audit_path
            path.parent.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            with path.open("a", encoding="utf-8", newline="") as handle:
                handle.write(f"{stamp} {note}\n")
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as exc:  # audyt nie może blokowac zapisu konfiguracji
            logger.error("Nie zapisano wpisu audytowego: %s", exc)

    def diff(self, other: "AppConfig") -> list[str]:
        """Różnice między konfiguracjami - do wpisu audytowego."""
        changes: list[str] = []
        for item in fields(self):
            old = getattr(self, item.name)
            new = getattr(other, item.name)
            if item.name == "profiles":
                old = [profile.to_dict() for profile in old]
                new = [profile.to_dict() for profile in new]
            if old != new:
                if item.name == "engineering_password_hash":
                    changes.append("engineering_password_hash: zmieniony")
                elif isinstance(old, list):
                    changes.append(f"{item.name}: {len(old)} -> {len(new)} pozycji")
                else:
                    changes.append(f"{item.name}: {old!r} -> {new!r}")
        return changes

    # ----------------------------------------------------------- walidacja

    def validate(self) -> list[str]:
        errors: list[str] = []
        if not self.profiles:
            errors.append("Wymagany jest co najmniej jeden profil testowy.")
        names: set[str] = set()
        indexes: set[int] = set()
        for profile in self.profiles:
            if not profile.name.strip():
                errors.append("Profil nie może mieć pustej nazwy.")
            if profile.name in names:
                errors.append(f"Powtórzona nazwa profilu: {profile.name}.")
            if profile.index in indexes:
                errors.append(f"Powtórzony indeks profilu: {profile.index}.")
            names.add(profile.name)
            indexes.add(profile.index)
            if profile.index < 1:
                errors.append(f"{profile.name}: indeks musi być większy od zera.")
            if profile.min_voltage >= profile.max_voltage:
                errors.append(f"{profile.name}: minimalne napięcie musi być mniejsze od maksymalnego.")
            if not profile.min_voltage <= profile.nominal_voltage <= profile.max_voltage:
                errors.append(f"{profile.name}: napięcie nominalne jest poza zakresem.")
            if not 0 <= profile.load_current_ma <= 5000:
                errors.append(f"{profile.name}: obciążenie musi mieścić się w zakresie 0-5000 mA.")
            if profile.no_load_seconds <= 0 or profile.load_seconds <= 0:
                errors.append(f"{profile.name}: czasy testu muszą być dodatnie.")
            if not 0 <= profile.no_load_current_max_ma <= 5000:
                errors.append(f"{profile.name}: limit prądu bez obciążenia jest nieprawidłowy.")
            if not 0 <= profile.load_current_tolerance_percent <= 100:
                errors.append(f"{profile.name}: tolerancja prądu musi mieścić się w zakresie 0-100%.")
            if self.settling_ignore_seconds >= min(profile.no_load_seconds, profile.load_seconds):
                errors.append(
                    f"{profile.name}: okno stabilizacji ({self.settling_ignore_seconds} s) "
                    "musi być krótsze od najkrótszej fazy pomiarowej."
                )

        if self.measurement_interval <= 0:
            errors.append("Interwał pomiarów musi być dodatni.")
        if self.profile_settle_seconds < 0 or self.load_settle_seconds < 0:
            errors.append("Czasy stabilizacji nie mogą być ujemne.")
        if self.settling_ignore_seconds < 0:
            errors.append("Okno stabilizacji nie może być ujemne.")
        if self.command_timeout <= 0 or self.test_timeout <= 0:
            errors.append("Timeouty muszą być dodatnie.")
        if self.minimum_samples < 1:
            errors.append("Minimalna liczba próbek musi wynosić co najmniej 1.")
        if self.settling_ignore_samples < 0:
            errors.append("Liczba nieocenianych próbek nie może być ujemna.")
        if self.command_overhead_seconds < 0:
            errors.append("Szacowany czas komendy PM125 nie może być ujemny.")
        if self.maximum_read_errors < 1:
            errors.append("Maksymalna liczba błędów odczytu musi wynosić co najmniej 1.")
        if self.maximum_serial_attempts < 1:
            errors.append("Limit prób serialu musi wynosić co najmniej 1.")
        if self.serial_minimum_length < 1:
            errors.append("Minimalna długość numeru seryjnego musi wynosić co najmniej 1.")
        if self.maximum_csv_rows < 2:
            errors.append("Limit wierszy CSV musi wynosić co najmniej 2.")
        if self.dut_precheck_min_voltage < 0:
            errors.append("Próg wykrycia zasilacza nie może być ujemny.")
        if self.health_check_seconds < 0:
            errors.append("Okres kontroli obecności PM125 nie może być ujemny.")
        if not 1 <= self.loop_default_cycles <= 500:
            errors.append("Domyślna liczba cykli walidacji musi mieścić się w zakresie 1-500.")
        if self.loop_pause_seconds < 0:
            errors.append("Przerwa między cyklami walidacji nie może być ujemna.")
        if self.result_window_pass_seconds <= 0 or self.result_window_fail_seconds <= 0:
            errors.append("Czasy okna wyniku muszą być dodatnie.")
        for name, value in (
            ("Retencja logów", self.log_retention_days),
            ("Retencja danych surowych", self.raw_retention_days),
            ("Retencja liczników prób", self.attempts_retention_days),
        ):
            if value < 1:
                errors.append(f"{name} musi wynosić co najmniej 1 dzień.")
        if not self.valid_hrids:
            errors.append("Lista HRID nie może być pusta.")

        active = self.active_stations()
        if not active:
            errors.append("Wymagane jest co najmniej jedno aktywne stanowisko.")
        if len(active) > 4:
            errors.append("Obsługiwane są maksymalnie 4 stanowiska.")
        names: set[str] = set()
        addresses: set[str] = set()
        for station in active:
            if not station.name.strip():
                errors.append("Stanowisko nie może mieć pustej nazwy.")
            if station.name in names:
                errors.append(f"Powtórzona nazwa stanowiska: {station.name}.")
            names.add(station.name)
            address = station.address.upper()
            if len(active) > 1 and address == "ANY":
                errors.append(
                    f"{station.name}: przy wielu stanowiskach każdy tester musi mieć "
                    "podany numer seryjny (adres „Any” wybrałby to samo urządzenie)."
                )
            if address in addresses and address != "ANY":
                errors.append(f"Powtórzony numer seryjny testera: {station.device_serial}.")
            addresses.add(address)

        estimated = self.estimated_test_seconds()
        if estimated >= self.test_timeout:
            errors.append(
                f"Timeout całego testu ({self.test_timeout:g} s) jest krótszy niż przewidywany "
                f"czas testu ({estimated:.1f} s przy komendzie {self.command_overhead_seconds:g} s)."
            )
        rated = self.estimated_samples_per_phase() - self.settling_ignore_samples
        if self.profiles and rated < self.minimum_samples:
            errors.append(
                f"Przy komendzie PM125 {self.command_overhead_seconds:g} s w fazie zmieści się "
                f"{self.estimated_samples_per_phase()} próbek, z czego ocenianych {max(0, rated)} — "
                f"mniej niż wymagane {self.minimum_samples}. Skróć okno stabilizacji, zmniejsz "
                "minimalną liczbę próbek albo wydłuż fazy."
            )
        return errors

    def measurement_seconds(self) -> float:
        """Suma czasów faz pomiarowych — wymaganie procesowe (2,5 s + 2,5 s na profil)."""
        return sum(profile.no_load_seconds + profile.load_seconds for profile in self.profiles)

    def command_count_estimate(self) -> int:
        """Liczba komend sterujących na jeden test (bez odczytów pomiarowych).

        Na profil: ustawienie profilu, ustawienie obciążenia, wyzerowanie po fazie.
        Do tego pre-kontrola obecności zasilacza i końcowy powrót do 5 V / 0 mA.
        """
        per_profile = 3
        extras = (1 if self.dut_precheck_enabled else 0) + 2
        return len(self.profiles) * per_profile + extras

    def estimated_test_seconds(self) -> float:
        """Szacowany czas testu razem z narzutem wywołań USBPDConsole.exe."""
        total = self.measurement_seconds()
        for _ in self.profiles:
            total += self.profile_settle_seconds + 2 * self.load_settle_seconds
        total += self.command_count_estimate() * self.command_overhead_seconds
        return total

    def estimated_samples_per_phase(self) -> int:
        """Ile próbek zmieści się w krótszej fazie przy zakładanym czasie komendy."""
        if self.command_overhead_seconds <= 0 or not self.profiles:
            return 0
        shortest = min(
            min(profile.no_load_seconds, profile.load_seconds) for profile in self.profiles
        )
        return max(1, int(shortest / self.command_overhead_seconds))

    def active_stations(self) -> list[StationConfig]:
        return [station for station in self.stations if station.enabled]

    @property
    def multi_station(self) -> bool:
        return len(self.active_stations()) > 1

    def verify_engineering_password(self, password: str) -> bool:
        return password_hash(password) == self.engineering_password_hash

    # -------------------------------------------------------------- ścieżki

    @property
    def reports_path(self) -> Path:
        return resolve_path(self.reports_directory)

    @property
    def backup_path(self) -> Path:
        return resolve_path(self.backup_directory)

    @property
    def logs_path(self) -> Path:
        return resolve_path(self.logs_directory)

    @property
    def raw_path(self) -> Path:
        return resolve_path(self.raw_directory)

    @property
    def validation_path(self) -> Path:
        return resolve_path(self.validation_directory)

    @property
    def attempts_path(self) -> Path:
        return resolve_path(self.attempts_file)

    @property
    def pending_path(self) -> Path:
        return resolve_path(self.pending_file)

    @property
    def audit_path(self) -> Path:
        return resolve_path(self.audit_file)
