# Zmiany

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
