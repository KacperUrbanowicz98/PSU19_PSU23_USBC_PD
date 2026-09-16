# PSU19 Tester v4.0 — test zasilaczy USB-C PD na PM125

Aplikacja stanowiskowa do testu zasilaczy USB Power Delivery (SR213 PSU / SPBV36UK-W / SPBV36EU-W)
przy użyciu testera PassMark PM125 i konsoli `USBPDConsole.exe`.

Wersja 4.0 to poprawki z audytu wersji 3.3 plus spłaszczenie projektu: **wszystkie pliki leżą
w jednym folderze**, bez pakietów i podkatalogów z kodem.

## Struktura

```text
app.py                punkt startowy
config.py             konfiguracja, walidacja, audyt zmian
models.py             profile, pomiary, wyniki, kryteria
hardware.py           PM125 (CLI) i symulator
engine.py             przebieg testu i logika wyniku
storage.py            raport CSV, dane surowe, liczniki prob, wyniki oczekujace
logging_utils.py      logowanie i retencja
theme.py              paleta kolorow
ui.py                 ekran operatora
settings_ui.py        panel inzynieryjny
selftest.py           testy automatyczne (38 testow)
build_exe.py          budowanie EXE
test_config.json      konfiguracja stanowiska
run_app.bat / run_simulator.bat / run_tests.bat / build_exe.bat
```

Foldery danych aplikacja tworzy sama obok programu: `reports/`, `backup/`, `logs/`, `raw/`
oraz pliki `serial_attempts.json`, `pending_results.json`, `config_audit.log`.

## Uruchomienie

Python 3.10 lub nowszy:

```powershell
py app.py
```

albo `run_app.bat`. Aplikacja otwiera się również bez podłączonego PM125 — test jest wtedy
zablokowany do czasu połączenia.

### Tryb symulatora

```powershell
py app.py --simulate --simulate-mode pass
```

Scenariusze: `pass`, `voltage_fail`, `current_fail`, `read_error`, `load_error`,
`disconnected`, `no_dut`. Symulator odwzorowuje też rampę prądu po skoku obciążenia,
więc nadaje się do sprawdzenia okna stabilizacji.

### Testy automatyczne

```powershell
py -m unittest selftest -v
```

albo `run_tests.bat`. 38 testów pokrywa m.in. wszystkie błędy naprawione w tej wersji.

### Budowanie EXE

```powershell
build_exe.bat
```

Build najpierw uruchamia testy automatyczne i przerywa się, jeśli któryś nie przejdzie.
W `dist` powstaje `PSU19_Tester.exe` i `test_config.json` — oba pliki kopiuje się na stanowisko.

## Kryteria testu

Zakresy zgodne ze specyfikacją wyrobu:

| Profil | Obciążenie | Zakres napięcia |
|--------|-----------|-----------------|
| 5 V    | 3,0 A     | 4,75 – 5,50 V   |
| 9 V    | 3,0 A     | 8,55 – 9,45 V   |
| 12 V   | 3,0 A     | 11,40 – 12,60 V |
| 15 V   | 2,4 A     | 14,25 – 15,75 V |

Każdy profil: **2,5 s bez obciążenia + 2,5 s pod obciążeniem** (20 s samych pomiarów).

PASS wymaga, aby wszystkie **oceniane** próbki obu faz mieściły się w zakresie napięcia
i prądu. Prąd pod obciążeniem sprawdzany jest z tolerancją ±20 % nastawy — to kontrola
tego, czy obciążenie się załączyło i czy zasilacz nie wszedł w ograniczenie, a nie badanie
parametru zasilacza; parametrem wyrobu jest napięcie pod obciążeniem.

### Okno stabilizacji

Po każdej zmianie profilu i po każdym skoku obciążenia pierwsze `settling_ignore_seconds`
(domyślnie 0,6 s) jest **rejestrowane, ale nieoceniane**. Rampa prądu obciążenia
elektronicznego i czas reakcji regulacji zasilacza trwają dziesiątki–setki milisekund;
bez tego okna pierwsze próbki po skoku obciążenia odrzucały sprawne zasilacze.

Fazy nadal trwają 2,5 s — zmienia się tylko to, które próbki decydują o wyniku.
Na ekranie operatora próbki z okna stabilizacji mają status `STABILIZACJA` (pomarańczowy).

## Czas cyklu

| Etap | Czas |
|------|------|
| Kontrola obecności zasilacza | ~0,3 s |
| 4 profile (stabilizacja + 2×2,5 s pomiaru) | ~22,8 s |
| Okno wyniku PASS | 2,0 s |
| **Razem PASS** | **~25 s** |

Okno wyniku FAIL trwa 8 s i ma przyciski „Zamknij" oraz „Powtórz test". Czasy okien
wyniku można zmienić w panelu inżynieryjnym. Skrócenie samych faz pomiarowych wymagałoby
zmiany wymagania procesowego 2,5 s + 2,5 s.

## Statusy wyniku

| Status | Znaczenie |
|--------|-----------|
| `PASS` | Wszystkie profile wykonane i w zakresie |
| `FAIL` | Wszystkie profile wykonane, co najmniej jeden pomiar poza zakresem |
| `ERROR` | Błąd stanowiska — wynik wyrobu nierozstrzygnięty |
| `TIMEOUT` | Przekroczony globalny czas testu |
| `CANCELLED` | Test przerwany przez operatora |

**Błąd stanowiska ma pierwszeństwo przed FAIL.** Jeśli którykolwiek profil się nie wykonał,
wynik końcowy to `ERROR`, nawet gdy wcześniejszy profil wypadł FAIL. Zasilacz można odrzucić
tylko na podstawie testu, który faktycznie się odbył.

## Licznik prób numeru seryjnego

Próbę zużywa wyłącznie test zakończony oceną wyrobu (`PASS` lub `FAIL`). Błąd stanowiska,
timeout, anulowanie i brak zasilacza na stanowisku **nie** zużywają próby.

Licznik można podejrzeć i wyzerować w panelu inżynieryjnym (zakładka **Liczniki prób**).
Każde zerowanie trafia do `config_audit.log` i do logu aplikacji.

## Raport i dane

- `reports/raport_testow_<odcisk>_<n>.csv` — raport główny; `<odcisk>` to skrót zestawu
  kolumn. Zmiana zestawu profili zakłada **nowy plik**, więc kolumny nigdy się nie rozjadą.
- `backup/raport_BACKUP_*.csv` — kopia awaryjna, gdy raport główny jest niedostępny
  (np. otwarty w Excelu).
- `raw/<SN>_<data>.csv` — wszystkie próbki testu z oznaczeniem, które były oceniane
  i które mieściły się w zakresie. Podstawa analizy yieldu i rozstrzygania sporów o FAIL.
- `pending_results.json` — wyniki, których nie udało się zapisać. Przeżywają zamknięcie
  aplikacji; do czasu zapisania kolejny test jest zablokowany, a w oknie głównym widać
  baner z przyciskiem „Ponów zapis".

CSV: separator `;`, kodowanie UTF-8 z BOM, przecinek dziesiętny — otwiera się w polskim Excelu
bez importu. Wartości zaczynające się od `=`, `+`, `-`, `@` są neutralizowane.

## Panel inżynieryjny

Wejście: trzykrotne `Ctrl+Shift+D` w ciągu ~2 sekund, potem hasło inżynieryjne.
Hasło jest statyczne i nie zmienia się z poziomu aplikacji.

Zakładki: **Ogólne** (ścieżki, parametry, przełączniki), **Profile testowe**,
**Operatorzy HRID**, **Liczniki prób**, **Logi**.

Panel nie pozwala zapisać ustawień w trakcie trwającego testu. Każdy zapis zostawia wpis
w `config_audit.log` z listą zmienionych pól.

## Co zmieniono względem v3.3

Pełna lista w `CHANGELOG_PL.md`. Najważniejsze:

- błąd stanowiska nie jest już raportowany jako FAIL wyrobu,
- raport CSV nie rozjeżdża się po zmianie zestawu profili,
- błąd pojedynczej komendy nie zrywa połączenia z PM125, a obciążenie jest zerowane zawsze,
- licznik prób nie jest zużywany przez awarie stanowiska i da się go wyzerować z panelu,
- okno stabilizacji eliminuje fałszywe FAIL na rampie prądu,
- progi napięć zgodne ze specyfikacją wyrobu (5 V: 4,75 V zamiast 4,90 V),
- diagnostyka FAIL: konkretna przyczyna, min/max z obu faz, surowe próbki na dysku,
- niezapisane wyniki przeżywają zamknięcie aplikacji,
- kontrola obecności zasilacza przed testem i okresowa kontrola obecności PM125,
- wskaźnik „profil X z Y" i pasek postępu, etykieta pola HRID, przycisk „Powtórz test",
- raport, liczniki prób i logi nie obciążają już stanowiska przy dużej liczbie testów.

## Przed wdrożeniem

Kod przeszedł testy automatyczne i przebieg na symulatorze, ale **nie był uruchomiony
na fizycznym PM125**. Test odbiorczy na stanowisku:

1. Start bez PM125, podłączenie i „Połącz ponownie".
2. Weryfikacja formatu odpowiedzi `USBPDConsole.exe` względem parsera (`hardware.py`):
   jednostki `mV`/`mA` albo `V`/`A`, separator dziesiętny.
3. PASS na sprawnym zasilaczu; sprawdzenie, ile próbek zebrano (widoczne w oknie wyniku)
   i czy `settling_ignore_seconds = 0,6 s` wystarcza — jeśli w danych surowych widać
   dojście prądu do nastawy później, zwiększyć okno.
4. FAIL na próbce z napięciem poza zakresem oraz na próbce bez wymaganego prądu.
5. Odłączenie USB w trakcie testu — oczekiwany `ERROR`, nie `FAIL` ani `PASS`.
6. Test pustego stanowiska — oczekiwany komunikat „Brak zasilacza" w ~1 s, bez zużycia próby.
7. Otwarcie raportu w Excelu i wykonanie testu — oczekiwany zapis do kopii awaryjnej.
8. Zablokowanie raportu i backupu — wynik ma trafić do `pending_results.json`, a po
   restarcie aplikacji baner ma się pojawić ponownie.
9. Kontrola fizycznego powrotu PM125 do 5 V / 0 mA po każdym scenariuszu.
10. Pomiar rzeczywistego czasu cyklu i częstotliwości próbkowania na stanowisku.

Autor: Kacper Urbanowicz
