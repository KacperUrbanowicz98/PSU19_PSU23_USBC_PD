"""Silnik wykonywania testow niezależny od GUI i zapisu wyników."""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Callable

from config import AppConfig
from hardware import Hardware
from models import (
    PHASE_LOAD,
    PHASE_NO_LOAD,
    STATION_PROBLEM_STATUSES,
    FinalStatus,
    Measurement,
    ProfileResult,
    ProfileStatus,
    TestProfile,
    TestResult,
)

logger = logging.getLogger(__name__)


class TestTimedOut(RuntimeError):
    pass


class TestCancelled(RuntimeError):
    pass


class MeasurementFailure(RuntimeError):
    pass


class DutNotDetected(RuntimeError):
    """Brak zasilacza na stanowisku - to nie jest wynik testu."""


@dataclass(frozen=True, slots=True)
class ProgressEvent:
    profile_name: str
    profile_number: int
    profile_total: int
    phase: str
    elapsed_seconds: float
    voltage: float
    current: float
    measurement_ok: bool
    settling: bool
    sample_number: int
    rated_samples: int
    overall_fraction: float


ProgressCallback = Callable[[ProgressEvent], None]


class TestEngine:
    def __init__(self, config: AppConfig, hardware: Hardware) -> None:
        self.config = config
        self.hardware = hardware

    # --------------------------------------------------------- pre-kontrola

    def detect_dut(self) -> tuple[bool, float, str]:
        """Szybka kontrola obecności zasilacza przed właściwym testem.

        Zwraca (wykryty, zmierzone napięcie, komunikat). Bez tej kontroli pusty
        stelaż kosztuje pełny czas testu i kończy się FAIL zamiast komunikatem.
        """
        config = self.config
        if not config.dut_precheck_enabled:
            return True, 0.0, ""

        # Konsola potrafi powiedzieć wprost, ile profili PD deklaruje wyrób.
        # Zero profili = puste gniazdo albo zasilacz bez PD — jedna komenda zamiast trzech.
        profiles = self.hardware.dut_profile_count()
        if profiles is not None:
            if profiles <= 0:
                return False, 0.0, (
                    "Nie wykryto zasilacza w gnieździe (tester nie widzi żadnego profilu PD)."
                )
            if profiles < len(config.profiles):
                logger.warning(
                    "Zasilacz deklaruje %s profili PD, a test wymaga %s.",
                    profiles, len(config.profiles),
                )
            return True, 0.0, ""

        # Konsola nie obsłużyła -p — schodzimy do pomiaru napięcia.
        measurement = self.hardware.read_measurement()
        if measurement is not None and measurement.voltage >= config.dut_precheck_min_voltage:
            return True, measurement.voltage, ""
        if not self.hardware.set_profile(1):
            return False, 0.0, self.hardware.last_error or "Nie ustawiono profilu 5 V."
        if not self.hardware.set_load(0):
            return False, 0.0, self.hardware.last_error or "Nie wyzerowano obciążenia."
        time.sleep(min(0.3, config.profile_settle_seconds))
        measurement = self.hardware.read_measurement()
        if measurement is not None:
            if measurement.voltage >= config.dut_precheck_min_voltage:
                return True, measurement.voltage, ""
            return (
                False,
                measurement.voltage,
                f"Nie wykryto zasilacza (zmierzono {measurement.voltage:.2f} V, "
                f"próg {config.dut_precheck_min_voltage:.2f} V).",
            )
        return False, 0.0, self.hardware.last_error or "Brak odczytu z PM125."

    # ----------------------------------------------------------- przebieg

    def run(
        self,
        hrid: str,
        serial_number: str,
        progress: ProgressCallback | None = None,
        cancel_event: threading.Event | None = None,
        station: str = "",
    ) -> TestResult:
        if not self.hardware.connected:
            raise ConnectionError("PM125 nie jest połączony.")
        config = self.config
        if not config.profiles:
            raise RuntimeError("Brak profili testowych.")

        started = time.monotonic()
        deadline = started + config.test_timeout
        results: list[ProfileResult] = []
        forced_status: FinalStatus | None = None
        final_message = ""
        total = len(config.profiles)

        reset_statistics = getattr(self.hardware, "reset_command_statistics", None)
        if callable(reset_statistics):
            reset_statistics()
        logger.info(
            "Start testu HRID=%s serial=%s stanowisko=%s", hrid, serial_number, station or "-"
        )
        try:
            for position, profile in enumerate(config.profiles):
                self._guard(deadline, cancel_event)
                result = self._run_profile(
                    profile, position + 1, total, deadline, progress, cancel_event
                )
                results.append(result)
                if result.status in {
                    ProfileStatus.PROFILE_ERROR,
                    ProfileStatus.LOAD_ERROR,
                    ProfileStatus.MEASUREMENT_ERROR,
                    ProfileStatus.NO_DATA,
                }:
                    final_message = result.message
                    self._append_skipped(
                        results, position + 1, ProfileStatus.CANCELLED,
                        "Test przerwany po błędzie stanowiska.",
                    )
                    break

        except TestTimedOut as exc:
            forced_status = FinalStatus.TIMEOUT
            final_message = str(exc)
            self._append_skipped(results, len(results), ProfileStatus.TIMEOUT, str(exc))
        except TestCancelled as exc:
            forced_status = FinalStatus.CANCELLED
            final_message = str(exc)
            self._append_skipped(results, len(results), ProfileStatus.CANCELLED, str(exc))
        except Exception as exc:
            logger.exception("Nieoczekiwany błąd testu")
            forced_status = FinalStatus.ERROR
            final_message = str(exc)
            self._append_skipped(results, len(results), ProfileStatus.MEASUREMENT_ERROR, str(exc))
        finally:
            # Utrata testera w trakcie testu daje objawy (brak próbek, nieudany reset),
            # które same w sobie nie wskazują przyczyny. Nazywamy ją wprost.
            if not self.hardware.connected:
                final_message = _join_messages(
                    final_message,
                    "Tester przestał odpowiadać w trakcie testu"
                    + (
                        f" ({self.hardware.last_error})."
                        if getattr(self.hardware, "last_error", "") else "."
                    ),
                )
                if forced_status is None:
                    forced_status = FinalStatus.ERROR
            if not self.hardware.safe_reset():
                logger.error("Nie udało się przywrócić bezpiecznego stanu 5 V / 0 mA")
                final_message = _join_messages(final_message, "Nie udało się przywrócić 5 V / 0 mA.")
                if forced_status is None:
                    forced_status = FinalStatus.ERROR

        duration = time.monotonic() - started
        final_status = self._calculate_final_status(results, forced_status)
        if final_status is FinalStatus.FAIL and not final_message:
            final_message = next((item.violation for item in results if item.violation), "")
        result = TestResult(
            hrid=hrid,
            serial_number=serial_number,
            profiles=results,
            final_status=final_status,
            duration_seconds=duration,
            message=final_message,
            station=station,
        )
        command_seconds = getattr(self.hardware, "average_command_seconds", 0.0)
        logger.info(
            "Koniec testu serial=%s status=%s czas=%.2fs próbki=%s (%.1f Hz), "
            "średnia komenda PM125 %.0f ms",
            serial_number, final_status.value, duration, result.sample_count,
            result.sampling_rate, command_seconds * 1000.0,
        )
        thin = [
            item.profile.name
            for item in results
            if item.status in {ProfileStatus.PASS, ProfileStatus.FAIL}
            and min(len(item.rated_no_load), len(item.rated_loaded)) <= config.minimum_samples
        ]
        if thin:
            logger.warning(
                "Mało ocenianych próbek na fazę (profile: %s). Konsola PM125 odpowiada w %.0f ms — "
                "rozważ skrócenie okna stabilizacji albo wydłużenie faz.",
                ", ".join(thin), command_seconds * 1000.0,
            )
        return result

    # ------------------------------------------------------------- profile

    def _run_profile(
        self,
        profile: TestProfile,
        number: int,
        total: int,
        deadline: float,
        progress: ProgressCallback | None,
        cancel_event: threading.Event | None,
    ) -> ProfileResult:
        result = ProfileResult(profile=profile)
        logger.info("Profil %s (%s/%s): start", profile.name, number, total)

        if not self.hardware.set_profile(profile.index):
            result.status = ProfileStatus.PROFILE_ERROR
            result.message = self.hardware.last_error or f"Nie ustawiono profilu {profile.index}."
            return result
        self._sleep(self.config.profile_settle_seconds, deadline, cancel_event)

        if not self.hardware.set_load(0):
            result.status = ProfileStatus.LOAD_ERROR
            result.message = self.hardware.last_error or "Nie wyzerowano obciążenia."
            return result
        self._sleep(self.config.load_settle_seconds, deadline, cancel_event)

        try:
            self._collect(
                result, profile, loaded=False, duration=profile.no_load_seconds,
                number=number, total=total, phase_number=2 * number - 1, phase_total=2 * total,
                deadline=deadline, progress=progress, cancel_event=cancel_event,
            )

            if not self.hardware.set_load(profile.load_current_ma):
                result.status = ProfileStatus.LOAD_ERROR
                result.message = self.hardware.last_error or "Nie ustawiono wymaganego obciążenia."
                return result
            self._sleep(self.config.load_settle_seconds, deadline, cancel_event)

            self._collect(
                result, profile, loaded=True, duration=profile.load_seconds,
                number=number, total=total, phase_number=2 * number, phase_total=2 * total,
                deadline=deadline, progress=progress, cancel_event=cancel_event,
            )
            result.finalize(self.config.minimum_samples)
            return result

        except MeasurementFailure as exc:
            result.status = ProfileStatus.MEASUREMENT_ERROR
            result.message = str(exc)
            return result
        finally:
            # Zerowanie obciążenia wykonujemy zawsze - także po błędzie komendy.
            if not self.hardware.set_load(0):
                logger.error(
                    "Nie wyzerowano obciążenia po profilu %s: %s", profile.name, self.hardware.last_error
                )
                if result.status is ProfileStatus.PASS:
                    result.status = ProfileStatus.SAFE_RESET_ERROR
                    result.message = "Nie wyzerowano obciążenia po profilu."

    def _collect(
        self,
        result: ProfileResult,
        profile: TestProfile,
        loaded: bool,
        duration: float,
        number: int,
        total: int,
        phase_number: int,
        phase_total: int,
        deadline: float,
        progress: ProgressCallback | None,
        cancel_event: threading.Event | None,
    ) -> None:
        """Zbiera próbki przez `duration` sekund.

        Czas próbki liczony jest od MOMENTU ROZPOCZĘCIA odczytu, nie jego zakończenia —
        przy konsoli odpowiadającej w kilkaset ms różnica decyduje o tym, czy próbka
        z transjentu trafi do oceny.
        """
        phase_started = time.monotonic()
        phase_deadline = phase_started + duration
        settling_window = self.config.settling_ignore_seconds
        settling_samples = self.config.settling_ignore_samples
        minimum = self.config.minimum_samples
        consecutive_errors = 0
        index = 0
        rated = 0

        while True:
            now = time.monotonic()
            if now >= phase_deadline:
                break
            # Nie rozpoczynamy odczytu, który i tak wybiegnie poza fazę, o ile mamy
            # już komplet ocenianych próbek — inaczej każda faza rosłaby o czas komendy.
            expected = self.hardware.average_command_seconds
            if expected > 0 and rated >= minimum and phase_deadline - now < expected * 0.75:
                break

            self._guard(deadline, cancel_event)
            sample_started = time.monotonic()
            raw = self.hardware.read_measurement()
            self._guard(deadline, cancel_event)
            if raw is None:
                consecutive_errors += 1
                if consecutive_errors >= self.config.maximum_read_errors:
                    raise MeasurementFailure(
                        f"{profile.name}: {consecutive_errors} kolejnych błędów pomiaru. "
                        f"{self.hardware.last_error}"
                    )
            else:
                consecutive_errors = 0
                elapsed = sample_started - phase_started
                # Okno stabilizacji: próbka jest rejestrowana, ale nie wchodzi do oceny.
                # Warunek czasowy dla szybkiej konsoli, warunek indeksowy dla wolnej.
                settling = elapsed < settling_window or index < settling_samples
                measurement = Measurement(elapsed, raw.voltage, raw.current, settling=settling)
                result.add(measurement, loaded=loaded)
                index += 1
                if not settling:
                    rated += 1
                measurement_ok = not profile.violation(raw.voltage, raw.current, loaded)
                if progress:
                    try:
                        phase_fraction = min(1.0, max(0.0, (time.monotonic() - phase_started) / duration))
                        progress(
                            ProgressEvent(
                                profile_name=profile.name,
                                profile_number=number,
                                profile_total=total,
                                phase=PHASE_LOAD if loaded else PHASE_NO_LOAD,
                                elapsed_seconds=elapsed,
                                voltage=raw.voltage,
                                current=raw.current,
                                measurement_ok=measurement_ok,
                                settling=settling,
                                sample_number=index,
                                rated_samples=rated,
                                overall_fraction=(phase_number - 1 + phase_fraction) / phase_total,
                            )
                        )
                    except Exception:
                        logger.exception("Błąd callbacku postępu; test jest kontynuowany")

            remaining = min(phase_deadline, deadline) - time.monotonic()
            if remaining > 0:
                time.sleep(min(self.config.measurement_interval, remaining))

    # ----------------------------------------------------------- narzędzia

    def _sleep(self, seconds: float, deadline: float, cancel_event: threading.Event | None) -> None:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            self._guard(deadline, cancel_event)
            time.sleep(max(0.0, min(0.05, end - time.monotonic())))

    @staticmethod
    def _guard(deadline: float, cancel_event: threading.Event | None) -> None:
        if cancel_event and cancel_event.is_set():
            raise TestCancelled("Test anulowany przez użytkownika.")
        if time.monotonic() >= deadline:
            raise TestTimedOut("Przekroczono globalny czas testu.")

    def _append_skipped(
        self,
        results: list[ProfileResult],
        start_index: int,
        status: ProfileStatus,
        message: str,
    ) -> None:
        existing_names = {item.profile.name for item in results}
        for profile in self.config.profiles[start_index:]:
            if profile.name not in existing_names:
                results.append(ProfileResult(profile=profile, status=status, message=message))

    @staticmethod
    def _calculate_final_status(
        results: list[ProfileResult], forced: FinalStatus | None = None
    ) -> FinalStatus:
        """Błąd stanowiska ma pierwszeństwo przed FAIL wyrobu.

        Wyrób można odrzucić tylko wtedy, gdy wszystkie profile faktycznie się wykonały.
        """
        if forced is not None:
            return forced
        if not results:
            return FinalStatus.ERROR
        if any(item.status in STATION_PROBLEM_STATUSES for item in results):
            return FinalStatus.ERROR
        if any(item.status is ProfileStatus.FAIL for item in results):
            return FinalStatus.FAIL
        if all(item.status is ProfileStatus.PASS for item in results):
            return FinalStatus.PASS
        return FinalStatus.ERROR


def _join_messages(first: str, second: str) -> str:
    return "; ".join(item for item in (first, second) if item)
