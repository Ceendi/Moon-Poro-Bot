# Odświeżanie rang i weryfikacja

Opis zachowania bota dla osób, które zmieniają kod. Konfiguracja produkcji jest w
[`PRODUCTION.md`](PRODUCTION.md).

## Kolejka rang

Worker pobiera najwyżej jedno najstarsze należne powiązanie co 10 sekund. Zajęcia rekordów są
trwałe i po pięciu minutach może je przejąć proces uruchomiony ponownie.

`RANK_REFRESH_POLICY` ma trzy tryby:

- `fixed` (domyślny) odświeża co `RANK_REFRESH_INTERVAL_HOURS`;
- `shadow` zostawia stały interwał, ale zapisuje proponowaną klasę i powód;
- `adaptive` wybiera 6, 12 albo 24 godziny wyłącznie na podstawie pobranej już odpowiedzi
  League-v4, bez Match-v5 i bez dodatkowych zapytań.

W trybie adaptacyjnym 6 godzin dostają aktywni gracze od Mastera w górę, świeża zmiana tieru i
aktywni gracze blisko granicy tieru. Wykryta aktywność, zmiana dywizji lub LP i stabilny Master lub
wyżej dostają 12 godzin. Stabilne i nieaktywne konta dostają 24 godziny. Deterministyczny jitter do
10% tylko przyspiesza termin. `RANK_REFRESH_ROLLOUT_PERCENT` pozwala włączać politykę stopniowo.

Pierwsza odpowiedź `200` bez Solo/Duo nie usuwa zapisanej rangi. Bot sprawdza ponownie po około
godzinie i dopiero druga zgodna odpowiedź ustawia Unranked. Odpowiedź `404` jest błędem.

Zmiana chronionej roli i powrót na serwer korzystają z zapisanej rangi bez zapytania do Riot.
Snapshot starszy niż godzina po zmianie roli dostaje priorytet najwyżej raz na godzinę. Powrót ze
snapshotem starszym niż 24 godziny dodaje odświeżenie do kolejki. Brak osoby na serwerze odkłada
sprawdzenie o siedem dni. Przycisk odświeżania ma cooldown 30 minut i tylko dodaje powiązanie do
tej samej kolejki.

## Błędy Riot i Discorda

Sukces Riot i synchronizacja ról w Discordzie są zapisywane osobno. Błąd nadawania roli jest
ponawiany z zapisanej rangi, bez nowego zapytania do Riot. Błędy sieci i 5xx mają wykładniczy
backoff z jitterem 20%. Po 429 bot respektuje `Retry-After` dla regionu. Odpowiedź 401 włącza
globalną blokadę autoryzacji, a 403 blokuje tylko rodzinę endpointów, która ją zwróciła. Obie
blokady przepuszczają jedną próbę kontrolną co 15 minut. Kolejka rang i profil sprawdzają też
blokadę League-v4.

Pojedyncze żądanie do Riot ma limit 15 sekund, połączenie 5 sekund, a brak danych 10 sekund. Limit
nie obejmuje czekania na limiter ani `Retry-After`; ponowienia wykonuje Pulsefire. Awaria
transportu nie blokuje workera na 5 minut.

Zmiana ról sprawdza pod blokadą tożsamość powiązania i wersję snapshotu. Lokalna blokada osoby jest
zawsze brana przed blokadą wiersza w bazie. Panel ról korzysta ze świeżego stanu Discorda i wyniku
wykonanych operacji, a nie zakłada, że `add_roles` od razu zmieni obiekt `Member` w pamięci.

Bez potwierdzonego snapshotu rangi bot nie zgaduje rangi ze zdarzenia Discorda. Przywraca role
wynikające z powiązania i zdejmuje niepotwierdzone role rang do czasu odświeżenia. Opóźnione
zdarzenie wywołane przez samego bota nie odwraca wtedy poprzedniej operacji.

## Weryfikacja i audyt

Publikacja wpisu audytu ma limit 10 sekund na blokady i operacje Discorda. Zamiar publikacji jest
zapisany wcześniej, więc timeout nie potwierdza częściowego wyniku ani nie kasuje zamiaru. Ponowienie
sprawdza historię kanału, żeby nie wysłać duplikatu. Sprzątanie audytu działa w osobnej pętli tego
samego procesu, niezależnie od kolejki rang.

Po ośmiu nieudanych próbach dokończenie weryfikacji przechodzi w stan `DISCORD_RETRY_PAUSED` i
zapisuje błąd w logu. Powiązanie i zamiary audytu zostają. Po naprawie uprawnień, kanału lub
konfiguracji osoba może kliknąć „Zweryfikuj konto”, żeby dokończyć tę samą weryfikację bez nowego
logowania Riot. Usunięcie powiązania anuluje tę pracę.

Po zakończeniu weryfikacji bot wysyła potwierdzenie (Riot ID, region, ranga Solo/Duo) prywatnie
pod wiadomością z linkiem. Zamiar wysyłki to kolumna `confirmation_due_at`, ustawiana w tej samej
transakcji, która kończy sesję, więc restart go nie gubi. Token interakcji Discorda jest tylko w
pamięci bota i działa 15 minut. Po jego wygaśnięciu albo po restarcie potwierdzenie idzie DM-em,
najpóźniej godzinę po zakończeniu weryfikacji. Wznowienie i weryfikacja ikoną podpinają swoje
kliknięcie, a potwierdzenie czeka, aż bot wyśle własną odpowiedź na to kliknięcie.

Usuwanie powiązania najpierw zapisuje stan oczekującego usunięcia, który blokuje nowe odświeżenia i
synchronizację ról. Bot zdejmuje rolę Zweryfikowany, sprząta wiadomość audytu i dopiero wtedy usuwa
rekord. Błąd Discorda jest ponawiany po restarcie, więc powiązanie nie zostaje w połowie usunięte.

## Strona logowania

Strona logowania pokazuje konto Discord i serwer zapisane przy wystawieniu linku oraz ostrzeżenie
przed linkami od innych osób. To ochrona interfejsu, a nie logowanie do Discorda w przeglądarce:
link jest jednorazowym sekretem, a pełne sprawdzenie odbiorcy wymagałoby dodatkowego logowania
Discord, którego nie robimy.

Limiter callbacku grupuje adresy IPv6 po prefiksie /64, trzyma najwyżej 4096 wpisów i przy pełnym
limicie odrzuca nowe adresy do wygaśnięcia starych. Nie chroni przed rozproszonym atakiem DDoS.
`/healthz` jest lekkim publicznym sprawdzeniem procesu, a `/readyz` (sprawdzenie bazy) działa tylko
lokalnie.
