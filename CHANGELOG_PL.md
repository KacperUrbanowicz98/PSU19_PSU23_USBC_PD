# Zmiany

## v5.0.0 — obsługa dwóch stanowisk i wnioski z diagnostyki konsoli

Diagnostyka na stanowisku (`check_dual.py`) wykazała, że dwie instancje
`USBPDConsole.exe` pracują równolegle — współczynnik 0,52 przy zerowych kolizjach.
To otworzyło drogę do stanowiska dwukanałowego z realnym podwojeniem przepustowości.

### Wiele kanałów pomiarowych

- Lista `stations` w konfiguracji: nazwa, numer seryjny testera, flaga aktywności.
  Jeden wpis → interfejs jak dotąd, dwa wpisy → dwie kolumny obok siebie. Jedna aplikacja
  obsługuje oba przypadki, bez osobnego buildu.
- Każdy kanał ma własnego klienta PM125, własny wątek, własne pole numeru seryjnego,
  własny przebieg testu i własny wynik. Okna modalne postępu i wyniku zastąpione panelami
  w kolumnie kanału — przy dwóch testach naraz okna modalne były nie do opanowania.
- Wspólne pozostają: zalogowany operator, raport, licznik prób, dane surowe, logi
  i panel inżynieryjny.
- Raport CSV dostał kolumnę **Stanowisko** (druga kolumna, zaraz po dacie).
- Ta sama sztuka nie może być testowana na dwóch gniazdach jednocześnie — numer seryjny
  jest rezerwowany na czas testu.
- Przy dwóch kanałach numer seryjny testera jest obowiązkowy; adres „Any" wybrałby to samo
  urządzenie dla obu gniazd, więc taka konfiguracja jest odrzucana przy zapisie.
- Nowa zakładka **Stanowiska** w panelu inżynieryjnym, z przyciskiem **WYKRYJ TESTERY**
  (komenda `-f`), który pokazuje numery seryjne podłączonych urządzeń.
- Przy starcie aplikacja porównuje konfigurację testerów (`-r`) i ostrzega, gdy kanały
  różnią się ustawieniami — wtedy ta sama sztuka może dostać inny wynik zależnie od gniazda.
- Stara konfiguracja z `device_serial` migruje automatycznie do jednego stanowiska.
- Tryb walidacyjny pyta, na którym stanowisku uruchomić serię; nazwa kanału trafia do
  nazwy plików raportu.

### Okna dopasowane do ekranu

- Panel inżynieryjny miał na sztywno 1300×1000 px i na monitorze 1080p **stopka
  z przyciskiem ZAPISZ USTAWIENIA wypadała poza ekran** — nie dało się zapisać zmian.
  Rozmiar okien (panel, tryb walidacyjny, okno główne) wynika teraz z rozdzielczości
  ekranu, a okna są wyśrodkowane tak, by nigdy nie wychodziły poza jego krawędzie.
- Stopka panelu jest przypięta do dołu okna i widoczna niezależnie od wysokości zawartości.
- Zakładki Ogólne, Stanowiska i Profile testowe mają pionowe przewijanie (także kółkiem
  myszy), więc działają również na niższych ekranach.

### Włączanie kolejnych kanałów

- Przy starcie aplikacja porównuje liczbę wykrytych testerów (`-f`) z liczbą
  skonfigurowanych stanowisk. Gdy testerów jest więcej, proponuje ustawić je jako osobne
  kanały — po potwierdzeniu zapisuje konfigurację i od razu startuje z nowym układem,
  bez restartu i bez wchodzenia do panelu.
- W zakładce **Stanowiska** przycisk **UŻYJ WYKRYTYCH** tworzy listę kanałów z faktycznie
  podłączonych testerów jednym kliknięciem.
- Domyślne nazwy: jeden kanał to „STANOWISKO", dwa to „LEWY" i „PRAWY".

### Wnioski z pomocy konsoli (`-h`) wdrożone w kodzie

- **`-c` zwraca status DUT, nie testera.** `STATUS:NOT CONNECTED` z kodem 0 oznacza sprawny
  tester z pustym gniazdem. Dotąd aplikacja uznawała to za brak połączenia z PM125, więc
  wskaźnik pokazywał „NIEPOŁĄCZONY" za każdym razem, gdy gniazdo było puste. Teraz
  dostępność testera wynika z kodu powrotu, a obecność zasilacza to osobna informacja.
- **Kody powrotu konsoli** (0–6) zamiast zgadywania po treści odpowiedzi. Kody 2 i 3
  oznaczają utratę testera i zrywają połączenie; 1, 4, 5 i 6 dotyczą samej komendy.
  Komunikaty w logu podają opis kodu, nie surowy numer.
- **Pre-kontrola obecności zasilacza przez `-p`** (`NUM PROFILES`) zamiast pomiaru napięcia:
  jedna komenda, jednoznaczna odpowiedź. Zero profili = puste gniazdo. Jeśli wyrób deklaruje
  mniej profili, niż wymaga test, trafia to do logu jako ostrzeżenie.
- **`command_timeout` 5 s → 8 s.** Odwołanie do nieobecnego testera trwa stabilne ~1,64 s,
  a pojedyncze komendy potrafią skoczyć do 1,28 s przy średniej 0,6 s.
- `find_devices()` — wykrywanie testerów komendą `-f` przy starcie; gdy skonfigurowany numer
  seryjny nie jest widoczny w systemie, aplikacja mówi o tym wprost przy uruchomieniu.

### Ekran operatora

- Układ dwukolumnowy z nazwami gniazd w nagłówkach kolumn (domyślnie LEWY / PRAWY).
- Panel spoczynkowy kanału pokazuje stan gniazda (GOTOWE / ZALOGUJ / BRAK TESTERA)
  oraz licznik PASS i FAIL tego kanału.
- Pasek operatora wspólny dla obu gniazd, z informacją, na którym trwa test.
- Historia sesji z kolumną stanowiska.

### Testy

- 11 nowych testów (razem 59): walidacja wielu stanowisk, wymóg numerów seryjnych, odrzucenie
  duplikatu, migracja starej konfiguracji, rezerwacja numeru między kanałami, nazwa
  stanowiska w wyniku i w CSV, pre-kontrola przez liczbę profili, kody powrotu konsoli,
  semantyka `-c`, parsowanie odpowiedzi `-f`.

### Narzędzia

- `check_dual.py` — diagnostyka stanowiska: pełna pomoc konsoli, wykrycie testerów,
  sprawdzenie adresacji, konfiguracja każdego testera, czasy komend i pomiar równoległości.
  Zapisuje cały przebieg do `check_dual_report.txt`. Używa wyłącznie komend odczytu.

### Do rozstrzygnięcia na stanowisku

- `CABLE RESISTANCE: 80` w konfiguracji obu testerów — jeśli to kompensacja rezystancji
  kabla, przy 3 A przesuwa odczyt napięcia o ~240 mV, co dotyka progu 4,75 V dla profilu 5 V.
- `-q load,slope` pozwala zadać nachylenie narastania prądu (mA/ms) — alternatywa dla okna
  stabilizacji, oparta na kontroli transjentu zamiast odrzucania próbek.
- `-x 1` (hold load przy zmianie napięcia) może skrócić cykl o część komend zerujących.

## v4.2.0 — tryb walidacyjny (seria testów) za hasłem inżynieryjnym

Nowy tryb do walidacji stanowiska przed wdrożeniem i po każdej zmianie konfiguracji.

- **Dostęp tylko z panelu inżynieryjnego** — przycisk „TRYB WALIDACYJNY (LOOP)" na dole
  panelu, czyli za hasłem. Z ekranu operatora nie ma do niego drogi.
- Seria N cykli (1–500) na jednej sztuce, z konfigurowalną przerwą między cyklami.
  Podgląd na żywo: licznik cykli, PASS/FAIL/ERROR, czas serii i czas pozostały.
- **Nie zapisuje do raportu produkcyjnego i nie zużywa licznika prób.** Dane trafiają do
  `validation/loop_<próbka>_<data>.csv`, podsumowanie do `..._raport.txt`.
- Na czas serii testy produkcyjne są zablokowane, a ekran operatora podaje powód.
  Serię można zatrzymać w dowolnym momencie; zamknięcie aplikacji w trakcie wymaga
  potwierdzenia.
- Błąd stanowiska w pojedynczym cyklu nie przerywa serii — zostaje policzony i opisany
  w sekcji zdarzeń.
- Raport statystyczny: PASS/FAIL/ERROR i yield, ocena powtarzalności, per profil i faza
  min/max/średnia/σ napięcia oraz zapas do bliższej granicy w mV i w % szerokości okna,
  dryf napięcia między cyklami (mV/cykl, regresja liniowa), statystyka prądu pod
  obciążeniem, średni czas cyklu i komendy PM125, jednozdaniowy wniosek.
- Ostrzeżenie, gdy najciaśniejszy zapas spada poniżej 5 % (ryzyko fałszywych odrzutów)
  lub 10 % szerokości okna kryterium.
- Nowe pliki: `loop_test.py` (logika i statystyka, bez GUI), `loop_ui.py` (okno).
- Nowe pola konfiguracji: `validation_directory`, `loop_default_cycles`, `loop_pause_seconds`.
- 6 nowych testów automatycznych (razem 48): zliczanie serii i brak wpływu na licznik prób,
  wyliczenie marginesu, seria z błędami stanowiska, przerwanie serii, zapis obu plików,
  wykrywanie dryfu.
- Statystyki sortowane w kolejności profili z konfiguracji (5 V, 9 V, 12 V, 15 V), nie
  alfabetycznie.

## v4.1.0 — dopasowanie do rzeczywistego tempa konsoli PM125

Na stanowisku okazało się, że jedno wywołanie `USBPDConsole.exe` trwa ~600 ms (32 próbki
na cały test, 0,9 Hz). Wersja 4.0 zakładała pomijalny narzut, stąd zacinanie się ekranu,
pasek postępu kończący się przed testem i czas cyklu 34 s zamiast zapowiadanych 25 s.

### Poprawność oceny

- Czas próbki liczony od **rozpoczęcia** odczytu, nie od jego zakończenia. Przy komendzie
  600 ms pierwsza próbka po skoku obciążenia była wcześniej datowana tak, jakby powstała
  po transjencie, i trafiała do oceny.
- Nowy parametr `settling_ignore_samples` (domyślnie 1): pierwsza próbka fazy nigdy nie
  jest oceniana, niezależnie od okna czasowego. Przy wolnej konsoli okno czasowe 0,6 s
  praktycznie nie działało.
- `minimum_samples` obniżone z 3 do 2. Przy 600 ms/komendę w fazie 2,5 s mieszczą się
  4 próbki (3 oceniane) — wymaganie 3 ocenianych nie zostawiało żadnego marginesu i przy
  odrobinę wolniejszym stanowisku dawało `NO_DATA` → `ERROR` na sprawnym zasilaczu.
- Walidacja konfiguracji odrzuca ustawienia, przy których w fazie nie zmieści się tyle
  ocenianych próbek, ile wymaga `minimum_samples`.
- Po teście z minimalnym marginesem próbek log zapisuje ostrzeżenie z faktycznym czasem
  komendy PM125.

### Czas cyklu

- Powtórzone komendy nie są wysyłane: klient PM125 pamięta ustawiony profil i obciążenie,
  a `safe_reset` zawsze wysyła je wymuszenie (`force=True`). Utrata łączności i rozłączenie
  kasują zapamiętany stan.
- Kontrola obecności zasilacza zaczyna od samego odczytu — po poprzednim teście PM125 jest
  już w stanie 5 V / 0 mA, więc zwykle wystarczy jedna komenda zamiast trzech.
- Faza nie przeciąga się o czas komendy: kolejny odczyt nie startuje, jeśli wybiegłby poza
  fazę, a komplet ocenianych próbek już jest.
- `profile_settle_seconds` 0,5 → 0,3 s, `load_settle_seconds` 0,1 → 0,0 s (pauzy zastąpione
  oknem próbek nieocenianych).
- Efekt: ~34 s → **~30 s testu, ~32 s cyklu** przy 600 ms/komendę. Czasy faz pomiarowych
  (2,5 s + 2,5 s = 20 s) bez zmian.
- `estimated_test_seconds()` uwzględnia teraz narzut komend (`command_overhead_seconds`),
  więc walidacja timeoutu i wyliczenie w panelu pokazują realny czas.

### Ekran operatora

- Pasek postępu liczony liczbą ukończonych faz, nie upływem czasu — kończy się razem
  z ostatnią fazą, zamiast dobijać do 100 % w połowie testu.
- Licznik „pomiar N (oceniane: M)" i animowany wskaźnik „odczyt z PM125…" odświeżany co
  250 ms — widać, że test trwa, mimo że wartości zmieniają się co ~0,6 s.
- Okno wyniku pokazuje zmierzony czas komendy PM125 (ms/komendę).

### Panel inżynieryjny

- Nowe pola: „Nieoceniane pierwsze próbki" i „Czas komendy PM125".
- Wyliczenie pod parametrami pokazuje: czas pomiarów, liczbę i koszt komend, szacowany czas
  testu oraz liczbę próbek na fazę (w tym ocenianych).

### Symulator i testy

- `--simulate-delay` ustawia opóźnienie jednej komendy w symulatorze; `--simulate-delay 0.6`
  odtwarza tempo realnego stanowiska.
- 4 nowe testy: datowanie próbki przy wolnej konsoli, brak przeciągania fazy, odrzucenie
  konfiguracji bez wystarczającej liczby ocenianych próbek, narzut w szacowanym czasie.
  Razem 42 testy.

## v4.0.0 — poprawki po audycie v3.3

Numeracja w nawiasach odsyła do rozdziałów raportu z audytu.

### Struktura projektu

- Projekt spłaszczony: wszystkie moduły `.py` leżą bezpośrednio w folderze aplikacji.
  Zlikwidowany pakiet `psu_tester/` i katalog `tests/`.
- Testy automatyczne w jednym pliku `selftest.py` (`py -m unittest selftest`).
- Foldery danych bez zagnieżdżenia: `reports/`, `backup/`, `logs/`, `raw/` obok programu.
- Usunięty martwy moduł `i18n.py` (nieużywany, interfejs jest wyłącznie polski).
- Usunięty zbędny handler `KeyRelease` zamieniający numer seryjny na wielkie litery —
  konwersja i tak następuje przy odczycie pola.
- `build_exe.py` napisany od nowa pod płaską strukturę; przed budowaniem uruchamia testy
  i przerywa build, jeśli któryś nie przejdzie.

### Błędy krytyczne (rozdz. 2)

- **(2.1)** Błąd stanowiska ma pierwszeństwo przed FAIL wyrobu. Wcześniej test, w którym
  jeden profil wypadł FAIL, a kolejny nie wykonał się z powodu awarii stanowiska, kończył
  się wynikiem `FAIL` — zasilacz był odrzucany na podstawie niedokończonego testu.
  Teraz każdy status oznaczający problem stanowiska (`PROFILE_ERROR`, `LOAD_ERROR`,
  `MEASUREMENT_ERROR`, `NO_DATA`, `TIMEOUT`, `CANCELLED`, `SAFE_RESET_ERROR`) daje `ERROR`.
- **(2.2)** Raport CSV nie rozjeżdża się po zmianie zestawu profili. Nazwa pliku zawiera
  odcisk zestawu kolumn, a przed dopisaniem wiersza sprawdzany jest nagłówek istniejącego
  pliku. Zmiana profili zakłada nowy plik zamiast dopisywać wiersze pod stary nagłówek.
- **(2.3)** Błąd pojedynczej komendy PM125 nie jest już traktowany jako utrata urządzenia.
  `connected = False` ustawiane jest tylko przy realnej utracie łączności (timeout, brak
  pliku konsoli, `OSError`), a udana komenda przywraca `connected = True`. Zerowanie
  obciążenia po profilu wykonywane jest **bezwarunkowo** — wcześniej po jednym zakłóceniu
  CLI obciążenie zostawało na zasilaczu do końca testu.
- **(2.4)** Licznik prób zużywa wyłącznie test zakończony oceną wyrobu (`PASS`/`FAIL`).
  Błąd stanowiska, timeout, anulowanie i brak zasilacza nie zużywają próby. W panelu
  inżynieryjnym dodana zakładka **Liczniki prób** z podglądem i zerowaniem (z wpisem
  audytowym). Komunikat o limicie mówi operatorowi, co zrobić dalej.

### Kryteria pomiarowe (rozdz. 3)

- **(3.1)** Wprowadzone okno stabilizacji `settling_ignore_seconds` (domyślnie 0,6 s).
  Próbki z okna są rejestrowane i zapisywane, ale nie decydują o wyniku. Kryteria i czasy
  faz (2,5 s + 2,5 s) pozostały bez zmian. Symulator odwzorowuje rampę prądu, a test
  automatyczny sprawdza, że bez okna ten sam wyrób daje FAIL, a z oknem PASS.
- **(3.2)** Progi napięć zgodne ze specyfikacją wyrobu: 5 V 4,75–5,50 V (było 4,90 V
  w pliku konfiguracji), 9 V 8,55–9,45 V, 12 V 11,40–12,60 V, 15 V 14,25–15,75 V.
  Usunięta rozbieżność między kodem, plikiem konfiguracji i dokumentacją.
- **(3.3)** Diagnostyka wyniku: konkretna przyczyna FAIL (co, w której fazie, jaka wartość,
  jaki zakres) w oknie wyniku, w CSV i w logu; min/max napięcia z obu faz; zapis wszystkich
  próbek do `raw/<SN>_<data>.csv` z oznaczeniem, które były oceniane; liczba próbek
  i efektywna częstotliwość próbkowania w raporcie i w oknie wyniku.

### Obsługa błędów (rozdz. 4)

- Repozytorium CSV łapie wszystkie wyjątki, nie tylko `OSError` — wynik nie ginie przy
  nietypowym błędzie zapisu.
- Niezapisane wyniki trafiają do `pending_results.json` i przeżywają zamknięcie aplikacji;
  po restarcie baner i blokada testu pojawiają się ponownie. Zamykanie aplikacji z
  niezapisanym wynikiem wymaga potwierdzenia.
- Rozłączanie PM125 przy zamykaniu wykonywane jest w osobnym wątku — okno nie zamarza,
  gdy urządzenie nie odpowiada.
- `set_profile` i `set_load` sprawdzają treść odpowiedzi konsoli, nie tylko kod powrotu.
- Komunikaty dla operatora zamiast surowej treści wyjątku Pythona; szczegóły idą do logu.
- Błąd pliku konfiguracji: próba odtworzenia z `.bak`, a następnie propozycja startu na
  ustawieniach domyślnych zamiast zamknięcia aplikacji. Nieznany klucz w pliku jest
  ostrzeżeniem w logu, a nie zatrzymaniem stanowiska.
- Okresowa kontrola obecności PM125 (domyślnie co 20 s w stanie bezczynności) — odpięcie
  USB między testami widać od razu na wskaźniku.
- `serial_attempts.json` zapisywany z `fsync`.
- Zapis konfiguracji jest logowany i trafia do `config_audit.log` z listą zmienionych pól.
- Panel inżynieryjny nie pozwala zapisać ustawień w trakcie trwającego testu.
- Walidacja konfiguracji odrzuca timeout testu krótszy niż przewidywany czas testu oraz
  okno stabilizacji dłuższe od najkrótszej fazy pomiarowej.

### Wydajność (rozdz. 5)

- Licznik wierszy raportu buforowany w pamięci — koniec z czytaniem całego pliku CSV przy
  każdym zapisie. Domyślny limit wierszy obniżony z 1 000 000 do 200 000.
- `serial_attempts.json` przechowuje datę ostatniej próby i jest czyszczony po
  `attempts_retention_days` (domyślnie 365 dni); obsługiwana migracja ze starego formatu.
- Dane surowe czyszczone po `raw_retention_days` (domyślnie 30 dni).
- Log do pliku domyślnie na poziomie INFO. DEBUG (log każdej komendy PM125) włącza się
  przełącznikiem w panelu albo parametrem `--debug`.
- Okno wyniku skrócone: 2 s dla PASS, 8 s dla FAIL, oba czasy konfigurowalne.
  Cykl PASS: ~25 s zamiast ~30 s, przy niezmienionych 20 s pomiarów.

### Ergonomia operatora (rozdz. 6)

- **(6.1)** Pole HRID ma etykietę i podpowiedź.
- **(6.3)** Okno testu pokazuje „Profil X z Y" i pasek postępu z rzeczywistym
  zaawansowaniem; próbki z okna stabilizacji mają własny status `STABILIZACJA`.
- **(6.6)** Kontrola obecności zasilacza przed testem: puste stanowisko kończy się
  komunikatem w ~1 s zamiast pełnym testem i wynikiem FAIL, i nie zużywa próby.
- **(6.7)** Okno wyniku ma przyciski „Zamknij" i „Powtórz test" (z liczbą pozostałych prób).
- Dodatkowo: komunikat pod polem numeru seryjnego mówi, który warunek blokuje test.

### Bez zmian

- Hasło inżynieryjne pozostaje statyczne i bez możliwości zmiany z poziomu aplikacji.
- Czasy faz pomiarowych: 2,5 s bez obciążenia + 2,5 s pod obciążeniem na każdy profil.
- Kryterium „wszystkie oceniane próbki w zakresie".

## v3.3 i wcześniejsze

Historia w poprzedniej wersji projektu.
