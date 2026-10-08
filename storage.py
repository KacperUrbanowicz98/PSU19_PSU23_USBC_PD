"""Trwały zapis wyników CSV, danych surowych, liczników prób i wyników oczekujących."""
from __future__ import annotations

import csv
import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from uuid import uuid4

from models import PHASE_LOAD, PHASE_NO_LOAD, TestResult

logger = logging.getLogger(__name__)


class SaveState(str, Enum):
    PRIMARY = "PRIMARY"
    BACKUP = "BACKUP"
    FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class SaveOutcome:
    state: SaveState
    path: Path | None
    message: str = ""

    @property
    def saved(self) -> bool:
        return self.state in {SaveState.PRIMARY, SaveState.BACKUP}


@dataclass(frozen=True, slots=True)
class PendingRecord:
    """Wynik, którego nie udało się zapisać - przechowywany poza pamięcią procesu."""

    serial_number: str
    timestamp: str
    final_status: str
    headers: list[str]
    row: list[str]

    def to_dict(self) -> dict:
        return {
            "serial_number": self.serial_number,
            "timestamp": self.timestamp,
            "final_status": self.final_status,
            "headers": self.headers,
            "row": self.row,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "PendingRecord":
        return cls(
            serial_number=str(data["serial_number"]),
            timestamp=str(data["timestamp"]),
            final_status=str(data["final_status"]),
            headers=[str(item) for item in data["headers"]],
            row=[str(item) for item in data["row"]],
        )

    @classmethod
    def from_result(cls, result: TestResult) -> "PendingRecord":
        return cls(
            serial_number=result.serial_number,
            timestamp=result.timestamp,
            final_status=result.final_status.value,
            headers=TestResult.csv_headers(result.profile_names),
            row=result.to_csv_row(),
        )


# ---------------------------------------------------------------- raport CSV


class CsvResultRepository:
    """Zapis raportu z kontrola zgodności nagłówka i buforowanym licznikiem wierszy."""

    def __init__(self, reports_dir: Path, backup_dir: Path, maximum_rows: int) -> None:
        self.reports_dir = reports_dir
        self.backup_dir = backup_dir
        self.maximum_rows = maximum_rows
        self._lock = threading.RLock()
        self._row_cache: dict[Path, int] = {}

    def reconfigure(self, reports_dir: Path, backup_dir: Path, maximum_rows: int) -> None:
        with self._lock:
            self.reports_dir = reports_dir
            self.backup_dir = backup_dir
            self.maximum_rows = maximum_rows
            self._row_cache.clear()

    def save(self, result: TestResult, retries: int = 3, retry_delay: float = 0.5) -> SaveOutcome:
        return self.save_record(PendingRecord.from_result(result), retries=retries, retry_delay=retry_delay)

    def save_record(self, record: PendingRecord, retries: int = 3, retry_delay: float = 0.5) -> SaveOutcome:
        with self._lock:
            primary_error = ""
            for attempt in range(1, retries + 1):
                try:
                    path = self._current_primary_file(record.headers)
                    self._append(path, record.headers, record.row)
                    self._row_cache[path] = self._row_cache.get(path, 0) + 1
                    logger.info("Wynik zapisany: %s", path)
                    return SaveOutcome(SaveState.PRIMARY, path)
                except Exception as exc:  # rowniez błędy inne niż OSError
                    primary_error = str(exc)
                    logger.warning("Błąd zapisu CSV, próba %s/%s: %s", attempt, retries, exc)
                    if attempt < retries:
                        time.sleep(retry_delay)

            try:
                backup = self._backup_file()
                self._append(backup, record.headers, record.row)
                message = f"Główny raport niedostępny ({primary_error}). Zapisano kopie awaryjna."
                logger.error("%s Plik: %s", message, backup)
                return SaveOutcome(SaveState.BACKUP, backup, message)
            except Exception as exc:
                message = f"Nie zapisano wyniku ani w raporcie głównym, ani w backupie: {exc}"
                logger.critical(message, exc_info=True)
                return SaveOutcome(SaveState.FAILED, None, message)

    def _current_primary_file(self, headers: list[str]) -> Path:
        """Plik raportu dla bieżącego zestawu kolumn.

        Nazwa zawiera odcisk zestawu profili, więc zmiana konfiguracji profili
        zakłada nowy plik zamiast dopisywać wiersze pod niepasujący nagłówek.
        """
        self.reports_dir.mkdir(parents=True, exist_ok=True)
        signature = TestResult.profiles_signature(headers)
        index = 1
        while True:
            candidate = self.reports_dir / f"raport_testow_{signature}_{index}.csv"
            if not candidate.exists():
                return candidate
            if self._headers_match(candidate, headers) and self._rows(candidate) < self.maximum_rows:
                return candidate
            index += 1

    def _rows(self, path: Path) -> int:
        cached = self._row_cache.get(path)
        if cached is not None:
            return cached
        counted = _line_count(path)
        self._row_cache[path] = counted
        return counted

    @staticmethod
    def _headers_match(path: Path, headers: list[str]) -> bool:
        try:
            with path.open("r", encoding="utf-8-sig", newline="") as handle:
                first = handle.readline()
        except OSError:
            return False
        if not first.strip():
            return True
        return [item.strip() for item in first.rstrip("\r\n").split(";")] == headers

    def _backup_file(self) -> Path:
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        return self.backup_dir / f"raport_BACKUP_{stamp}_{uuid4().hex[:6]}.csv"

    @staticmethod
    def _append(path: Path, headers: list[str], row: list[str]) -> None:
        new_file = not path.exists() or path.stat().st_size == 0
        with path.open("a", encoding="utf-8-sig", newline="") as handle:
            writer = csv.writer(handle, delimiter=";", quoting=csv.QUOTE_MINIMAL)
            if new_file:
                writer.writerow(headers)
            writer.writerow(row)
            handle.flush()
            os.fsync(handle.fileno())


# ------------------------------------------------------------- dane surowe


class RawSampleWriter:
    """Zapis wszystkich próbek testu - podstawa analizy yieldu i sporów o FAIL."""

    def __init__(self, directory: Path, enabled: bool = True, retention_days: int = 30) -> None:
        self.directory = directory
        self.enabled = enabled
        self.retention_days = retention_days
        self._lock = threading.RLock()

    def reconfigure(self, directory: Path, enabled: bool, retention_days: int) -> None:
        with self._lock:
            self.directory = directory
            self.enabled = enabled
            self.retention_days = retention_days

    def write(self, result: TestResult) -> Path | None:
        if not self.enabled:
            return None
        with self._lock:
            try:
                self.directory.mkdir(parents=True, exist_ok=True)
                safe_serial = "".join(char for char in result.serial_number if char.isalnum() or char in "-_")
                stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                path = self.directory / f"{safe_serial or 'SN'}_{stamp}.csv"
                with path.open("w", encoding="utf-8-sig", newline="") as handle:
                    writer = csv.writer(handle, delimiter=";", quoting=csv.QUOTE_MINIMAL)
                    writer.writerow(
                        [
                            "Profil", "Faza", "Czas fazy [s]", "Napięcie [V]", "Prąd [A]",
                            "Oceniana", "W zakresie",
                        ]
                    )
                    for profile_result in result.profiles:
                        profile = profile_result.profile
                        for phase, samples in (
                            (PHASE_NO_LOAD, profile_result.no_load),
                            (PHASE_LOAD, profile_result.loaded),
                        ):
                            loaded = phase == PHASE_LOAD
                            for sample in samples:
                                in_range = not profile.violation(sample.voltage, sample.current, loaded)
                                writer.writerow(
                                    [
                                        profile.name,
                                        phase,
                                        f"{sample.elapsed_seconds:.3f}".replace(".", ","),
                                        f"{sample.voltage:.4f}".replace(".", ","),
                                        f"{sample.current:.4f}".replace(".", ","),
                                        "NIE" if sample.settling else "TAK",
                                        "TAK" if in_range else "NIE",
                                    ]
                                )
                    handle.flush()
                    os.fsync(handle.fileno())
                self._cleanup()
                return path
            except Exception as exc:  # brak danych surowych nie może przerwac testu
                logger.error("Nie zapisano danych surowych: %s", exc)
                return None

    def _cleanup(self) -> None:
        cutoff = datetime.now() - timedelta(days=self.retention_days)
        for path in self.directory.glob("*.csv"):
            try:
                if datetime.fromtimestamp(path.stat().st_mtime) < cutoff:
                    path.unlink()
            except OSError:
                pass


# ---------------------------------------------------------- liczniki prób


class AttemptStore:
    """Trwały licznik prób per numer seryjny, z data ostatniej próby i retencja."""

    def __init__(self, path: Path, retention_days: int = 365) -> None:
        self.path = path
        self.retention_days = retention_days
        self._lock = threading.RLock()
        self._attempts = self._load()
        #: Numery seryjne aktualnie testowane, per kanał — tylko w pamięci procesu.
        self._reserved: dict[str, str] = {}

    def reconfigure(self, path: Path, retention_days: int) -> None:
        with self._lock:
            self.path = path
            self.retention_days = retention_days
            self._attempts = self._load()

    def count(self, serial_number: str) -> int:
        with self._lock:
            entry = self._attempts.get(_normalize_serial(serial_number))
            return int(entry["count"]) if entry else 0

    # -- rezerwacja numeru na czas testu (ochrona przed tą samą sztuką na dwóch kanałach) --

    def reserve(self, serial_number: str, station: str) -> str | None:
        """Rezerwuje numer seryjny dla kanału. Zwraca nazwę kanału, który już go trzyma."""
        serial = _normalize_serial(serial_number)
        with self._lock:
            holder = self._reserved.get(serial)
            if holder is not None and holder != station:
                return holder
            self._reserved[serial] = station
            return None

    def release(self, serial_number: str, station: str) -> None:
        serial = _normalize_serial(serial_number)
        with self._lock:
            if self._reserved.get(serial) == station:
                self._reserved.pop(serial, None)

    def reserved_by(self, serial_number: str) -> str | None:
        with self._lock:
            return self._reserved.get(_normalize_serial(serial_number))

    def entries(self) -> list[tuple[str, int, str]]:
        with self._lock:
            return sorted(
                ((serial, int(data["count"]), str(data.get("last", "")))
                 for serial, data in self._attempts.items()),
                key=lambda item: item[2],
                reverse=True,
            )

    def record(self, serial_number: str) -> int:
        serial = _normalize_serial(serial_number)
        with self._lock:
            previous = dict(self._attempts.get(serial, {"count": 0, "last": ""}))
            self._attempts[serial] = {
                "count": int(previous["count"]) + 1,
                "last": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }
            try:
                self._save()
            except OSError:
                if previous["count"]:
                    self._attempts[serial] = previous
                else:
                    self._attempts.pop(serial, None)
                raise
            return int(self._attempts[serial]["count"])

    def reset(self, serial_number: str) -> None:
        with self._lock:
            self._attempts.pop(_normalize_serial(serial_number), None)
            self._save()

    def _load(self) -> dict[str, dict]:
        if not self.path.exists():
            return {}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("Główny element nie jest obiektem JSON.")
            attempts: dict[str, dict] = {}
            for key, value in payload.items():
                if isinstance(value, dict):  # nowy format
                    count = int(value.get("count", 0))
                    last = str(value.get("last", ""))
                else:  # migracja ze starego formatu {serial: liczba}
                    count = int(value)
                    last = ""
                if count > 0:
                    attempts[str(key)] = {"count": count, "last": last}
            return self._prune(attempts)
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            logger.error("Nie można odczytać liczników prób %s: %s", self.path, exc)
            corrupt = self.path.with_suffix(self.path.suffix + f".corrupt_{int(time.time())}")
            try:
                self.path.replace(corrupt)
            except OSError:
                pass
            return {}

    def _prune(self, attempts: dict[str, dict]) -> dict[str, dict]:
        cutoff = datetime.now() - timedelta(days=self.retention_days)
        kept: dict[str, dict] = {}
        for serial, data in attempts.items():
            last = str(data.get("last", ""))
            if not last:
                kept[serial] = data
                continue
            try:
                if datetime.strptime(last, "%Y-%m-%d %H:%M:%S") >= cutoff:
                    kept[serial] = data
            except ValueError:
                kept[serial] = data
        removed = len(attempts) - len(kept)
        if removed:
            logger.info("Usunięto %s przeterminowanych liczników prób.", removed)
        return kept

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(self._attempts, handle, indent=2, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, self.path)


# ------------------------------------------------------- wyniki oczekujace


class PendingResultStore:
    """Wyniki, których nie udało się zapisać - przetrwają zamknięcie aplikacji."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.RLock()
        self._records = self._load()

    def reconfigure(self, path: Path) -> None:
        with self._lock:
            self.path = path
            self._records = self._load()

    def all(self) -> list[PendingRecord]:
        with self._lock:
            return list(self._records)

    @property
    def count(self) -> int:
        with self._lock:
            return len(self._records)

    def add(self, record: PendingRecord) -> bool:
        with self._lock:
            self._records.append(record)
            return self._save()

    def remove(self, record: PendingRecord) -> bool:
        with self._lock:
            self._records = [item for item in self._records if item != record]
            return self._save()

    def clear(self) -> bool:
        with self._lock:
            self._records = []
            return self._save()

    def _load(self) -> list[PendingRecord]:
        if not self.path.exists():
            return []
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            return [PendingRecord.from_dict(item) for item in payload]
        except Exception as exc:
            logger.error("Nie można odczytać wyników oczekujących %s: %s", self.path, exc)
            return []

    def _save(self) -> bool:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(self.path.suffix + ".tmp")
            with temporary.open("w", encoding="utf-8", newline="\n") as handle:
                json.dump([item.to_dict() for item in self._records], handle, indent=2, ensure_ascii=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            return True
        except OSError as exc:
            logger.critical("Nie zapisano listy wyników oczekujących: %s", exc)
            return False


def _normalize_serial(value: str) -> str:
    return value.strip().upper()


def _line_count(path: Path) -> int:
    try:
        with path.open("r", encoding="utf-8-sig", errors="replace") as handle:
            return sum(1 for _ in handle)
    except OSError:
        return 0
