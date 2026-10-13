"""Tryb walidacyjny: seria powtórzonych testów na jednej sztuce wraz ze statystyką.

Służy do sprawdzenia dwóch rzeczy naraz:

* powtarzalności stanowiska — czy ten sam zasilacz daje ten sam wynik w kolejnych cyklach,
* marginesu wyrobu — jak blisko granic kryteriów leżą rzeczywiste pomiary.

Wyniki serii NIE trafiają do raportu produkcyjnego i NIE zużywają licznika prób.
"""
from __future__ import annotations

import csv
import logging
import os
import statistics
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

from config import APP_VERSION, AppConfig
from engine import TestEngine
from models import PHASE_LABELS, PHASE_LOAD, PHASE_NO_LOAD, FinalStatus, TestResult

logger = logging.getLogger(__name__)



@dataclass(frozen=True, slots=True)
class LoopProgress:
    cycle: int
    total: int
    stage: str           # "test", "przerwa", "koniec"
    last_status: str
    passed: int
    failed: int
    errors: int
    elapsed_seconds: float


LoopProgressCallback = Callable[[LoopProgress], None]


@dataclass(slots=True)
class PhaseStatistics:
    """Statystyka jednej fazy jednego profilu w całej serii."""

    profile: str
    phase: str
    samples: int = 0
    minimum: float = 0.0
    maximum: float = 0.0
    average: float = 0.0
    deviation: float = 0.0
    limit_low: float = 0.0
    limit_high: float = 0.0
    #: Zapas do granicy dolnej i górnej w woltach (najgorszy pomiar serii).
    margin_low: float = 0.0
    margin_high: float = 0.0
    #: Trend napięcia w serii: nachylenie regresji liniowej w mV na cykl.
    drift_mv_per_cycle: float = 0.0

    @property
    def span(self) -> float:
        return self.maximum - self.minimum

    @property
    def worst_margin(self) -> float:
        return min(self.margin_low, self.margin_high)

    @property
    def worst_margin_percent(self) -> float:
        width = self.limit_high - self.limit_low
        return (self.worst_margin / width * 100.0) if width > 0 else 0.0


@dataclass(frozen=True, slots=True)
class SourceResistance:
    """Rezystancja szeregowa toru: zasilacz + kabel + styk + wejście testera.

    Liczona z danych, które seria i tak zbiera: ze spadku napięcia między fazą bez
    obciążenia a fazą z obciążeniem, podzielonego przez zmierzony prąd. Pozwala
    porównywać gniazda jedną liczbą — sam spadek napięcia zależy od prądu profilu,
    rezystancja nie.
    """

    profile: str
    no_load_volts: float
    loaded_volts: float
    current_amps: float

    @property
    def drop_volts(self) -> float:
        return self.no_load_volts - self.loaded_volts

    @property
    def milliohms(self) -> float:
        return (self.drop_volts / self.current_amps * 1000.0) if self.current_amps else 0.0


@dataclass(slots=True)
class LoopSummary:
    label: str
    hrid: str
    started: str
    finished: str
    cycles_planned: int
    cycles_done: int
    passed: int = 0
    failed: int = 0
    errors: int = 0
    cancelled: bool = False
    durations: list[float] = field(default_factory=list)
    command_seconds: list[float] = field(default_factory=list)
    phases: list[PhaseStatistics] = field(default_factory=list)
    current_stats: list[PhaseStatistics] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    report_path: Path | None = None
    data_path: Path | None = None
    #: Ile razy w serii trzeba było odzyskać połączenie z testerem.
    reconnects: int = 0
    #: Wypełnione, gdy seria została przerwana — np. tester przestał odpowiadać.
    aborted_reason: str = ""

    @property
    def yield_percent(self) -> float:
        return (self.passed / self.cycles_done * 100.0) if self.cycles_done else 0.0

    @property
    def average_duration(self) -> float:
        return statistics.fmean(self.durations) if self.durations else 0.0

    @property
    def average_command_ms(self) -> float:
        return statistics.fmean(self.command_seconds) * 1000.0 if self.command_seconds else 0.0

    @property
    def source_resistances(self) -> list[SourceResistance]:
        """Rezystancja toru per profil — liczona z już zebranej statystyki."""
        no_load = {item.profile: item.average for item in self.phases if item.phase == PHASE_NO_LOAD}
        loaded = {item.profile: item.average for item in self.phases if item.phase == PHASE_LOAD}
        currents = {item.profile: item.average for item in self.current_stats}
        output: list[SourceResistance] = []
        for profile, idle_volts in no_load.items():
            if profile in loaded and currents.get(profile):
                output.append(
                    SourceResistance(profile, idle_volts, loaded[profile], currents[profile])
                )
        return output

    @property
    def average_milliohms(self) -> float:
        values = [item.milliohms for item in self.source_resistances]
        return statistics.fmean(values) if values else 0.0

    @property
    def worst_phase(self) -> PhaseStatistics | None:
        return min(self.phases, key=lambda item: item.worst_margin_percent, default=None)

    @property
    def repeatable(self) -> bool:
        """Seria jest powtarzalna, gdy wszystkie cykle dały ten sam werdykt."""
        return self.cycles_done > 0 and max(self.passed, self.failed) == self.cycles_done


class LoopRunner:
    """Wykonuje serię testów i buduje z nich statystykę."""

    #: Ile razy przed cyklem próbować odzyskać utracony tester.
    RECONNECT_ATTEMPTS = 3
    #: Przerwa między próbami odzyskania — USB potrzebuje chwili na ponowne zgłoszenie.
    RECONNECT_PAUSE_SECONDS = 2.0

    def __init__(self, config: AppConfig, engine: TestEngine) -> None:
        self.config = config
        self.engine = engine

    def run(
        self,
        label: str,
        hrid: str,
        cycles: int,
        pause_seconds: float,
        progress: LoopProgressCallback | None = None,
        cancel_event: threading.Event | None = None,
        raw_writer=None,
    ) -> tuple[LoopSummary, list[TestResult]]:
        started_at = time.monotonic()
        summary = LoopSummary(
            label=label,
            hrid=hrid,
            started=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            finished="",
            cycles_planned=cycles,
            cycles_done=0,
        )
        results: list[TestResult] = []

        logger.info("Tryb walidacyjny: start serii %s cykli, próbka=%s", cycles, label)
        for cycle in range(1, cycles + 1):
            if cancel_event and cancel_event.is_set():
                summary.cancelled = True
                break

            if not self._ensure_connected(summary, cycle, cancel_event, progress, started_at):
                break

            if progress:
                progress(
                    LoopProgress(
                        cycle, cycles, "test", "", summary.passed, summary.failed,
                        summary.errors, time.monotonic() - started_at,
                    )
                )
            try:
                result = self.engine.run(
                    hrid=hrid,
                    serial_number=f"{label}#{cycle:03d}",
                    cancel_event=cancel_event,
                )
            except Exception as exc:  # błąd stanowiska nie przerywa całej serii
                logger.exception("Tryb walidacyjny: cykl %s nie wykonał się", cycle)
                summary.errors += 1
                summary.cycles_done += 1
                summary.problems.append(f"cykl {cycle}: {exc}")
                continue

            results.append(result)
            summary.cycles_done += 1
            summary.durations.append(result.duration_seconds)
            command_seconds = getattr(self.engine.hardware, "average_command_seconds", 0.0)
            if command_seconds:
                summary.command_seconds.append(command_seconds)

            if result.final_status is FinalStatus.PASS:
                summary.passed += 1
            elif result.final_status is FinalStatus.FAIL:
                summary.failed += 1
                summary.problems.append(
                    f"cykl {cycle}: FAIL — {result.first_violation or result.message}"
                )
            else:
                summary.errors += 1
                summary.problems.append(
                    f"cykl {cycle}: {result.final_status.value} — {result.message}"
                )

            if raw_writer is not None:
                raw_writer.write(result)

            if progress:
                progress(
                    LoopProgress(
                        cycle, cycles, "przerwa", result.final_status.value, summary.passed,
                        summary.failed, summary.errors, time.monotonic() - started_at,
                    )
                )
            if cycle < cycles and pause_seconds > 0:
                self._sleep(pause_seconds, cancel_event)

        summary.finished = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        summary.phases = self._voltage_statistics(results)
        summary.current_stats = self._current_statistics(results)
        if progress:
            progress(
                LoopProgress(
                    summary.cycles_done, cycles, "koniec", "", summary.passed, summary.failed,
                    summary.errors, time.monotonic() - started_at,
                )
            )
        logger.info(
            "Tryb walidacyjny: koniec serii — PASS %s, FAIL %s, ERROR %s z %s cykli",
            summary.passed, summary.failed, summary.errors, summary.cycles_done,
        )
        return summary, results

    def _ensure_connected(
        self,
        summary: LoopSummary,
        cycle: int,
        cancel_event: threading.Event | None,
        progress: LoopProgressCallback | None,
        started_at: float,
    ) -> bool:
        """Odzyskuje połączenie z testerem przed cyklem.

        Bez tego pojedyncza utrata urządzenia (kod 2 albo 3) kasowała resztę serii:
        kolejne cykle kończyły się natychmiast komunikatem „PM125 nie jest połączony”,
        a raport pokazywał kilkanaście fałszywych ERROR-ów zamiast jednego zdarzenia.
        """
        hardware = self.engine.hardware
        if hardware.connected:
            return True

        for attempt in range(1, self.RECONNECT_ATTEMPTS + 1):
            if cancel_event and cancel_event.is_set():
                summary.cancelled = True
                return False
            if progress:
                progress(
                    LoopProgress(
                        cycle, summary.cycles_planned, "odzyskiwanie", "",
                        summary.passed, summary.failed, summary.errors,
                        time.monotonic() - started_at,
                    )
                )
            try:
                hardware.forget_state()
                restored = hardware.connect()
            except Exception as exc:
                logger.warning("Tryb walidacyjny: próba odzyskania %s nie powiodła się: %s",
                               attempt, exc)
                restored = False
            if restored:
                summary.reconnects += 1
                summary.problems.append(
                    f"cykl {cycle}: odzyskano połączenie z testerem (próba {attempt})"
                )
                logger.info("Tryb walidacyjny: odzyskano tester przed cyklem %s.", cycle)
                return True
            self._sleep(self.RECONNECT_PAUSE_SECONDS, cancel_event)

        reason = (
            f"tester nie odpowiada od cyklu {cycle} "
            f"({self.RECONNECT_ATTEMPTS} nieudane próby odzyskania): "
            f"{getattr(hardware, 'last_error', '') or 'brak urządzenia'}"
        )
        summary.aborted_reason = reason
        summary.problems.append(f"cykl {cycle}: PRZERWANO SERIĘ — {reason}")
        logger.error("Tryb walidacyjny: przerwano serię — %s", reason)
        return False

    @staticmethod
    def _sleep(seconds: float, cancel_event: threading.Event | None) -> None:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if cancel_event and cancel_event.is_set():
                return
            time.sleep(min(0.1, max(0.0, end - time.monotonic())))

    # --------------------------------------------------------------- statystyka

    @staticmethod
    def _voltage_statistics(results: list[TestResult]) -> list[PhaseStatistics]:
        """Statystyka napięcia per profil i faza, liczona z ocenianych próbek."""
        collected: dict[tuple[str, str], list[float]] = {}
        per_cycle: dict[tuple[str, str], list[float]] = {}
        limits: dict[str, tuple[float, float]] = {}
        order: dict[str, int] = {}

        for result in results:
            for profile_result in result.profiles:
                profile = profile_result.profile
                limits[profile.name] = (profile.min_voltage, profile.max_voltage)
                order[profile.name] = profile.index
                for phase, samples in (
                    (PHASE_NO_LOAD, profile_result.rated_no_load),
                    (PHASE_LOAD, profile_result.rated_loaded),
                ):
                    if not samples:
                        continue
                    key = (profile.name, phase)
                    values = [item.voltage for item in samples]
                    collected.setdefault(key, []).extend(values)
                    per_cycle.setdefault(key, []).append(statistics.fmean(values))

        output: list[PhaseStatistics] = []
        for (profile_name, phase), values in collected.items():
            low, high = limits[profile_name]
            statistic = PhaseStatistics(
                profile=profile_name,
                phase=phase,
                samples=len(values),
                minimum=min(values),
                maximum=max(values),
                average=statistics.fmean(values),
                deviation=statistics.pstdev(values) if len(values) > 1 else 0.0,
                limit_low=low,
                limit_high=high,
                margin_low=min(values) - low,
                margin_high=high - max(values),
                drift_mv_per_cycle=_slope(per_cycle[(profile_name, phase)]) * 1000.0,
            )
            output.append(statistic)
        # Kolejność profili jak w konfiguracji (5 V, 9 V, 12 V, 15 V), a nie alfabetyczna.
        output.sort(key=lambda item: (order.get(item.profile, 0), item.phase != PHASE_NO_LOAD))
        return output

    @staticmethod
    def _current_statistics(results: list[TestResult]) -> list[PhaseStatistics]:
        """Statystyka prądu pod obciążeniem — kontrola, czy obciążenie trzyma nastawę."""
        collected: dict[str, list[float]] = {}
        limits: dict[str, tuple[float, float]] = {}
        order: dict[str, int] = {}
        for result in results:
            for profile_result in result.profiles:
                samples = profile_result.rated_loaded
                if not samples:
                    continue
                profile = profile_result.profile
                low, high = profile.current_limits(loaded=True)
                limits[profile.name] = (low / 1000.0, high / 1000.0)
                order[profile.name] = profile.index
                collected.setdefault(profile.name, []).extend(item.current for item in samples)

        output: list[PhaseStatistics] = []
        for profile_name, values in collected.items():
            low, high = limits[profile_name]
            output.append(
                PhaseStatistics(
                    profile=profile_name,
                    phase=PHASE_LOAD,
                    samples=len(values),
                    minimum=min(values),
                    maximum=max(values),
                    average=statistics.fmean(values),
                    deviation=statistics.pstdev(values) if len(values) > 1 else 0.0,
                    limit_low=low,
                    limit_high=high,
                    margin_low=min(values) - low,
                    margin_high=high - max(values),
                )
            )
        output.sort(key=lambda item: order.get(item.profile, 0))
        return output


# --------------------------------------------------------------------- zapis


class LoopReportWriter:
    """Zapisuje dane serii (CSV) i czytelne podsumowanie (TXT)."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def write(self, summary: LoopSummary, results: list[TestResult]) -> tuple[Path | None, Path | None]:
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            safe_label = "".join(c for c in summary.label if c.isalnum() or c in "-_") or "PROBKA"
            data_path = self.directory / f"loop_{safe_label}_{stamp}.csv"
            report_path = self.directory / f"loop_{safe_label}_{stamp}_raport.txt"
            self._write_data(data_path, results)
            self._write_report(report_path, summary)
            summary.data_path = data_path
            summary.report_path = report_path
            return data_path, report_path
        except OSError as exc:
            logger.error("Nie zapisano raportu walidacyjnego: %s", exc)
            summary.problems.append(f"Nie zapisano raportu: {exc}")
            return None, None

    @staticmethod
    def _write_data(path: Path, results: list[TestResult]) -> None:
        headers = TestResult.csv_headers(results[0].profile_names) if results else ["Brak danych"]
        with path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.writer(handle, delimiter=";", quoting=csv.QUOTE_MINIMAL)
            writer.writerow(["Cykl", *headers])
            for index, result in enumerate(results, start=1):
                writer.writerow([index, *result.to_csv_row()])
            handle.flush()
            os.fsync(handle.fileno())

    @staticmethod
    def _write_report(path: Path, summary: LoopSummary) -> None:
        with path.open("w", encoding="utf-8", newline="\n") as handle:
            for line in build_report_lines(summary):
                handle.write(line + "\n")
            handle.flush()
            os.fsync(handle.fileno())


def build_report_lines(summary: LoopSummary) -> list[str]:
    """Treść raportu walidacyjnego — ta sama w pliku TXT i w oknie podsumowania."""
    lines: list[str] = []
    lines.append("=" * 78)
    lines.append(f"  RAPORT WALIDACYJNY STANOWISKA — PSU19 Tester {APP_VERSION}")
    lines.append("=" * 78)
    lines.append(f"Próbka:            {summary.label}")
    lines.append(f"Operator (HRID):   {summary.hrid}")
    lines.append(f"Start / koniec:    {summary.started} — {summary.finished}")
    note = ""
    if summary.cancelled:
        note = "  (seria zatrzymana przez operatora)"
    elif summary.aborted_reason:
        note = "  (seria przerwana — tester przestał odpowiadać)"
    lines.append(f"Cykle:             {summary.cycles_done} z {summary.cycles_planned}{note}")
    if summary.reconnects:
        lines.append(
            f"Odzyskania testera: {summary.reconnects} "
            "— tester zniknął w trakcie serii i został odzyskany"
        )
    if summary.aborted_reason:
        lines.append(f"Przerwanie:        {summary.aborted_reason}")
    lines.append("")
    lines.append(
        f"Wyniki:            PASS {summary.passed} · FAIL {summary.failed} · ERROR {summary.errors}"
        f"   (yield {summary.yield_percent:.1f} %)"
    )
    lines.append(
        f"Czas cyklu:        średnio {summary.average_duration:.1f} s"
        + (
            f"  (min {min(summary.durations):.1f} s, max {max(summary.durations):.1f} s)"
            if summary.durations else ""
        )
    )
    if summary.command_seconds:
        lines.append(f"Komenda PM125:     średnio {summary.average_command_ms:.0f} ms")
    lines.append("")

    lines.append("POWTARZALNOŚĆ STANOWISKA")
    lines.append("-" * 78)
    if summary.errors:
        lines.append(
            f"  NIE — {summary.errors} cykli zakończyło się błędem stanowiska. "
            "Stanowisko nie jest gotowe do produkcji."
        )
    elif summary.repeatable:
        verdict = "PASS" if summary.passed else "FAIL"
        lines.append(f"  TAK — wszystkie {summary.cycles_done} cykli dało ten sam werdykt ({verdict}).")
    else:
        lines.append(
            f"  NIE — werdykt zmieniał się w serii (PASS {summary.passed} / FAIL {summary.failed}). "
            "Sprawdź margines do granic poniżej."
        )
    lines.append("")

    lines.append("NAPIĘCIE — margines do granic kryteriów")
    lines.append("-" * 78)
    lines.append(
        f"{'Profil':<8}{'Faza':<16}{'min [V]':>9}{'max [V]':>9}{'śr [V]':>9}"
        f"{'σ [mV]':>9}{'zapas [mV]':>12}{'zapas [%]':>11}"
    )
    for item in summary.phases:
        lines.append(
            f"{item.profile:<8}{PHASE_LABELS[item.phase]:<16}"
            f"{item.minimum:>9.3f}{item.maximum:>9.3f}{item.average:>9.3f}"
            f"{item.deviation * 1000:>9.1f}{item.worst_margin * 1000:>12.0f}"
            f"{item.worst_margin_percent:>11.1f}"
        )
    lines.append("")
    lines.append("  zapas = odległość najgorszego pomiaru serii od bliższej granicy kryterium")
    lines.append("  zapas [%] = zapas odniesiony do szerokości okna kryterium")
    lines.append("")

    worst = summary.worst_phase
    if worst is not None:
        lines.append(
            f"  Najciaśniej: {worst.profile} {PHASE_LABELS[worst.phase]} — "
            f"zapas {worst.worst_margin * 1000:.0f} mV ({worst.worst_margin_percent:.1f} %), "
            f"granice {worst.limit_low:.2f}–{worst.limit_high:.2f} V"
        )
        if worst.worst_margin_percent < 5:
            lines.append(
                "  UWAGA: zapas poniżej 5 % szerokości okna — przy rozrzucie produkcyjnym "
                "należy się spodziewać fałszywych odrzutów."
            )
        elif worst.worst_margin_percent < 10:
            lines.append("  Zapas poniżej 10 % szerokości okna — warto obserwować.")
        lines.append("")

    drifting = [item for item in summary.phases if abs(item.drift_mv_per_cycle) >= 1.0]
    if drifting:
        lines.append("DRYF W SERII (trend napięcia między cyklami)")
        lines.append("-" * 78)
        for item in drifting:
            total = item.drift_mv_per_cycle * max(1, summary.cycles_done - 1)
            lines.append(
                f"  {item.profile:<6} {PHASE_LABELS[item.phase]:<16} "
                f"{item.drift_mv_per_cycle:+.2f} mV/cykl  (łącznie {total:+.0f} mV w serii)"
            )
        lines.append("")
        lines.append("  Systematyczny dryf zwykle oznacza nagrzewanie się zasilacza lub obciążenia.")
        lines.append("")

    if summary.current_stats:
        lines.append("PRĄD POD OBCIĄŻENIEM — czy obciążenie trzyma nastawę")
        lines.append("-" * 78)
        lines.append(
            f"{'Profil':<8}{'min [A]':>10}{'max [A]':>10}{'śr [A]':>10}{'σ [mA]':>10}"
            f"{'granice [A]':>18}"
        )
        for item in summary.current_stats:
            lines.append(
                f"{item.profile:<8}{item.minimum:>10.3f}{item.maximum:>10.3f}"
                f"{item.average:>10.3f}{item.deviation * 1000:>10.1f}"
                f"{item.limit_low:>9.2f}–{item.limit_high:<8.2f}"
            )
        lines.append("")

    resistances = summary.source_resistances
    if resistances:
        lines.append("REZYSTANCJA TORU — zasilacz + kabel + styk + wejście testera")
        lines.append("-" * 78)
        lines.append(
            f"{'Profil':<8}{'U bez obc. [V]':>16}{'U pod obc. [V]':>16}"
            f"{'spadek [mV]':>13}{'prąd [A]':>10}{'R [mΩ]':>10}"
        )
        for item in resistances:
            lines.append(
                f"{item.profile:<8}{item.no_load_volts:>16.3f}{item.loaded_volts:>16.3f}"
                f"{item.drop_volts * 1000:>13.0f}{item.current_amps:>10.3f}"
                f"{item.milliohms:>10.0f}"
            )
        lines.append(f"{'ŚREDNIO':<8}{'':>16}{'':>16}{'':>13}{'':>10}"
                     f"{summary.average_milliohms:>10.0f}")
        lines.append("")
        lines.append(
            "  Spadek napięcia zależy od prądu profilu, rezystancja nie — dlatego to ona"
        )
        lines.append(
            "  nadaje się do porównywania gniazd. Różnica R między stanowiskami oznacza"
        )
        lines.append(
            "  gorszy kabel, zużyte gniazdo albo słaby styk, a nie gorszy wyrób: ten sam"
        )
        lines.append(
            "  zasilacz da na takim gnieździe niższe napięcie pod obciążeniem i wpadnie"
        )
        lines.append("  w fałszywy FAIL.")
        lines.append("")

    if summary.problems:
        lines.append("ZDARZENIA W SERII")
        lines.append("-" * 78)
        for problem in summary.problems[:40]:
            lines.append(f"  {problem}")
        if len(summary.problems) > 40:
            lines.append(f"  ... oraz {len(summary.problems) - 40} dalszych wpisów (patrz log)")
        lines.append("")

    lines.append("WNIOSEK")
    lines.append("-" * 78)
    for line in verdict_lines(summary):
        lines.append(f"  {line}")
    lines.append("")
    lines.append("Seria walidacyjna nie trafia do raportu produkcyjnego i nie zużywa licznika prób.")
    return lines


def verdict_lines(summary: LoopSummary) -> list[str]:
    """Ocena serii — osobne zdanie na każdy wniosek.

    Wnioski nie wykluczają się nawzajem: problem z łącznością nie może przykryć
    informacji o ciasnym marginesie, bo to dwie różne przyczyny i dwa różne działania.
    """
    if summary.cycles_done == 0:
        return ["Brak wykonanych cykli."]

    lines: list[str] = []
    worst = summary.worst_phase

    if summary.aborted_reason:
        lines.append(
            f"Seria PRZERWANA: {summary.aborted_reason}. Statystyka poniżej liczy się "
            f"z {summary.cycles_done} wykonanych cykli i jest niepełna."
        )
    if summary.errors:
        lines.append(
            f"Stanowisko NIEGOTOWE: {summary.errors} z {summary.cycles_done} cykli zakończyło się "
            "błędem stanowiska. Usuń przyczynę przed wdrożeniem."
        )
    if summary.reconnects and not summary.aborted_reason:
        lines.append(
            f"Tester zniknął i wrócił {summary.reconnects} raz(y) w serii. To problem "
            "połączenia (kabel, port, zasilanie huba), nie wyrobu — na produkcji objawi się "
            "losowymi błędami stanowiska."
        )

    if worst is not None and worst.worst_margin < 0:
        lines.append(
            f"Kryterium PRZEKROCZONE w serii: {worst.profile} {PHASE_LABELS[worst.phase]} "
            f"zeszło {abs(worst.worst_margin) * 1000:.0f} mV poza granicę. Ten sam egzemplarz "
            "raz przechodzi, raz nie — stanowisko albo wyrób leżą dokładnie na progu."
        )
    elif not summary.repeatable and not summary.errors:
        lines.append(
            "Wynik NIEPOWTARZALNY: ten sam egzemplarz raz przechodzi, raz nie. "
            "Zwykle oznacza to kryterium przechodzące przez rozrzut pomiaru."
        )
    elif worst is not None and worst.worst_margin_percent < 5:
        lines.append(
            f"Margines jest ciasny ({worst.profile} {PHASE_LABELS[worst.phase]}: "
            f"{worst.worst_margin_percent:.1f} %). Przed wdrożeniem sprawdź serię na kilku "
            "egzemplarzach."
        )

    if not lines:
        if summary.failed == summary.cycles_done:
            return [
                "Egzemplarz konsekwentnie FAIL we wszystkich cyklach — wynik wiarygodny, "
                "ale to ocena wyrobu, nie stanowiska."
            ]
        return [
            f"Stanowisko powtarzalne: {summary.cycles_done} cykli, wszystkie PASS, "
            f"najmniejszy zapas {worst.worst_margin_percent:.1f} %."
            if worst
            else f"Stanowisko powtarzalne: {summary.cycles_done} cykli, wszystkie PASS."
        ]
    return lines


def _slope(values: list[float]) -> float:
    """Nachylenie prostej regresji (wartość na cykl). Zwraca 0 dla serii krótszych niż 2."""
    count = len(values)
    if count < 2:
        return 0.0
    mean_x = (count - 1) / 2.0
    mean_y = statistics.fmean(values)
    numerator = sum((index - mean_x) * (value - mean_y) for index, value in enumerate(values))
    denominator = sum((index - mean_x) ** 2 for index in range(count))
    return numerator / denominator if denominator else 0.0
