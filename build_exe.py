"""Builder EXE aplikacji PSU19 Tester (PM125).

Buduje aplikację PyInstallerem w trybie ONEDIR, bez podpisu cyfrowego.
Plik ``test_config.json`` zostaje OBOK EXE i jest edytowalny z panelu
inżynieryjnego — razem z folderami danych, które aplikacja zakłada sama.

    py -m pip install -r requirements-dev.txt
    py build_exe.py

Wynik: folder ``dist\\PSU19 Tester\\`` — kopiuje się CAŁY FOLDER, nie samo EXE.

Kontrole przy budowaniu i co każda z nich RZECZYWIŚCIE daje:

1. Środowisko budowania — wersja Pythona i wersja PyInstallera. Wykrywa
   zbudowanie wydania na przypadkowej maszynie albo na innym interpreterze
   niż zatwierdzony.

2. Markery w źródłach. Każdy plik musi zawierać wskazane elementy (nazwy
   funkcji stanowiących bramki) i nie może zawierać zabronionych.
   Wykrywa: wycięcie całej bramki przy refaktoryzacji, jawne hasło zostawione
   w kodzie po debugowaniu.
   NIE wykrywa: podmiany TREŚCI funkcji przy zachowanej nazwie. To kontrola
   pomyłek, nie zabezpieczenie przed świadomą zmianą.

3. Preflight — kompilacja wszystkich źródeł, pełne testy automatyczne
   (``selftest.py``) i kontrola ZACHOWANIA aplikacji: że domyślna konfiguracja
   przechodzi walidację, że hasło inżynieryjne działa, a puste i błędne
   odpada, że progi profili odpowiadają specyfikacji wyrobu i że czasy faz
   mieszczą się w timeoucie testu.

4. ``approved_sources.json`` — sumy SHA-256 plików źródłowych. Gdy plik
   istnieje, każda rozbieżność zatrzymuje build.
   Daje: wykrycie zbudowania EXE ze starej albo roboczej kopii pliku.
   NIE daje: dowodu integralności wydania — lista leży obok źródeł, nie jest
   podpisana i regeneruje ją ``--approve``. Nie opisuj jej w dokumentacji
   jakościowej jako kontroli bezpieczeństwa. Wygenerujesz ją poleceniem:
       py build_exe.py --approve

Numer wersji ma jedno źródło: ``APP_VERSION`` w ``config.py``. Builder czyta
go stamtąd, raport walidacyjny też — dzięki temu wersja w raporcie ze
stanowiska nie może rozjechać się z wersją binarki.
"""
from __future__ import annotations

import hashlib
import importlib.metadata
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional

from config import APP_VERSION as SOURCE_APP_VERSION

APP_NAME = "PSU19 Tester"
APP_DESCRIPTION = "PSU USB-C PD Tester (PassMark PM125)"
COMPANY_NAME = "Reconext"
VERSION = f"{SOURCE_APP_VERSION}.0"
COPYRIGHT = "Reconext 2026"

REQUIRED_PYTHON_MINIMUM = (3, 10)
REQUIRED_PACKAGES = {
    "PyInstaller": ("pyinstaller", (6, 0), (7, 0)),
}

ROOT_DIR = Path(__file__).resolve().parent
DIST_DIR = ROOT_DIR / "dist"
BUILD_DIR = ROOT_DIR / "build"
STAGING_DIR = BUILD_DIR / "_psu19_staging"
OUTPUT_DIR = DIST_DIR / APP_NAME
EXE_PATH = OUTPUT_DIR / f"{APP_NAME}.exe"
APPROVED_SOURCES_FILE = ROOT_DIR / "approved_sources.json"

#: Moduły aplikacji — struktura płaska, wszystkie leżą obok app.py.
PROJECT_FILES = [
    "app.py",
    "config.py",
    "detection.py",
    "engine.py",
    "hardware.py",
    "logging_utils.py",
    "loop_test.py",
    "loop_ui.py",
    "models.py",
    "settings_ui.py",
    "station_panel.py",
    "storage.py",
    "theme.py",
    "ui.py",
]

#: Pliki, które mają zostać OBOK EXE i być edytowalne na stanowisku.
EDITABLE_DATA_FILES = [
    "test_config.json",
]

#: Dokumentacja kopiowana razem z aplikacją — na stanowisku bywa potrzebna
#: bez dostępu do repozytorium.
OPTIONAL_DATA_FILES = [
    "README_PL.md",
    "INSTALACJA_PL.md",
    "CHANGELOG_PL.md",
]

OPTIONAL_ICONS = ("reconext_logo.ico", "psu19.ico", "icon.ico")

HIDDEN_IMPORTS = [
    "tkinter", "tkinter.ttk", "tkinter.messagebox", "tkinter.simpledialog",
    "tkinter.filedialog", "tkinter.scrolledtext",
    "hashlib", "csv", "queue", "statistics",
] + [name[:-3] for name in PROJECT_FILES]

#: Elementy, które muszą być obecne. Każdy odpowiada bramce, której wycięcie
#: przeszłoby niezauważone, bo aplikacja nadal by się uruchomiła.
REQUIRED_MARKERS = {
    "config.py": (
        # Hasło inżynieryjne: statyczne, weryfikowane skrótem.
        "def verify_engineering_password",
        "DEFAULT_ENGINEERING_PASSWORD_HASH",
        # Walidacja konfiguracji — bez niej da się zapisać ustawienia, przy
        # których w fazie nie zmieści się wymagana liczba próbek.
        "def validate",
        "def estimated_samples_per_phase",
        # Przy wielu gniazdach adres „Any" wybrałby to samo urządzenie.
        "przy wielu stanowiskach każdy tester musi mieć",
        # Dziennik zmian konfiguracji.
        "def append_audit",
        "APP_VERSION",
    ),
    "models.py": (
        "def profiles_signature",
        # Werdykt profilu liczony z próbek OCENIANYCH, nigdy ze stabilizacji.
        "def rated_no_load",
        "def rated_loaded",
        "def finalize",
        # Za mało ocenianych pomiarów to NO_DATA, nie PASS.
        "ProfileStatus.NO_DATA",
        "def violation",
    ),
    "engine.py": (
        # Błąd stanowiska ma pierwszeństwo nad werdyktem wyrobu.
        "STATION_PROBLEM_STATUSES",
        "def _calculate_final_status",
        # Bezpieczny stan po teście: 5 V / 0 mA.
        "safe_reset",
        # Utrata testera w trakcie testu nazywana wprost.
        "Tester przestał odpowiadać w trakcie testu",
        # Okno stabilizacji: próbki zapisane, ale nieoceniane.
        "settling",
    ),
    "hardware.py": (
        # Kody powrotu konsoli: 2 i 3 to utrata urządzenia, reszta to błąd komendy.
        "DEVICE_LOST_CODES",
        "RETURN_CODES",
        "def find_devices",
        # -c zwraca stan DUT, nie testera — rozróżnienie wynika z diagnostyki.
        "def dut_present",
        "def read_configuration",
    ),
    "storage.py": (
        # Zmiana zestawu profili nie może rozjechać kolumn raportu — plik jest
        # rolowany, gdy sygnatura nagłówka się zmieni.
        "TestResult.profiles_signature",
        # Wynik, którego nie udało się zapisać, przeżywa restart aplikacji.
        "class PendingResultStore",
        # Ta sama sztuka nie może być testowana na dwóch gniazdach naraz.
        "def reserve",
        "def release",
    ),
    "loop_test.py": (
        # Utrata testera nie może skasować reszty serii.
        "def _ensure_connected",
        "RECONNECT_ATTEMPTS",
        "aborted_reason",
        # Rezystancja toru — rozróżnia złe gniazdo od złego wyrobu.
        "class SourceResistance",
        "def source_resistances",
    ),
    "station_panel.py": (
        # Błąd stanowiska nie zużywa próby numeru seryjnego.
        "ATTEMPT_CONSUMING_STATUSES",
        # Automatyczne odzyskiwanie utraconego testera.
        "automatyczna próba odzyskania",
        "def report_connection",
    ),
    "ui.py": (
        # Wykrywanie testerów dostępne dla operatora, bez hasła.
        "def detect_testers",
        "def _apply_remap",
        # Panel inżynieryjny tylko po haśle.
        "verify_engineering_password",
        # Seria walidacyjna blokuje tylko swoje gniazdo.
        "def loop_active",
    ),
    "detection.py": (
        "def plan_station_remap",
        "class StationRemapPlan",
    ),
    "settings_ui.py": (
        # Zapis ustawień idzie przez AppConfig.save, które waliduje konfigurację
        # i dopisuje wpis audytowy — panel nie ma własnej, omijającej drogi zapisu.
        "audit_note=note",
        "def _use_detected_devices",
        "def _reset_attempt",
    ),
}

#: Hasło standardowe nie może trafić do binarki jawnym tekstem — build jest
#: niepodpisany, więc literal dałoby się wyciągnąć poleceniem `strings`.
#: Samo hasło pozostaje niezmienione; w źródle jest jego skrót SHA-256.
_PLAINTEXT_PASSWORD_MARKER = "recon" + "ext2025"
FORBIDDEN_MARKERS = {name: (_PLAINTEXT_PASSWORD_MARKER,) for name in PROJECT_FILES}


class BuildError(RuntimeError):
    pass


def print_header(title: str) -> None:
    print("\n" + "=" * 68)
    print(f"  {title}")
    print("=" * 68)


def run_command(command: list[str], cwd: Optional[Path] = None) -> None:
    print("[*] " + subprocess.list2cmdline(command))
    result = subprocess.run(command, cwd=str(cwd) if cwd else None, text=True, check=False)
    if result.returncode != 0:
        raise BuildError(
            f"Polecenie zakończyło się błędem {result.returncode}: {command[0]}"
        )


# --------------------------------------------------------------- środowisko


def ensure_package(import_name: str, pip_name: str, lowest: tuple, below: tuple) -> None:
    if importlib.util.find_spec(import_name) is None:
        raise BuildError(
            f"Brak pakietu {pip_name}. Zainstaluj: "
            f"{sys.executable} -m pip install -r requirements-dev.txt"
        )
    try:
        actual = importlib.metadata.version(pip_name)
    except importlib.metadata.PackageNotFoundError as exc:
        raise BuildError(f"Nie można odczytać wersji pakietu {pip_name}") from exc
    parts = tuple(int(item) for item in actual.split(".")[:3] if item.isdigit())
    if not lowest <= parts < below:
        raise BuildError(
            f"Wymagany {pip_name} >= {'.'.join(map(str, lowest))} i < "
            f"{'.'.join(map(str, below))}, znaleziono {actual}."
        )
    print(f"[+] {pip_name} {actual}: OK")


def verify_build_environment() -> None:
    print_header("Środowisko budowania")
    if os.name != "nt":
        raise BuildError(
            "EXE dla Windows buduje się na Windows. Na innym systemie PyInstaller "
            "zbuduje binarkę dla TEGO systemu, nie dla stanowiska."
        )
    if sys.version_info[:2] < REQUIRED_PYTHON_MINIMUM:
        raise BuildError(
            f"Wymagany Python {REQUIRED_PYTHON_MINIMUM[0]}.{REQUIRED_PYTHON_MINIMUM[1]} "
            f"lub nowszy, uruchomiono {sys.version_info.major}.{sys.version_info.minor}."
        )
    print(f"[+] Python {sys.version_info.major}.{sys.version_info.minor}."
          f"{sys.version_info.micro}: OK")
    for import_name, (pip_name, lowest, below) in REQUIRED_PACKAGES.items():
        ensure_package(import_name, pip_name, lowest, below)
    try:
        import tkinter  # noqa: F401
    except ImportError as exc:
        raise BuildError(
            "Brak modułu tkinter. Zainstaluj Pythona z komponentem „tcl/tk and IDLE”."
        ) from exc
    print("[+] tkinter: OK")


# ------------------------------------------------------------------ źródła


def resolve_project_file(name: str, required: bool = True) -> Optional[Path]:
    """Używa WYŁĄCZNIE pliku o dokładnej, kanonicznej nazwie.

    Celowo nie wybiera plików typu ``ui(3).py`` — builder nie może niejawnie
    zbudować EXE ze starej albo przypadkowej kopii kodu.
    """
    exact = ROOT_DIR / name
    if exact.is_file():
        return exact
    if required:
        raise BuildError(
            f"Brak wymaganego pliku: {name}. Nadaj aktualnemu plikowi dokładnie tę nazwę."
        )
    return None


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def _string_constants(text: str) -> list[str]:
    """Stałe napisowe pliku, po sklejeniu prostych konkatenacji.

    Szukanie hasła jako podciągu tekstu źródłowego wykrywa tylko naiwne
    wklejenie. Parser Pythona sam skleja przylegające literały, a tu dodatkowo
    składamy łańcuchy ``"a" + "b"``. Obfuskacji przez chr() albo base64 ta
    kontrola nie wykryje i nie udaje, że wykrywa.
    """
    import ast

    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []

    values: list[str] = []

    def fold(node) -> Optional[str]:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            left, right = fold(node.left), fold(node.right)
            if left is not None and right is not None:
                return left + right
        return None

    for node in ast.walk(tree):
        folded = fold(node)
        if folded:
            values.append(folded)
    return values


def validate_source_file(name: str, source: Path) -> None:
    text = source.read_text(encoding="utf-8", errors="strict")

    markers = FORBIDDEN_MARKERS.get(name, ())
    forbidden = [item for item in markers if item in text]
    if markers:
        constants = _string_constants(text)
        forbidden += [
            item for item in markers
            if item not in forbidden and any(item in value for value in constants)
        ]
    if forbidden:
        raise BuildError(
            f"{name}: jawne hasło inżynieryjne w źródle. Build jest niepodpisany, "
            "więc literal da się wyciągnąć z EXE poleceniem `strings`. W kodzie ma być "
            "skrót SHA-256 (DEFAULT_ENGINEERING_PASSWORD_HASH), nie samo hasło."
        )

    missing = [item for item in REQUIRED_MARKERS.get(name, ()) if item not in text]
    if missing:
        raise BuildError(
            f"{name}: brakuje wymaganych elementów: " + ", ".join(missing)
        )


def verify_approved_sources() -> None:
    """Porównuje sumy SHA-256 źródeł z listą zatwierdzoną przez technologa."""
    if not APPROVED_SOURCES_FILE.is_file():
        print(f"[~] Brak {APPROVED_SOURCES_FILE.name} — pomijam kontrolę sum kontrolnych. "
              "Wygeneruj: py build_exe.py --approve")
        return

    approved = json.loads(APPROVED_SOURCES_FILE.read_text(encoding="utf-8"))
    entries = approved.get("files", {})
    problems: list[str] = []
    for name in PROJECT_FILES:
        actual = sha256_file(ROOT_DIR / name)
        expected = entries.get(name)
        if expected is None:
            problems.append(f"{name}: brak na liście zatwierdzonej")
        elif expected.upper() != actual:
            problems.append(
                f"{name}: suma {actual[:16]} != zatwierdzona {expected.upper()[:16]}"
            )
    extra = sorted(set(entries) - set(PROJECT_FILES))
    if extra:
        problems.append(f"lista zawiera nieistniejące pliki: {extra}")

    if problems:
        raise BuildError(
            "Źródła nie zgadzają się z zatwierdzoną wersją:\n  - "
            + "\n  - ".join(problems)
            + "\n\nJeśli zmiany są celowe, zatwierdź je ponownie: py build_exe.py --approve"
        )
    print(f"[+] Sumy SHA-256 zgodne z {APPROVED_SOURCES_FILE.name} "
          f"(zatwierdzone {approved.get('approved_at', '?')})")


def write_approved_sources() -> int:
    payload = {
        "approved_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "app_version": SOURCE_APP_VERSION,
        "files": {name: sha256_file(ROOT_DIR / name) for name in PROJECT_FILES},
    }
    APPROVED_SOURCES_FILE.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(f"[+] Zapisano {APPROVED_SOURCES_FILE.name} dla wersji {SOURCE_APP_VERSION}")
    for name, digest in payload["files"].items():
        print(f"    {digest[:16]}  {name}")
    return 0


# --------------------------------------------------------------- preflight


#: Kontrola ZACHOWANIA aplikacji, nie obecności napisów. Uruchamiana w osobnym
#: procesie, żeby liczyła się dokładnie ta konfiguracja, która pójdzie na stanowisko.
PREFLIGHT_SCRIPT = r"""
from config import AppConfig, APP_VERSION
from models import TestProfile

config = AppConfig()
errors = config.validate()
assert not errors, f'Domyslna konfiguracja nie przechodzi walidacji: {errors}'

assert config.verify_engineering_password('recon' + 'ext2025'), \
    'haslo standardowe stanowiska nie dziala'
assert not config.verify_engineering_password('zle-haslo'), \
    'weryfikacja hasla przepuszcza dowolna wartosc'
assert not config.verify_engineering_password(''), 'puste haslo przechodzi weryfikacje'

# Progi wyrobu SR213 PSU (SPBV36UK-W / SPBV36EU-W). Tolerancja +/-5 % nominalu.
expected = {'5V': (4.75, 5.50), '9V': (8.55, 9.45),
            '12V': (11.40, 12.60), '15V': (14.25, 15.75)}
names = {profile.name for profile in config.profiles}
assert names == set(expected), f'Zestaw profili rozni sie od specyfikacji: {sorted(names)}'
for profile in config.profiles:
    low, high = expected[profile.name]
    assert abs(profile.min_voltage - low) < 1e-9, f'{profile.name}: dolny prog {profile.min_voltage}'
    assert abs(profile.max_voltage - high) < 1e-9, f'{profile.name}: gorny prog {profile.max_voltage}'
    # Wymaganie procesowe: 2,5 s bez obciazenia + 2,5 s pod obciazeniem na profil.
    assert profile.no_load_seconds >= 2.5, f'{profile.name}: faza bez obciazenia skrocona'
    assert profile.load_seconds >= 2.5, f'{profile.name}: faza pod obciazeniem skrocona'

assert config.measurement_seconds() >= 20.0, 'Suma faz pomiarowych ponizej wymaganych 20 s'
assert config.estimated_test_seconds() < config.test_timeout, \
    'Przewidywany czas testu przekracza timeout'
rated = config.estimated_samples_per_phase() - config.settling_ignore_samples
assert rated >= config.minimum_samples, \
    f'W fazie zmiesci sie {rated} ocenianych probek, wymagane {config.minimum_samples}'

assert len(config.active_stations()) >= 1, 'Brak aktywnego stanowiska'
assert config.valid_hrids, 'Pusta lista HRID'
print(f'[PREFLIGHT] wersja {APP_VERSION}, profile {sorted(names)}, '
      f'fazy {config.measurement_seconds():.0f} s, '
      f'test ~{config.estimated_test_seconds():.0f} s: OK')
"""


def run_release_preflight() -> None:
    print_header("Kontrola przed wydaniem")

    sources = [ROOT_DIR / name for name in PROJECT_FILES]
    sources += [ROOT_DIR / "selftest.py", ROOT_DIR / "build_exe.py"]
    missing = [str(path) for path in sources if not path.is_file()]
    if missing:
        raise BuildError("Brak plików kontroli wydania: " + ", ".join(missing))

    run_command([sys.executable, "-m", "py_compile", *map(str, sources)], cwd=ROOT_DIR)
    print("[+] Kompilacja wszystkich skryptów: OK")

    run_command([sys.executable, "-m", "unittest", "selftest", "-v"], cwd=ROOT_DIR)
    print("[+] Testy automatyczne: OK")

    run_command([sys.executable, "-c", PREFLIGHT_SCRIPT], cwd=ROOT_DIR)

    verify_approved_sources()
    print(f"[+] Wersja źródła: {SOURCE_APP_VERSION}; wersja EXE: {VERSION}")


# ------------------------------------------------------------------ build


def create_version_file(path: Path) -> None:
    parts = tuple(int(item) for item in VERSION.split("."))
    if len(parts) != 4:
        raise BuildError("VERSION musi mieć format np. 5.5.0.0")
    path.write_text(f"""VSVersionInfo(
  ffi=FixedFileInfo(
    filevers={parts},
    prodvers={parts},
    mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable('041504b0', [
        StringStruct('CompanyName', '{COMPANY_NAME}'),
        StringStruct('FileDescription', '{APP_DESCRIPTION}'),
        StringStruct('FileVersion', '{VERSION}'),
        StringStruct('InternalName', '{APP_NAME}'),
        StringStruct('LegalCopyright', '{COPYRIGHT}'),
        StringStruct('OriginalFilename', '{APP_NAME}.exe'),
        StringStruct('ProductName', '{COMPANY_NAME} {APP_NAME}'),
        StringStruct('ProductVersion', '{VERSION}'),
      ])
    ]),
    VarFileInfo([VarStruct('Translation', [1045, 1200])])
  ]
)
""", encoding="utf-8")


def create_runtime_hook(path: Path) -> None:
    """Katalogiem roboczym musi być folder EXE, nie folder skrótu.

    Bez tego foldery danych (reports, logs, raw) powstają tam, skąd aplikację
    uruchomiono — a przy skrócie z pulpitu bywa to profil użytkownika.
    """
    path.write_text('''import os
import sys

if getattr(sys, "frozen", False):
    app_dir = os.path.dirname(os.path.abspath(sys.executable))
    internal_dir = getattr(sys, "_MEIPASS", os.path.join(app_dir, "_internal"))

    tcl_dir = os.path.join(internal_dir, "_tcl_data")
    tk_dir = os.path.join(internal_dir, "_tk_data")

    if os.path.isdir(tcl_dir):
        os.environ["TCL_LIBRARY"] = tcl_dir
    if os.path.isdir(tk_dir):
        os.environ["TK_LIBRARY"] = tk_dir

    os.chdir(app_dir)
''', encoding="utf-8")


def locate_tcl_tk() -> tuple[Path, Path]:
    try:
        import tkinter as tk

        root = tk.Tk()
        root.withdraw()
        try:
            tcl_dir = Path(root.tk.eval("info library")).resolve()
            tk_dir = Path(root.tk.eval("set tk_library")).resolve()
        finally:
            root.destroy()
    except Exception as exc:
        raise BuildError(f"Nie mogę ustalić katalogów Tcl/Tk: {exc}") from exc

    required = [tcl_dir / "init.tcl", tk_dir / "tk.tcl", tk_dir / "ttk" / "scrollbar.tcl"]
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise BuildError("Instalacja Tcl/Tk jest niekompletna. Brakuje: " + ", ".join(missing))
    print(f"[+] Tcl: {tcl_dir}")
    print(f"[+] Tk : {tk_dir}")
    return tcl_dir, tk_dir


def copy_tcl_tk_runtime(tcl_dir: Path, tk_dir: Path) -> None:
    print_header("Weryfikacja bibliotek Tcl/Tk")
    internal_dir = OUTPUT_DIR / "_internal"
    tcl_target = internal_dir / "_tcl_data"
    tk_target = internal_dir / "_tk_data"

    internal_dir.mkdir(parents=True, exist_ok=True)
    shutil.copytree(tcl_dir, tcl_target, dirs_exist_ok=True)
    shutil.copytree(tk_dir, tk_target, dirs_exist_ok=True)

    required = [tcl_target / "init.tcl", tk_target / "tk.tcl",
                tk_target / "ttk" / "scrollbar.tcl"]
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise BuildError("Po buildzie nadal brakuje plików Tcl/Tk: " + ", ".join(missing))
    print(f"[+] Tcl skopiowany do: {tcl_target}")
    print(f"[+] Tk/Ttk skopiowany do: {tk_target}")


def prepare_staging() -> dict[str, Path]:
    print_header("Przygotowanie plików")
    if STAGING_DIR.exists():
        shutil.rmtree(STAGING_DIR)
    STAGING_DIR.mkdir(parents=True, exist_ok=True)

    resolved: dict[str, Path] = {}
    for name in PROJECT_FILES:
        source = resolve_project_file(name)
        assert source is not None
        validate_source_file(name, source)
        shutil.copy2(source, STAGING_DIR / name)
        resolved[name] = source
        print(f"[+] {name}  SHA256:{sha256_file(source)[:16]}")

    for name in EDITABLE_DATA_FILES:
        source = resolve_project_file(name)
        assert source is not None
        resolved[name] = source
        print(f"[+] Dane edytowalne: {name}")

    for name in OPTIONAL_DATA_FILES:
        source = resolve_project_file(name, required=False)
        if source is None:
            print(f"[~] {name}: brak — pomijam")
            continue
        resolved[name] = source
        print(f"[+] Dokumentacja: {name}")

    create_version_file(STAGING_DIR / "version_info.txt")
    create_runtime_hook(STAGING_DIR / "runtime_hook_psu19.py")
    return resolved


def find_icon() -> Optional[Path]:
    for name in OPTIONAL_ICONS:
        candidate = ROOT_DIR / name
        if candidate.is_file():
            return candidate
    return None


def build_application(tcl_dir: Path, tk_dir: Path) -> None:
    print_header(f"Budowanie {APP_NAME}")
    if OUTPUT_DIR.exists():
        shutil.rmtree(OUTPUT_DIR)

    work_dir = BUILD_DIR / "pyinstaller"
    spec_dir = BUILD_DIR / "spec"
    work_dir.mkdir(parents=True, exist_ok=True)
    spec_dir.mkdir(parents=True, exist_ok=True)

    command = [
        sys.executable, "-m", "PyInstaller",
        "--onedir", "--windowed", "--clean", "--noconfirm", "--noupx",
        "--name", APP_NAME,
        "--distpath", str(DIST_DIR),
        "--workpath", str(work_dir),
        "--specpath", str(spec_dir),
        "--version-file", str(STAGING_DIR / "version_info.txt"),
        "--runtime-hook", str(STAGING_DIR / "runtime_hook_psu19.py"),
        "--add-data", f"{tcl_dir};_tcl_data",
        "--add-data", f"{tk_dir};_tk_data",
    ]

    icon = find_icon()
    if icon:
        command.extend(["--icon", str(icon)])
        print(f"[+] Ikona: {icon.name}")
    else:
        print("[~] Brak ikony — używam domyślnej")

    for module in HIDDEN_IMPORTS:
        command.extend(["--hidden-import", module])

    command.append("app.py")
    run_command(command, cwd=STAGING_DIR)

    if not EXE_PATH.is_file():
        raise BuildError(f"Nie znaleziono utworzonego EXE: {EXE_PATH}")


def copy_editable_files(resolved: dict[str, Path]) -> None:
    print_header("Kopiowanie konfiguracji i dokumentacji")
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    for name in EDITABLE_DATA_FILES + OPTIONAL_DATA_FILES:
        source = resolved.get(name)
        if source:
            shutil.copy2(source, OUTPUT_DIR / name)
            print(f"[+] {name} obok EXE")


def iter_output_files() -> Iterable[Path]:
    for path in sorted(OUTPUT_DIR.rglob("*")):
        if path.is_file() and path.name != "build_manifest.txt":
            yield path


def write_manifest() -> None:
    manifest = OUTPUT_DIR / "build_manifest.txt"
    lines = [
        f"Application: {APP_NAME}",
        f"Version: {VERSION}",
        f"Company: {COMPANY_NAME}",
        f"Build time: {datetime.now().astimezone().isoformat(timespec='seconds')}",
        "Authenticode signed: NO",
        f"Approved sources file: {'YES' if APPROVED_SOURCES_FILE.is_file() else 'NO'}",
        "",
        "SHA-256:",
    ]
    for path in iter_output_files():
        lines.append(f"{sha256_file(path)}  {path.relative_to(OUTPUT_DIR)}")
    manifest.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"[+] build_manifest.txt: {sum(1 for _ in iter_output_files())} plików")


def show_summary() -> None:
    exe_size = EXE_PATH.stat().st_size / (1024 * 1024)
    folder_size = sum(
        path.stat().st_size for path in OUTPUT_DIR.rglob("*") if path.is_file()
    ) / (1024 * 1024)

    print_header("BUILD ZAKOŃCZONY")
    print(f"Folder aplikacji : {OUTPUT_DIR}")
    print(f"Uruchamiaj       : {EXE_PATH}")
    print(f"Rozmiar EXE      : {exe_size:.1f} MB")
    print(f"Rozmiar folderu  : {folder_size:.1f} MB")
    print("Podpis cyfrowy   : NIE")
    print(f"\n[!] Kopiuj CAŁY folder „{APP_NAME}”, nie samo EXE.")
    print("[!] test_config.json musi pozostać obok EXE — bez niego aplikacja")
    print("    odtworzy ustawienia domyślne, czyli jedno gniazdo i adres „Any”.")
    print("[!] Foldery reports/, backup/, logs/, raw/, validation/ aplikacja")
    print("    zakłada sama przy pierwszym uruchomieniu, obok EXE.")
    print("[!] Konto operatora musi mieć prawo ZAPISU w tym folderze.")
    print("[!] Hasło panelu inżynieryjnego jest w dokumentacji wdrożeniowej;")
    print("    w plikach aplikacji jest tylko jego skrót SHA-256.")


def main() -> int:
    try:
        if "--approve" in sys.argv:
            return write_approved_sources()

        print_header(f"Build {APP_NAME} {VERSION}")
        verify_build_environment()
        run_release_preflight()
        resolved = prepare_staging()
        tcl_dir, tk_dir = locate_tcl_tk()
        build_application(tcl_dir, tk_dir)
        copy_tcl_tk_runtime(tcl_dir, tk_dir)
        copy_editable_files(resolved)
        write_manifest()
        show_summary()
        return 0

    except KeyboardInterrupt:
        print("\n[!] Anulowano przez użytkownika.")
        return 130
    except BuildError as error:
        print_header("BŁĄD BUDOWANIA")
        print(f"[!] {error}")
        return 1
    except Exception as error:
        print_header("NIEOCZEKIWANY BŁĄD")
        print(f"[!] {type(error).__name__}: {error}")
        return 1
    finally:
        if sys.stdin.isatty():
            try:
                input("\nNaciśnij Enter, aby zamknąć...")
            except (EOFError, KeyboardInterrupt):
                pass


if __name__ == "__main__":
    raise SystemExit(main())
