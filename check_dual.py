"""Diagnostyka pod stanowisko dwustanowiskowe (dwa PM125 na jednym PC).

Wersja 2 — dopasowana do rzeczywistej składni USBPDConsole V1.0.1016.

UWAGA DO WERSJI 1: pierwsza wersja próbowała listować urządzenia przełącznikiem -l.
W tej konsoli -l to --load, czyli USTAWIENIE PRĄDU OBCIĄŻENIA. Wersja 1 nie podawała
argumentu, więc komenda była odrzucana, ale przełącznik był użyty błędnie.
Ta wersja używa wyłącznie komend odczytu:

    -h/--help         pomoc
    -f/--find         wykrycie urządzeń i ich indeksów
    -c/--connection   status połączenia
    -s/--stats        status DUT
    -p/--profiles     profile deklarowane przez DUT
    -r/--config       konfiguracja testera

Żadna z nich nie zmienia stanu testera ani obciążenia podłączonego zasilacza.
Przełączniki ustawiające (-v, -l, -q, -m, -n) nie są tu w ogóle wywoływane.

Uruchomienie:

    py check_dual.py                                   # wykrycie przez --find
    py check_dual.py --serials PMPD4VLH7Y PMPD0000XX   # jawne numery seryjne
    py check_dual.py --indexes 0 1                     # adresacja po indeksie
    py check_dual.py --console "C:\\USBPDAPI\\USBPDConsole.exe"

Pełna pomoc konsoli zapisuje się do pliku console_help.txt obok skryptu.
"""
from __future__ import annotations

import argparse
import re
import statistics
import subprocess
import sys
import threading
import time
from pathlib import Path

DEFAULT_CONSOLE = r"C:\USBPDAPI\USBPDConsole.exe"
REPEATS = 10

#: Komendy, które wyłącznie czytają stan. Nic poza tą listą nie jest tu wywoływane.
READ_ONLY = ("-h", "-f", "-c", "-s", "-p", "-r")

#: Kody powrotu USBPDConsole V1.0.1016 (sekcja "Return Codes" w pomocy).
RETURN_CODES = {
    0: "Succeed",
    1: "Invalid command line",
    2: "No device detected",
    3: "Failed to connect",
    4: "Invalid profile",
    5: "Command is not supported by PD tester model",
    6: "Device firmware update required",
}
#: Kody oznaczające realną utratę testera (w odróżnieniu od błędu samej komendy).
DEVICE_LOST_CODES = frozenset({2, 3})


def describe(code: int) -> str:
    return RETURN_CODES.get(code, f"kod {code}")


def run(console: str, *arguments: str, timeout: float = 15.0) -> tuple[int, str, float]:
    """Uruchamia konsolę i zwraca (kod wyjścia, wyjście, czas w sekundach)."""
    for argument in arguments:
        if argument.startswith("-") and argument not in READ_ONLY and not argument.startswith("-d"):
            if argument not in ("-i",):
                raise RuntimeError(f"Przełącznik {argument} nie jest na liście komend odczytu.")
    command = [console, *arguments]
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
            command, capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout, startupinfo=startupinfo, creationflags=creationflags, check=False,
        )
    except subprocess.TimeoutExpired:
        return -1, "<timeout>", time.monotonic() - started
    except OSError as exc:
        return -2, f"<nie uruchomiono: {exc}>", time.monotonic() - started
    output = "\n".join(
        part for part in ((completed.stdout or "").strip(), (completed.stderr or "").strip()) if part
    )
    return completed.returncode, output, time.monotonic() - started


class Tee:
    """Dubluje wydruk na konsolę i do pliku — cały raport w jednym miejscu."""

    def __init__(self, path: Path) -> None:
        self.stream = sys.stdout
        self.file = path.open("w", encoding="utf-8", newline="\n")

    def write(self, text: str) -> int:
        self.stream.write(text)
        self.file.write(text)
        return len(text)

    def flush(self) -> None:
        self.stream.flush()
        self.file.flush()

    def close(self) -> None:
        try:
            self.file.close()
        except OSError:
            pass


def heading(text: str) -> None:
    print()
    print("=" * 76)
    print(f"  {text}")
    print("=" * 76)


def dump_help(console: str) -> None:
    """Zapisuje pełną pomoc konsoli — w konsoli jest więcej opcji, niż opisuje dokumentacja."""
    heading("1. Pełna pomoc konsoli")
    code, output, seconds = run(console, "-h")
    target = Path(__file__).resolve().parent / "console_help.txt"
    try:
        target.write_text(output, encoding="utf-8")
        print(f"  Zapisano pełną pomoc do: {target}  (kod {code}, {seconds * 1000:.0f} ms)")
    except OSError as exc:
        print(f"  Nie zapisano pomocy do pliku: {exc}")
    print()
    for line in output.splitlines():
        print(f"  {line}")


def find_devices(console: str) -> list[tuple[str, str]]:
    """Wykrywa urządzenia komendą --find. Zwraca listę (indeks, numer seryjny)."""
    heading("2. Wykrywanie urządzeń (--find)")
    code, output, seconds = run(console, "-f")
    print(f"  {console} -f   -> kod {code}, {seconds * 1000:.0f} ms\n")
    for line in output.splitlines():
        print(f"      {line}")

    devices: list[tuple[str, str]] = []
    for line in output.splitlines():
        # Spodziewany kształt: "Index: 0  Serial: PMPD4VLH7Y" albo "0: PMPD4VLH7Y".
        index_match = re.search(r"\b(?:index|idx)\D{0,4}(\d+)", line, flags=re.IGNORECASE)
        serial_match = re.search(r"\b(PM[A-Z0-9]{6,})\b", line.upper())
        if serial_match:
            index = index_match.group(1) if index_match else str(len(devices))
            devices.append((index, serial_match.group(1)))

    print()
    if devices:
        print(f"  Wykryte urządzenia: {devices}")
    else:
        print("  Nie rozpoznano urządzeń w odpowiedzi --find.")
        print("  Podaj je ręcznie: --serials <A> <B>   albo   --indexes 0 1")
    return devices


def check_address(console: str, selector: str, value: str) -> tuple[bool, bool, str]:
    """Sprawdza, czy selektor trafia w tester.

    UWAGA: -c/--connection zwraca status DUT (zasilacza wpiętego do testera), a nie
    status testera wobec PC. "STATUS:NOT CONNECTED" z kodem 0 oznacza: tester
    odpowiedział poprawnie, ale w gnieździe nie ma zasilacza. Dla adresacji liczy się
    kod powrotu, nie treść statusu.
    """
    code, output, seconds = run(console, selector, value, "-c")
    upper = output.upper()
    tester_ok = code == 0 and "STATUS:" in upper
    dut_present = tester_ok and "NOT CONNECTED" not in upper and "CONNECTED" in upper
    mark = "OK  " if tester_ok else "BŁĄD"
    first = output.splitlines()[0] if output else ""
    print(
        f"  [{mark}] {selector} {value:<16} {describe(code):<28} "
        f"{seconds * 1000:>5.0f} ms   {first[:40]}"
    )
    return tester_ok, dut_present, output


def measure(
    console: str, selector: str, value: str, repeats: int, command: str = "-s"
) -> tuple[list[float], int]:
    """Zwraca czasy odczytów i liczbę niepowodzeń — to drugie jest tłem odniesienia.

    Bez wpiętego zasilacza komenda -s może zwracać niezerowy kod. To nie jest kolizja
    dwóch instancji, tylko brak DUT, więc musimy znać ten poziom przed pomiarem
    równoległości.
    """
    times: list[float] = []
    failures = 0
    for _ in range(repeats):
        code, output, seconds = run(console, selector, value, command)
        times.append(seconds)
        if code != 0 or "ERROR" in output.upper():
            failures += 1
    return times, failures


def parallel_pass(
    console: str, selector: str, first: str, second: str, repeats: int
) -> tuple[float, int]:
    """Odpala serie odczytów na obu urządzeniach JEDNOCZEŚNIE, w dwóch wątkach."""
    errors = [0]
    lock = threading.Lock()

    def worker(value: str) -> None:
        local = 0
        for _ in range(repeats):
            code, output, _seconds = run(console, selector, value, "-c")
            if code in DEVICE_LOST_CODES or "ERROR" in output.upper():
                local += 1
        with lock:
            errors[0] += local

    threads = [threading.Thread(target=worker, args=(value,)) for value in (first, second)]
    started = time.monotonic()
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return time.monotonic() - started, errors[0]


def run_diagnostics() -> int:
    parser = argparse.ArgumentParser(description="Diagnostyka dwóch testerów PM125")
    parser.add_argument("--console", default=DEFAULT_CONSOLE, help="ścieżka do USBPDConsole.exe")
    parser.add_argument("--serials", nargs=2, metavar=("A", "B"), help="numery seryjne obu testerów")
    parser.add_argument("--indexes", nargs=2, metavar=("A", "B"), help="indeksy obu testerów")
    parser.add_argument("--repeats", type=int, default=REPEATS, help="liczba powtórzeń pomiaru")
    args = parser.parse_args()

    console = args.console
    print(f"Konsola: {console}")
    print("Skrypt wywołuje wyłącznie komendy odczytu: -h, -f, -c, -s. Nie ustawia profilu")
    print("ani obciążenia, więc jest bezpieczny dla podłączonego zasilacza.")
    if not Path(console).is_file():
        print("\nBŁĄD: nie znaleziono USBPDConsole.exe pod tą ścieżką.")
        return 2

    dump_help(console)

    if args.indexes:
        selector, values = "-i", list(args.indexes)
    elif args.serials:
        selector, values = "-d", list(args.serials)
    else:
        devices = find_devices(console)
        if len(devices) < 2:
            print("\n  Do dalszych kroków potrzebne są dwa urządzenia.")
            print("  Podłącz oba i powtórz, albo podaj je ręcznie (--serials / --indexes).")
            return 3
        selector = "-d"
        values = [serial for _index, serial in devices[:2]]

    heading(f"3. Czy {selector} wybiera konkretne urządzenie")
    print("  -c zwraca status DUT (zasilacza), nie testera. Bez wpiętego zasilacza")
    print("  poprawną odpowiedzią jest STATUS:NOT CONNECTED z kodem Succeed.\n")
    print(f"  Sprawdzane: A = {values[0]}, B = {values[1]}\n")
    ok_a, dut_a, _ = check_address(console, selector, values[0])
    ok_b, dut_b, _ = check_address(console, selector, values[1])
    # Fałszywy adres w POPRAWNYM formacie — inaczej konsola odrzuci go już na parsowaniu.
    bogus_value = "99" if selector == "-i" else "PMPD00000A"
    bogus, _, _ = check_address(console, selector, bogus_value)
    print()
    for name, present in (("A", dut_a), ("B", dut_b)):
        print(f"  Zasilacz w gnieździe {name}: {'TAK' if present else 'nie'}")
    print()
    if not (ok_a and ok_b):
        print("  WNIOSEK: co najmniej jeden tester nie odpowiada na własny adres.")
        print("  Sprawdź, czy oba PM125 są podłączone i widoczne w --find.")
        print("  Dalsze pomiary pominięte.")
        return 4
    if bogus:
        print("  UWAGA: konsola odpowiedziała poprawnie dla nieistniejącego adresu —")
        print("  selektor może być ignorowany i obie instancje trafią w to samo urządzenie.")
        return 5
    print(f"  WNIOSEK: adresacja przez {selector} działa poprawnie.")

    heading("3a. Konfiguracja testerów (-r)")
    print("  Porównaj oba wydruki. Różnice w maxload / defload / defvolt / holdload")
    print("  oznaczają, że testery nie są skonfigurowane jednakowo.\n")
    for name, value in (("A", values[0]), ("B", values[1])):
        code, output, seconds = run(console, selector, value, "-r")
        print(f"  --- Tester {name} ({value}) — {describe(code)}, {seconds * 1000:.0f} ms")
        for line in output.splitlines():
            print(f"      {line}")
        print()

    heading("3b. Profile deklarowane przez DUT (-p)")
    print("  Z wpiętym zasilaczem ta komenda mówi, jakie profile wyrób faktycznie oferuje.\n")
    for name, value in (("A", values[0]), ("B", values[1])):
        code, output, seconds = run(console, selector, value, "-p")
        print(f"  --- Gniazdo {name} ({value}) — {describe(code)}, {seconds * 1000:.0f} ms")
        for line in output.splitlines()[:12]:
            print(f"      {line}")
        print()

    heading("4. Czas pojedynczej komendy")
    print("  Mierzone na -c (działa niezależnie od obecności zasilacza) oraz na -s.\n")
    for command in ("-c", "-s"):
        samples: list[float] = []
        for value in values:
            times, _failures = measure(console, selector, value, args.repeats, command)
            samples.extend(times)
        print(
            f"  {command}: średnio {statistics.fmean(samples) * 1000:6.0f} ms   "
            f"(min {min(samples) * 1000:.0f}, max {max(samples) * 1000:.0f})"
        )
    print()
    times_a, failures_a = measure(console, selector, values[0], args.repeats)
    times_b, failures_b = measure(console, selector, values[1], args.repeats)
    for name, times in (("A", times_a), ("B", times_b)):
        print(
            f"  Urządzenie {name}: średnio {statistics.fmean(times) * 1000:6.0f} ms   "
            f"(min {min(times) * 1000:.0f}, max {max(times) * 1000:.0f})"
        )
    baseline = failures_a + failures_b
    if baseline:
        print(
            f"\n  Odczyt -s zwrócił błąd {baseline} razy na {args.repeats * 2} prób "
            "przy pracy POJEDYNCZEJ."
        )
        print("  To normalne, gdy do testerów nie jest wpięty zasilacz — ten poziom")
        print("  traktujemy jako tło i odejmujemy od pomiaru równoległego.")
    single = statistics.fmean(times_a + times_b)

    heading("4a. Czas odwołania do nieobecnego urządzenia")
    print("  Decyduje o tym, jak szybko aplikacja wykryje odpięty tester w trakcie testu.\n")
    missing_value = "99" if selector == "-i" else "PMPD00000A"
    missing_times: list[float] = []
    for _ in range(3):
        _code, _output, seconds = run(console, selector, missing_value, "-c")
        missing_times.append(seconds)
    print(
        f"  {selector} {missing_value}: średnio {statistics.fmean(missing_times) * 1000:.0f} ms "
        f"(min {min(missing_times) * 1000:.0f}, max {max(missing_times) * 1000:.0f})"
    )
    print(f"  Zalecany command_timeout: co najmniej {max(missing_times) * 3:.1f} s")

    heading("5. Równoległość dwóch instancji konsoli")
    sequential_expected = single * args.repeats * 2
    print(f"  Czas oczekiwany przy pełnej serializacji: {sequential_expected:.1f} s")
    print(f"  Czas oczekiwany przy pełnej równoległości: {sequential_expected / 2:.1f} s")
    print("  Trwa pomiar...")
    elapsed, errors = parallel_pass(console, selector, values[0], values[1], args.repeats)
    ratio = elapsed / sequential_expected if sequential_expected else 1.0
    collisions = max(0, errors - baseline)
    print(f"\n  Zmierzony czas: {elapsed:.1f} s   (współczynnik {ratio:.2f})")
    print(f"  Błędy przy pracy równoległej: {errors}   (tło pojedyncze: {baseline})")
    print(f"  Nadwyżka ponad tło = realne kolizje: {collisions}")

    heading("WNIOSEK")
    if collisions:
        print("  Konsola zgłaszała błędy przy równoległych wywołaniach.")
        print("  Dwa stanowiska wymagają SERIALIZACJI komend (wspólna blokada w aplikacji).")
        mode = "serializacja"
    elif ratio < 0.65:
        print("  Dwie instancje konsoli działają RÓWNOLEGLE.")
        print("  Możliwe jest pełne stanowisko dwukanałowe z ~dwukrotną przepustowością.")
        mode = "równolegle"
    elif ratio > 0.85:
        print("  Instancje konsoli SERIALIZUJĄ SIĘ (czas równoległy ≈ sekwencyjny).")
        print("  Dwa gniazda dadzą zysk tylko z nakładania manipulacji operatora na test.")
        mode = "serializacja"
    else:
        print("  Wynik niejednoznaczny — częściowa równoległość. Powtórz pomiar.")
        mode = "niejednoznaczny"

    print()
    print(f"  Czas jednej komendy:        {single * 1000:.0f} ms")
    print(f"  Adresacja:                  {selector} {values[0]} / {selector} {values[1]}")
    if mode == "równolegle":
        print(f"  Próbek na fazę 2,5 s:       ~{int(2.5 / single)} na stanowisko (bez zmian)")
        print("  Zalecenie: osobny wątek i osobny klient PM125 na stanowisko, wspólny raport")
        print("             z kolumną stanowiska i wspólny licznik prób.")
    elif mode == "serializacja":
        per_phase = int(2.5 / (single * 2))
        print(f"  Próbek na fazę 2,5 s:       ~{per_phase} na stanowisko przy pracy równoczesnej")
        if per_phase - 1 < 2:
            print("  UWAGA: przy równoczesnych fazach zostaje za mało ocenianych próbek —")
            print("         testy muszą iść NAPRZEMIENNIE (cały test A, potem cały test B).")
    return 0


def main() -> int:
    report_path = Path(__file__).resolve().parent / "check_dual_report.txt"
    tee = Tee(report_path)
    original = sys.stdout
    sys.stdout = tee
    try:
        code = run_diagnostics()
    finally:
        sys.stdout = original
        tee.close()
    print()
    print("=" * 76)
    print(f"  Pełny raport zapisany do: {report_path}")
    print("  Wklej TEN plik — zawiera cały przebieg, nie tylko pomoc konsoli.")
    print("=" * 76)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
