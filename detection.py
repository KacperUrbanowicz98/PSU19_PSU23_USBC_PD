"""Dopasowanie wykrytych testerów PM125 do skonfigurowanych stanowisk.

Numer seryjny testera potrafi się zmienić (wymiana sztuki, inne gniazdo USB,
przeinstalowanie sterownika). Wtedy aplikacja nie rozpoznaje gniazda, choć tester
fizycznie jest podłączony. Ten moduł wylicza — bez dotykania GUI i bez zapisu na
dysk — jak przepiąć stanowiska na faktycznie widoczne numery seryjne, zachowując
nazwy gniazd i ich kolejność. Logika jest osobno, żeby dało się ją przetestować.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace

from config import StationConfig, station_names_for


@dataclass(frozen=True, slots=True)
class StationRemapPlan:
    """Propozycja nowego przypisania testerów do stanowisk."""

    #: Pełna lista stanowisk po zmianie (łącznie z wyłączonymi, nietkniętymi).
    stations: list[StationConfig] = field(default_factory=list)
    #: Opis zmian dla operatora i wpisu audytowego.
    changes: list[str] = field(default_factory=list)
    #: Stanowiska, dla których nie ma wolnego testera.
    missing: list[str] = field(default_factory=list)
    #: Stanowiska, których numer seryjny już się zgadzał.
    unchanged: list[str] = field(default_factory=list)
    #: Nazwy stanowisk dołożonych, bo wykryto więcej testerów niż gniazd.
    added: list[str] = field(default_factory=list)

    @property
    def has_changes(self) -> bool:
        return bool(self.changes)

    @property
    def requires_restart(self) -> bool:
        """Dołożenie kanału zmienia układ okna — to wymaga ponownego uruchomienia."""
        return bool(self.added)

    def summary(self) -> str:
        lines: list[str] = []
        if self.changes:
            lines.append("Zmiany:")
            lines.extend(f"  • {item}" for item in self.changes)
        if self.unchanged:
            lines.append("Bez zmian: " + ", ".join(self.unchanged))
        if self.missing:
            lines.append("Brak testera dla: " + ", ".join(self.missing))
        return "\n".join(lines)


def _unique_serials(detected: list[tuple[int, str]]) -> list[str]:
    serials: list[str] = []
    for _index, serial in detected:
        upper = str(serial).strip().upper()
        if upper and upper not in serials:
            serials.append(upper)
    return serials


def _unique_name(candidate: str, used: set[str]) -> str:
    name = candidate
    suffix = 2
    while name in used:
        name = f"{candidate} {suffix}"
        suffix += 1
    used.add(name)
    return name


def plan_station_remap(
    stations: list[StationConfig], detected: list[tuple[int, str]]
) -> StationRemapPlan:
    """Przypisuje wykryte numery seryjne do stanowisk, zachowując ich nazwy.

    Zasady:
    * stanowisko, którego numer seryjny jest widoczny, zostaje bez zmian,
    * pozostałe wolne testery trafiają do pozostałych stanowisk w kolejności listy,
    * nadmiarowe testery tworzą nowe stanowiska (to wymaga restartu aplikacji),
    * stanowiska bez wolnego testera zostają nietknięte i są zgłoszone jako brakujące,
    * stanowiska wyłączone są przepisywane bez zmian i nie biorą udziału w dopasowaniu.
    """
    serials = _unique_serials(detected)
    result = list(stations)
    active_positions = [
        position for position, station in enumerate(stations) if station.enabled
    ]

    taken: set[str] = set()
    unchanged: list[str] = []
    for position in active_positions:
        current = (stations[position].device_serial or "").strip().upper()
        if current and current != "ANY" and current in serials and current not in taken:
            taken.add(current)
            unchanged.append(stations[position].name)

    free = [serial for serial in serials if serial not in taken]
    changes: list[str] = []
    missing: list[str] = []
    for position in active_positions:
        station = stations[position]
        if station.name in unchanged:
            continue
        if not free:
            missing.append(station.name)
            continue
        serial = free.pop(0)
        result[position] = replace(station, device_serial=serial, enabled=True)
        previous = station.device_serial or "Any"
        changes.append(f"{station.name}: {previous} → {serial}")

    added: list[str] = []
    if free:
        used_names = {station.name for station in result}
        default_names = station_names_for(len(active_positions) + len(free))
        for offset, serial in enumerate(free):
            index = len(active_positions) + offset
            candidate = (
                default_names[index] if index < len(default_names) else f"STANOWISKO {index + 1}"
            )
            name = _unique_name(candidate, used_names)
            result.append(StationConfig(name, serial, True))
            changes.append(f"nowe stanowisko {name}: {serial}")
            added.append(name)

    return StationRemapPlan(
        stations=result,
        changes=changes,
        missing=missing,
        unchanged=unchanged,
        added=added,
    )
