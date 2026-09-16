"""Budowanie PSU19_Tester.exe (PyInstaller, Windows).

Uruchomienie:
    py -m pip install -r requirements-dev.txt
    py build_exe.py

Efekt w folderze dist:
    PSU19_Tester.exe
    test_config.json
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

APP_NAME = "PSU19_Tester"
VERSION = "4.0.0"
COMPANY = "Reconext"
DESCRIPTION = "PSU USB-C PD Tester (PM125)"

ROOT = Path(__file__).resolve().parent
DIST = ROOT / "dist"
WORK = ROOT / "build"
SPEC = ROOT / "build" / "spec"

#: Moduly aplikacji - plaska struktura, więc wszystkie leza obok app.py.
MODULES = [
    "config.py", "engine.py", "hardware.py", "logging_utils.py",
    "models.py", "settings_ui.py", "storage.py", "theme.py", "ui.py",
]
OPTIONAL_ICONS = ("reconext_logo.ico", "icon.ico")


def fail(message: str) -> None:
    print(f"[!] {message}")
    raise SystemExit(1)


def check_sources() -> None:
    missing = [name for name in [*MODULES, "app.py", "test_config.json"] if not (ROOT / name).is_file()]
    if missing:
        fail("Brak plików: " + ", ".join(missing))
    print(f"[+] Komplet plików źródłowych ({len(MODULES) + 1} modułów)")


def run_selftest() -> None:
    print("[*] Testy automatyczne...")
    completed = subprocess.run(
        [sys.executable, "-m", "unittest", "selftest"], cwd=ROOT, capture_output=True, text=True
    )
    if completed.returncode != 0:
        print(completed.stdout)
        print(completed.stderr)
        fail("Testy automatyczne nie przeszły - build przerwany.")
    print("[+] Testy automatyczne: OK")


def ensure_pyinstaller() -> None:
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print("[~] Brak PyInstaller - instaluje...")
        subprocess.run([sys.executable, "-m", "pip", "install", "pyinstaller>=6.0,<7"], check=True)


def write_version_file() -> Path:
    parts = tuple(int(item) for item in VERSION.split(".")) + (0,)
    path = WORK / "version_info.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"""VSVersionInfo(
  ffi=FixedFileInfo(
    filevers={parts[:4]},
    prodvers={parts[:4]},
    mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable('041504b0', [
        StringStruct('CompanyName', '{COMPANY}'),
        StringStruct('FileDescription', '{DESCRIPTION}'),
        StringStruct('FileVersion', '{VERSION}'),
        StringStruct('InternalName', '{APP_NAME}'),
        StringStruct('LegalCopyright', '{COMPANY}'),
        StringStruct('OriginalFilename', '{APP_NAME}.exe'),
        StringStruct('ProductName', '{COMPANY} {DESCRIPTION}'),
        StringStruct('ProductVersion', '{VERSION}')])
    ]),
    VarFileInfo([VarStruct('Translation', [1045, 1200])])
  ]
)
""",
        encoding="utf-8",
    )
    return path


def find_icon() -> Path | None:
    for name in OPTIONAL_ICONS:
        candidate = ROOT / name
        if candidate.is_file():
            return candidate
    return None


def clean() -> None:
    for path in (DIST, WORK):
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)
            print(f"[+] Usunięto: {path.name}")


def build() -> None:
    command = [
        sys.executable, "-m", "PyInstaller",
        "--onefile", "--windowed", "--clean", "--noconfirm", "--noupx",
        "--name", APP_NAME,
        "--distpath", str(DIST),
        "--workpath", str(WORK),
        "--specpath", str(SPEC),
        "--paths", str(ROOT),
        "--version-file", str(write_version_file()),
    ]
    icon = find_icon()
    if icon:
        command.extend(["--icon", str(icon)])
        print(f"[+] Ikona: {icon.name}")
    else:
        print("[~] Brak ikony - używam domyślnej")
    for module in MODULES:
        command.extend(["--hidden-import", Path(module).stem])
    command.append(str(ROOT / "app.py"))

    print("[*] " + subprocess.list2cmdline(command))
    subprocess.run(command, cwd=ROOT, check=True)

    exe = DIST / f"{APP_NAME}.exe"
    if not exe.is_file():
        fail(f"Nie znaleziono utworzonego EXE: {exe}")
    shutil.copy2(ROOT / "test_config.json", DIST / "test_config.json")
    print(f"[+] Gotowe: {exe}")
    print(f"[+] Konfiguracja: {DIST / 'test_config.json'}")
    print("\nSkopiuj oba pliki na stanowisko. Foldery reports/, backup/, logs/, raw/")
    print("aplikacja utworzy sama przy pierwszym uruchomieniu.")


def main() -> int:
    if sys.platform != "win32":
        print("[~] Uwaga: EXE dla Windows buduje się na Windows.")
    print("=" * 68)
    print(f"  Build {APP_NAME} {VERSION}")
    print("=" * 68)
    check_sources()
    run_selftest()
    ensure_pyinstaller()
    clean()
    build()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
