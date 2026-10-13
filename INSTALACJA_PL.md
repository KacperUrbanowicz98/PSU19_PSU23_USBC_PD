# Przygotowanie nowego komputera pod PSU19 Tester

Instrukcja dla inżyniera stanowiskowego. Zakłada Windows 10/11 64-bit i konto z prawami
administratora na czas instalacji (sama aplikacja praw administratora nie potrzebuje).

Są dwa warianty wdrożenia. **Wariant EXE** jest domyślny dla produkcji — na stanowisku nie
ma wtedy Pythona. **Wariant z Pythonem** jest dla komputera inżynierskiego, na którym chcesz
zmieniać kod i uruchamiać testy automatyczne.

---

## 1. Sprzęt i system

| Pozycja | Wymaganie |
|---|---|
| System | Windows 10 lub 11, 64-bit |
| Ekran | minimum 1366×768 dla jednego gniazda, **1920×1080 dla dwóch** |
| Porty USB | jeden wolny port USB-A/USB-C **na każdy tester PM125** |
| Dysk | ~500 MB na aplikację i dane (raporty rosną ~1 MB na 5 000 testów) |
| Uprawnienia | konto operatora musi mieć **prawo zapisu w folderze aplikacji** |

Nie podłączaj testerów przez hub bez własnego zasilania — PM125 pobiera prąd z portu i przy
dwóch sztukach na pasywnym hubie potrafi się rozłączać w trakcie testu.

---

## 2. Sterownik i konsola PassMark (oba warianty)

1. Zainstaluj pakiet **PassMark USB Power Delivery Tester** (instalator od PassMark,
   ten sam, z którego pochodzi `USBPDConsole.exe`).
2. Domyślna lokalizacja konsoli to:

   ```text
   C:\USBPDAPI\USBPDConsole.exe
   ```

   Jeżeli instalator położy ją gdzie indziej, zapamiętaj ścieżkę — trafi do konfiguracji.
3. Podłącz tester (albo oba testery) i **sprawdź, że konsola je widzi**:

   ```powershell
   C:\USBPDAPI\USBPDConsole.exe -f
   ```

   Poprawna odpowiedź to wiersze w postaci:

   ```text
   0=PMPD80LLAD
   1=PMPD80LN6E
   ```

   Zapisz te numery seryjne — to one trafiają do listy stanowisk. Jeśli komenda nic nie
   zwraca, aplikacji nie ma sensu uruchamiać: najpierw sterownik i kable.
4. Wersja konsoli zweryfikowana z aplikacją: **V1.0.1016**. Przy nowszej wersji przejdź
   punkt 8 (walidacja) uważniej — format odpowiedzi może się zmienić.

---

## 3. Wariant EXE (stanowisko produkcyjne)

### 3.1 Zbuduj wydanie na komputerze inżynierskim

Na maszynie z Pythonem, w folderze projektu:

```powershell
approve_sources.bat      REM po sprawdzeniu zmian — zatwierdza sumy SHA-256 źródeł
build_exe.bat
```

Builder sam, zanim cokolwiek zbuduje:

1. sprawdza środowisko (Windows, Python 3.10+, PyInstaller 6.x, tkinter),
2. kompiluje wszystkie źródła i uruchamia **pełne testy automatyczne**,
3. sprawdza zachowanie aplikacji: czy domyślna konfiguracja przechodzi walidację, czy
   hasło inżynieryjne działa, a błędne i puste odpada, czy progi profili zgadzają się ze
   specyfikacją wyrobu i czy fazy 2,5 s + 2,5 s mieszczą się w timeoucie,
4. porównuje sumy SHA-256 źródeł z `approved_sources.json`,
5. sprawdza, czy w kodzie są obecne bramki, których wycięcie przeszłoby niezauważone
   (pierwszeństwo błędu stanowiska nad FAIL, odzyskiwanie testera, licznik prób,
   hasło panelu).

Każda z tych kontroli **zatrzymuje build**. To kontrola pomyłek, nie zabezpieczenie przed
świadomą zmianą kodu — podmiana treści funkcji przy zachowanej nazwie przejdzie.

Wynik: folder `dist\PSU19 Tester\` oraz `build_manifest.txt` z sumami SHA-256 wszystkich
plików wydania.

### 3.2 Rozłóż pliki na stanowisku

Skopiuj **CAŁY FOLDER** `PSU19 Tester` — nie samo EXE. Aplikacja jest budowana w trybie
onedir, więc biblioteki leżą obok pliku wykonywalnego w `_internal\`.

Docelowa lokalizacja — **nie `C:\Program Files`** (tam operator nie zapisze raportów):

```text
C:\PSU19Tester\
    PSU19 Tester.exe         ← plik uruchamiany
    _internal\               ← biblioteki i runtime Tcl/Tk (NIE ruszaj)
    test_config.json         ← konfiguracja stanowiska, edytowalna z panelu
    README_PL.md             ← dokumentacja obsługi
    INSTALACJA_PL.md         ← ten dokument
    CHANGELOG_PL.md          ← historia zmian
    build_manifest.txt       ← sumy SHA-256 wydania
    reports\                 ← raporty CSV            ┐
    backup\                  ← kopie awaryjne         │
    logs\                    ← logi aplikacji         │ aplikacja zakłada
    raw\                     ← surowe próbki          │ je sama przy
    validation\              ← raporty serii          │ pierwszym starcie
    serial_attempts.json     ← licznik prób           │
    pending_results.json     ← niezapisane wyniki     │
    config_audit.log         ← dziennik zmian         ┘
```

Co musi zostać razem: `_internal\` (bez niego EXE nie wystartuje) oraz `test_config.json`
(bez niego aplikacja odtworzy ustawienia domyślne — jedno gniazdo i adres „Any”).

### 3.3 Uprawnienia

Konto operatora musi mieć prawo zapisu do `C:\PSU19Tester`. Sprawdzenie:

```powershell
icacls C:\PSU19Tester
```

Nadanie (z konta administratora — zobacz najpierw powyższy odczyt, `icacls` nie ma trybu
podglądu zmian i działa od razu; zasięg to cały podkatalog wraz z zawartością):

```powershell
icacls C:\PSU19Tester /grant "Users:(OI)(CI)M" /T
```

`M` to Modify — zapis i kasowanie w obrębie folderu. Nie nadawaj `F` (Full), bo pozwala też
zmieniać uprawnienia.

### 3.4 Skrót na pulpicie

Skrót do `C:\PSU19Tester\PSU19 Tester.exe`, pole **Rozpocznij w:** ustaw na
`C:\PSU19Tester`.

Aplikacja i tak przestawia katalog roboczy na folder EXE (robi to hook startowy), więc
foldery danych powstaną we właściwym miejscu nawet przy źle ustawionym skrócie — ale pole
**Rozpocznij w:** warto ustawić poprawnie, bo tak zachowują się inne stanowiska.

Autostart (opcjonalnie): skrót w
`C:\ProgramData\Microsoft\Windows\Start Menu\Programs\StartUp`.

### 3.5 Aktualizacja wersji

Podmień `PSU19 Tester.exe` **i folder `_internal\`** — to jedna całość, pochodzą z tego
samego buildu. `test_config.json`, foldery danych i liczniki zostają nietknięte.

Numer wersji sprawdzisz w nagłówku raportu walidacyjnego, w `build_manifest.txt` oraz we
właściwościach pliku EXE (zakładka Szczegóły).

---

## 4. Wariant z Pythonem (komputer inżynierski)

1. Zainstaluj **Python 3.10 lub nowszy** z python.org, zaznaczając *Add Python to PATH*
   oraz komponent **tcl/tk and IDLE** (bez niego nie ruszy interfejs Tkinter).
2. Skopiuj cały folder projektu, np. do `C:\PSU19Tester\`.
3. Zainstaluj zależności:

   ```powershell
   cd C:\PSU19Tester
   pip install -r requirements.txt
   ```

4. Uruchom testy automatyczne — to najszybsza kontrola, czy środowisko jest kompletne:

   ```powershell
   run_tests.bat
   ```

   Oczekiwany wynik: `Ran 83 tests ... OK`.

5. Uruchomienie bez testera (sprawdzenie interfejsu):

   ```powershell
   run_simulator.bat
   ```

6. Uruchomienie produkcyjne: `run_app.bat`.

---

## 5. Konfiguracja

Przy pierwszym starcie powstaje `test_config.json` z ustawieniami domyślnymi. Do ręcznego
sprawdzenia są trzy rzeczy:

```json
{
  "console_path": "C:\\USBPDAPI\\USBPDConsole.exe",
  "stations": [
    { "name": "LEWY",  "device_serial": "PMPD80LLAD", "enabled": true },
    { "name": "PRAWY", "device_serial": "PMPD80LN6E", "enabled": true }
  ],
  "valid_hrids": ["44963", "81705", "..."]
}
```

* **`console_path`** — musi wskazywać faktyczną lokalizację `USBPDConsole.exe`.
* **`stations`** — jeden wpis to jedno gniazdo, dwa wpisy to układ LEWY/PRAWY. Przy dwóch
  gniazdach numer seryjny jest obowiązkowy.
* **`valid_hrids`** — lista numerów operatorów dopuszczonych na stanowisku.

Listy stanowisk nie musisz wypełniać ręcznie. Po podłączeniu testerów wystarczy przycisk
**WYKRYJ TESTERY** w pasku operatora (bez hasła) albo **UŻYJ WYKRYTYCH** w zakładce
*Stanowiska* panelu inżynieryjnego.

Profile, kryteria, czasy faz i hasło inżynieryjne zostawiasz bez zmian — są ustawione pod
specyfikację SR213 PSU.

**Panel inżynieryjny:** trzy razy `Ctrl+Shift+D` w ciągu 2 sekund, potem hasło.

---

## 6. Kopiowanie konfiguracji na kolejne stanowiska

Przenieś na nowy komputer **tylko** `test_config.json`, a potem na miejscu:

1. popraw `console_path`, jeśli konsola leży gdzie indziej,
2. naciśnij **WYKRYJ TESTERY** — numery seryjne są inne na każdym stanowisku, więc
   skopiowana lista wymaga przepięcia.

Nie kopiuj `serial_attempts.json`, `pending_results.json`, `reports/` ani `raw/` — to dane
tamtego stanowiska i przeniesione zafałszują liczniki prób oraz raport.

---

## 7. Diagnostyka przed produkcją (zalecane)

W wariancie z Pythonem uruchom:

```powershell
python check_dual.py
```

Skrypt wykonuje **wyłącznie odczyty** (`-h`, `-f`, `-c`, `-s`, `-p`, `-r`) i zapisuje
`check_dual_report.txt`: listę testerów, ich konfigurację, czasy komend i pomiar pracy
równoległej. Raport dołącz do dokumentacji uruchomienia stanowiska.

Najważniejsze, na co patrzysz w raporcie:

* oba testery widoczne w sekcji `--find`,
* identyczna konfiguracja (`-r`) na obu — różnica w `CABLE RESISTANCE` albo
  `HOLD LOAD ON VOLTAGE CHANGES` oznacza, że ta sama sztuka dostanie inny wynik zależnie
  od gniazda,
* czas pojedynczej komendy — przy ~0,6 s w fazie mieszczą się 4 próbki, z czego
  3 oceniane. Gdy komenda trwa dłużej niż ~0,8 s, aplikacja odrzuci konfigurację przy
  zapisie i trzeba wydłużyć fazy albo zmniejszyć `minimum_samples`.

---

## 8. Walidacja stanowiska przed oddaniem do produkcji

1. Uruchom aplikację, zaloguj się numerem HRID.
2. Sprawdź, że każde gniazdo pokazuje **TESTER OK**. Jeśli nie — **WYKRYJ TESTERY**.
3. Wejdź do panelu inżynieryjnego → **Tryb walidacyjny**, uruchom serię **20 cykli** na
   znanym dobrym zasilaczu, na każdym gnieździe osobno.
4. W raporcie serii (`validation\loop_*_raport.txt`) sprawdź:
   * 100 % PASS,
   * zapas do granicy specyfikacji (margines w mV i % szerokości okna) — mały margines
     oznacza, że stanowisko będzie produkować przypadkowe FAIL-e,
   * dryf napięcia w serii — rosnący dryf to grzejący się tester albo zasilacz.
5. Przetestuj jedną sztukę niesprawną, żeby potwierdzić, że stanowisko wykrywa FAIL.
6. Sprawdź, że w `reports\` powstał plik CSV i że ma kolumnę **Stanowisko**.

---

## 9. Utrzymanie

| Czynność | Częstotliwość |
|---|---|
| Archiwizacja `reports\` | wg procedury jakości |
| Seria walidacyjna 20 cykli na wzorcu | po każdej wymianie testera lub kabla |
| Sprawdzenie `config_audit.log` | przy audycie — jest tam każda zmiana konfiguracji z HRID |
| Czyszczenie `raw\`, `logs\` | automatyczne (retencja 30 / 14 dni) |

Aktualizacja aplikacji w wariancie EXE: patrz punkt 3.5 — podmieniasz EXE **razem
z folderem `_internal\`**. `test_config.json` i foldery danych zostają nietknięte.

---

## 10. Najczęstsze problemy przy uruchomieniu

| Objaw | Przyczyna | Co zrobić |
|---|---|---|
| „Nie znaleziono USBPDConsole.exe" | zła `console_path` | popraw ścieżkę w panelu inżynieryjnym |
| Gniazdo pokazuje BRAK TESTERA, kabel wpięty | zmieniony numer seryjny testera | przycisk **WYKRYJ TESTERY** |
| Widać jedno gniazdo zamiast dwóch | jeden wpis w `stations` | **WYKRYJ TESTERY**, potwierdź dołożenie kanału, uruchom ponownie |
| Aplikacja nie zapisuje raportu | brak praw do folderu | `icacls` wg punktu 3.3 |
| Interfejs nie startuje (wariant z Pythonem) | brak tcl/tk | doinstaluj Pythona z komponentem tcl/tk |
| EXE nie startuje albo zgłasza brak Tcl/Tk | skopiowano samo EXE bez `_internal\` | skopiuj cały folder wydania |
| Po aktualizacji aplikacja zachowuje się dziwnie | podmieniono EXE bez `_internal\` | podmień obie rzeczy z jednego buildu |
| Build zatrzymuje się na sumach SHA-256 | źródła zmienione po zatwierdzeniu | sprawdź zmiany i uruchom `approve_sources.bat` |
| Testery rozłączają się w trakcie testu | pasywny hub USB | wepnij bezpośrednio albo użyj huba z zasilaczem |
| Aplikacja odrzuca konfigurację przy zapisie | za wolna konsola na tym komputerze | sprawdź `check_dual.py`, podnieś `command_overhead_seconds` i zweryfikuj liczbę próbek |
