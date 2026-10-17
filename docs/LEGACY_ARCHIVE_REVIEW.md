# Kontrola dawnego archiwum weryfikacji

Migracja `20260810_0001` zachowała dawną tabelę jako `legacy_zweryfikowani`.
Obecne usuwanie powiązania nie zmienia tej tabeli. Najpierw należy ustalić,
czy archiwum nadal istnieje w konkretnej instalacji; nie zakładamy tego na podstawie kodu.

## Raport bez zmiany danych

Właściwy identyfikator serwera Discord zastępuje `ID_SERWERA`. Polecenie musi
wskazywać świadomie wybraną bazę; poniższy przykład dotyczy produkcyjnej nazwy bazy.
Nie uruchamiać go jako części automatycznej migracji ani wdrożenia.

```bash
sudo -u postgres psql --dbname=moon_poro_prod --set=guild_id=ID_SERWERA \
  --file=/opt/moon-poro-prod/deploy/inspect-legacy-verification-archive.psql
```

Raport działa w transakcji `READ ONLY`, wypisuje nazwę bazy i same liczności.
Nie wypisuje identyfikatorów graczy ani kont Discord. Brak tabeli jest poprawnym wynikiem.
Rekord archiwum bez aktualnego powiązania może oznaczać wcześniejsze usunięcie przez
użytkownika; inny PUUID może oznaczać ponowną weryfikację. Żadna z tych kategorii nie
jest podstawą do automatycznego modyfikowania `verification_links`.

## Co dalej, jeśli archiwum istnieje

1. Potwierdzić zgodność migracji i potrzebne dane w aktualnych tabelach. Różnic nie
   wyrównywać przez ponowne kopiowanie dawnego archiwum: przywróciłoby to usunięte powiązania.
2. Potwierdzić gotowy backup, możliwość odtworzenia i obowiązującą retencję 90 dni.
3. Uzyskać osobne zatwierdzenie usunięcia wyłącznie dawnego archiwum z aktywnej bazy.
   Nie zmieniać wykonanej migracji `0001`; potrzebna jest osobna, kontrolowana operacja.
4. Po jej zatwierdzeniu sprawdzić, że liczba i zawartość aktualnych powiązań nie zmieniły się.

Usunięcie archiwum ograniczy możliwość cofnięcia do schematu sprzed `0001`; odtworzenie
tego schematu będzie wymagało backupu. Dane archiwalne mogą jeszcze pozostawać w kopiach
do końca ich retencji. Nie wolno traktować przywrócenia archiwum jako ponownej zgody
użytkownika na aktywne powiązanie.

W ramach bieżących poprawek przygotowano wyłącznie raport i tę procedurę.
Nie wykonano inspekcji produkcji ani usuwania tabel lub danych.
