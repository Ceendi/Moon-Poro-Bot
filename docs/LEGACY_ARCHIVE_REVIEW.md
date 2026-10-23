# Dawne archiwum weryfikacji

Migracja `20260810_0001` zostawiła starą tabelę weryfikacji jako `legacy_zweryfikowani`.
Usuwanie powiązania jej nie zmienia, więc mogą w niej zostać dane osób, które usunęły powiązanie.
Zanim cokolwiek zrobisz, sprawdź, czy ta tabela jest w danej bazie.

## Raport tylko do odczytu

Wpisz ID serwera Discord zamiast `ID_SERWERA` i upewnij się, że to właściwa baza (przykład jest
dla produkcji). Nie uruchamiaj raportu w migracji ani we wdrożeniu.

```bash
sudo -u postgres psql --dbname=moon_poro_prod --set=guild_id=ID_SERWERA \
  --file=/opt/moon-poro-prod/deploy/inspect-legacy-verification-archive.psql
```

Raport działa w transakcji tylko do odczytu i wypisuje nazwę bazy oraz liczby, bez ID graczy i
kont Discord. Brak tabeli to poprawny wynik. Wpis w archiwum bez aktualnego powiązania zwykle
znaczy, że ta osoba usunęła powiązanie, a inny PUUID, że zweryfikowała się ponownie. Żaden z tych
wyników nie jest powodem do zmiany `verification_links`.

## Jeśli archiwum istnieje

1. Sprawdź, czy aktualne tabele mają wszystkie potrzebne dane. Nie kopiuj niczego z archiwum, bo
   przywróciłoby to usunięte powiązania.
2. Sprawdź, czy jest świeży backup i czy da się go odtworzyć.
3. Usunięcie archiwum wymaga osobnej zgody właściciela i osobnej operacji. Nie zmieniaj migracji
   `0001`.
4. Po usunięciu sprawdź, czy aktualne powiązania się nie zmieniły.

Po usunięciu archiwum powrót do schematu sprzed `0001` wymaga backupu. Dane z archiwum zostają w
kopiach do końca ich przechowywania (90 dni). Przywrócenie archiwum nie jest zgodą na ponowne
powiązanie konta.
