# PSU19 Tester v5.0 — test zasilaczy USB-C PD na PM125

Aplikacja stanowiskowa do testu zasilaczy USB Power Delivery (SR213 PSU / SPBV36UK-W / SPBV36EU-W)
przy użyciu testera PassMark PM125 i konsoli `USBPDConsole.exe`.

Wersja 4.x to poprawki z audytu wersji 3.3 plus spłaszczenie projektu: **wszystkie pliki leżą
w jednym folderze**, bez pakietów i podkatalogów z kodem.

## Struktura

```text
app.py                punkt startowy
config.py             konfiguracja, walidacja, audyt zmian
models.py             profile, pomiary, wyniki, kryteria
hardware.py           PM125 (CLI) i symulator
engine.py             przebieg testu i logika wyniku
storage.py            raport CSV, dane surowe, liczniki prób, wyniki oczekujące
logging_utils.py      logowanie i retencja
loop_test.py          tryb walidacyjny: seria testów i statystyka
loop_ui.py            okno trybu walidacyjnego
theme.py              paleta kolorów
ui.py                 okno główne: operator, kanały, statystyki
station_panel.py      panel jednego kanału pomiarowego
settings_ui.py        panel inżynieryjny
selftest.py           testy automatyczne (59 testów)
build_exe.py          budowanie EXE
test_config.json      konfiguracja stanowiska
run_app.bat / run_simulator.bat / run_tests.bat / build_exe.bat
```

Dodatkowo `check_dual.py` — diagnostyka konsoli i testerów, uruchamiana ręcznie.

Foldery danych aplikacja tworzy sama obok programu: `reports/`, `backup/`, `logs/`, `raw/`,
`validation/` oraz pliki `serial_attempts.json`, `pending_results.json`, `config_audit.log`.

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

albo `run_tests.bat`. 59 testów pokrywa m.in. wszystkie błędy naprawione w tej wersji.

### Budowanie EXE

```powershell
build_exe.bat
```

Build najpierw uruchamia testy automatyczne i przerywa się, jeśli któryś nie przejdzie.
W `dist` powstaje `PSU19_Tester.exe` i `test_config.json` — oba pliki kopiuje się na stanowisko.

## Stanowiska (jeden lub dwa testery)

Liczba kanałów wynika z listy `stations` w `test_config.json`. Jeden wpis to interfejs
jednokolumnowy, dwa wpisy to dwa niezależne gniazda obok siebie, każde z własnym testerem,
własnym polem numeru seryjnego, własnym przebiegiem i własnym wynikiem.

```json
"stations": [
  { "name": "LEWY",  "device_serial": "PMPD80LLAD", "enabled": true },
  { "name": "PRAWY", "device_serial": "PMPD80LN6E", "enabled": true }
]
```

### Jak włączyć drugie gniazdo

**Najprościej:** podłącz oba testery i uruchom aplikację. Jeśli widzi więcej testerów niż
skonfigurowanych stanowisk, sama zapyta, czy ustawić je jako osobne kanały — po
potwierdzeniu zapisuje konfigurację i od razu startuje z dwiema kolumnami. Bez restartu
i bez wchodzenia do panelu.

**Z panelu inżynieryjnego:** trzy razy `Ctrl+Shift+D`, hasło, zakładka **Stanowiska**,
przycisk **UŻYJ WYKRYTYCH** (tworzy listę kanałów z podłączonych testerów), potem
**ZAPISZ USTAWIENIA** i ponowne uruchomienie aplikacji. Pojedyncze stanowiska można też
dodawać i edytować ręcznie — nazwa i numer seryjny.

**Ręcznie w pliku:** wpisz listę `stations` jak wyżej w `test_config.json`.

Numery seryjne testerów odczytasz komendą `USBPDConsole.exe -f` (odpowiedź w postaci
`0=PMPD80LLAD`) albo przyciskiem **WYKRYJ TESTERY** w panelu. Przy dwóch kanałach numer
seryjny jest obowiązkowy — adres „Any" wybrałby to samo urządzenie dla obu gniazd, więc
konfiguracja bez numerów jest odrzucana.

Co jest wspólne, a co osobne:

| Osobne dla każdego kanału | Wspólne dla całego stanowiska |
|---|---|
| tester PM125 i jego wątek | zalogowany operator (HRID) |
| pole numeru seryjnego, przebieg, wynik | raport CSV (kolumna „Stanowisko") |
| licznik PASS/FAIL kanału | licznik prób numerów seryjnych |
| wskaźnik dostępności testera | dane surowe, logi, panel inżynieryjny |

Ta sama sztuka nie może być testowana na obu gniazdach jednocześnie — numer seryjny jest
rezerwowany na czas testu, a próba zeskanowania go na drugim gnieździe kończy się
komunikatem, które gniazdo go właśnie trzyma.

Przy starcie aplikacja porównuje konfigurację testerów (`-r`). Różnica — na przykład w
`CABLE RESISTANCE` albo `HOLD LOAD ON VOLTAGE CHANGES` — oznacza, że ta sama sztuka dostanie
inny wynik zależnie od gniazda, więc pojawia się ostrzeżenie. Kontrolę wyłącza pole
`verify_station_configuration`.

### Czas cyklu przy dwóch kanałach

Zmierzone na stanowisku: dwie instancje `USBPDConsole.exe` pracują równolegle
(współczynnik 0,52 przy zerowych kolizjach), więc kanały nie konkurują o konsolę. Test trwa
tyle samo co przy jednym gnieździe (~30 s), ale biegną dwa naraz:

| | Jeden kanał | Dwa kanały |
|---|---|---|
| Czas testu | ~30 s | ~30 s na kanał |
| Cykl operatora | ~32 s/szt. | **~16 s/szt.** |
| Próbek na fazę | 4 (3 oceniane) | 4 (3 oceniane) — bez zmian |

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

Po każdej zmianie profilu i po każdym skoku obciążenia próbki są **rejestrowane, ale
nieoceniane** — w dwóch wariantach naraz:

- `settling_ignore_seconds` (0,6 s) — okno czasowe, działa przy szybkiej konsoli,
- `settling_ignore_samples` (1) — pierwsza próbka fazy nigdy nie jest oceniana, co
  zabezpiecza przy wolnej konsoli, gdzie jedna próbka trwa dłużej niż całe okno czasowe.

Czas próbki liczony jest od **momentu rozpoczęcia odczytu**, nie jego zakończenia — przy
komendzie trwającej 600 ms ta różnica decyduje o tym, czy próbka z transjentu trafi do oceny.

Fazy nadal trwają 2,5 s — zmienia się tylko to, które próbki decydują o wyniku.
Na ekranie operatora próbki z okna stabilizacji mają status `STABILIZACJA` (pomarańczowy).

### Ile próbek zmieści się w fazie

Przy 600 ms na komendę w fazie 2,5 s mieszczą się **4 próbki, z czego 3 oceniane**
(`minimum_samples` = 2). Panel inżynieryjny pokazuje to wyliczenie na żywo, konfiguracja
z niewystarczającą liczbą ocenianych próbek jest odrzucana przy zapisie, a po teście,
w którym margines był minimalny, w logu pojawia się ostrzeżenie.

Jeśli konsola na stanowisku okaże się wolniejsza niż 600 ms, trzeba zmienić
`command_overhead_seconds` w panelu — wtedy walidacja od razu pokaże, czy przy takim
tempie kryteria da się jeszcze utrzymać.

## Czas cyklu

Czas testu zdominowany jest przez **narzut wywołań `USBPDConsole.exe`**. Każda komenda to
osobne uruchomienie procesu; na stanowisku zmierzono **~600 ms na komendę** (widać to w logu
po każdym teście: „średnia komenda PM125 … ms" oraz w oknie wyniku).

| Etap | Czas przy 600 ms/komendę |
|------|--------------------------|
| Kontrola obecności zasilacza (1 komenda) | ~0,6 s |
| Fazy pomiarowe (wymaganie 4 × 2×2,5 s) | 20,0 s |
| Stabilizacja profili (4 × 0,3 s) | 1,2 s |
| Komendy sterujące (15 × 0,6 s) | ~9,0 s |
| **Test razem** | **~30 s** |
| Okno wyniku PASS | 2,0 s |
| **Cykl operatora** | **~32 s** |

Zmierzone na symulatorze z opóźnieniem 600 ms: test 30,6 s, cykl 33,9 s (z zapisem).
Wcześniejsza deklaracja ~25 s nie uwzględniała narzutu konsoli i była błędna.

Co jeszcze skraca cykl:

- **Mniej wywołań konsoli.** Każda komenda mniej to −0,6 s. Warto sprawdzić, czy
  `USBPDConsole.exe` przyjmuje kilka przełączników w jednym wywołaniu (np. `-v 2 -q 3000`);
  połączenie ustawienia profilu z ustawieniem obciążenia dałoby ~−2,4 s na test.
- **Wyłączenie kontroli obecności zasilacza** (panel → Przełączniki): −0,6 s, kosztem
  szybkiego wykrywania pustego stanowiska.
- **Skrócenie faz pomiarowych**: wymaga zmiany wymagania procesowego 2,5 s + 2,5 s.

Aplikacja nie przeciąga faz: kolejny odczyt nie startuje, jeśli i tak wybiegłby poza fazę,
a komplet ocenianych próbek już jest. Powtórzone komendy (ten sam profil/obciążenie, które
już są ustawione) nie są wysyłane ponownie; `safe_reset` zawsze wysyła je wymuszenie.

Okno wyniku FAIL trwa 8 s i ma przyciski „Zamknij" oraz „Powtórz test". Czasy okien
wyniku można zmienić w panelu inżynieryjnym.

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

## Tryb walidacyjny (seria testów)

Służy do sprawdzenia dwóch rzeczy naraz: **powtarzalności stanowiska** (czy ten sam egzemplarz
daje ten sam wynik w kolejnych cyklach) i **marginesu wyrobu** (jak blisko granic kryteriów
leżą rzeczywiste pomiary).

Dostęp: panel inżynieryjny → przycisk **TRYB WALIDACYJNY (LOOP)**, czyli wyłącznie po haśle
inżynieryjnym. Operator nie ma jak tam trafić z ekranu produkcyjnego.

Zasady:

- seria **nie zapisuje się do raportu produkcyjnego** — dane idą do `validation/`,
- seria **nie zużywa licznika prób** żadnego numeru seryjnego,
- na czas serii testy produkcyjne są zablokowane, a ekran operatora informuje dlaczego,
- serię można zatrzymać w dowolnym momencie; statystyka liczy się z wykonanych cykli,
- błąd stanowiska w jednym cyklu nie przerywa serii — zostaje policzony i opisany.

Powstają dwa pliki:

- `validation/loop_<próbka>_<data>.csv` — jeden wiersz na cykl, format jak raport produkcyjny,
- `validation/loop_<próbka>_<data>_raport.txt` — podsumowanie, to samo co widać w oknie.

Raport zawiera: liczbę PASS/FAIL/ERROR i yield, ocenę powtarzalności, dla każdego profilu
i fazy min/max/średnią/σ napięcia oraz **zapas do bliższej granicy** w mV i w % szerokości
okna kryterium, trend napięcia między cyklami (dryf w mV/cykl), statystykę prądu pod
obciążeniem, listę zdarzeń oraz jednozdaniowy wniosek.

### Jak to sensownie przeprowadzić

1. **Sprawny egzemplarz, 20–30 cykli.** Sztuka zostaje w stanowisku na całą serię — inaczej
   mierzysz też powtarzalność styku, nie samego stanowiska. Oczekiwane: 100 % PASS, zero ERROR.
2. **Egzemplarz graniczny**, jeśli da się taki zdobyć (napięcie blisko dolnej granicy).
   Tu wychodzi, czy kryterium nie przechodzi przez rozrzut pomiaru: seria, w której ta sama
   sztuka raz przechodzi, a raz nie, to sygnał, że okno pomiarowe jest za ciasne.
3. **Egzemplarz wadliwy.** Oczekiwane: 100 % FAIL i za każdym razem ta sama przyczyna
   w opisie naruszenia.
4. **Puste stanowisko i wyrwanie kabla w trakcie** — oczekiwane `ERROR` lub komunikat
   o braku zasilacza, nigdy `PASS` ani `FAIL`.
5. **Dłuższa seria (50+)** jeśli podejrzewasz nagrzewanie — patrz na dryf w raporcie.

Interpretacja zapasu: poniżej 5 % szerokości okna raport ostrzega, bo przy rozrzucie
produkcyjnym należy się spodziewać fałszywych odrzutów. Poniżej 10 % warto obserwować.

Czas: seria 20 cykli po ~30 s to ~11 minut z przerwami 2 s. Okno pokazuje szacunek przed
startem i czas pozostały w trakcie.

## Panel inżynieryjny

Wejście: trzykrotne `Ctrl+Shift+D` w ciągu ~2 sekund, potem hasło inżynieryjne.
Hasło jest statyczne i nie zmienia się z poziomu aplikacji.

Zakładki: **Ogólne** (ścieżki, parametry, przełączniki), **Stanowiska**, **Profile testowe**,
**Operatorzy HRID**, **Liczniki prób**, **Logi**. Na dole przycisk **TRYB WALIDACYJNY (LOOP)**.

Zmiana liczby stanowisk wymaga ponownego uruchomienia aplikacji — układ okna powstaje przy
starcie.

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
