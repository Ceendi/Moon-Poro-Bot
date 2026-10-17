# Wdrożenie RSO na GCP e2-micro (1 GB RAM)

Architektura nie używa Dockera ani serwera Node.js:

```text
Internet → Caddy :443 ─┬─ pliki statyczne Astro (0 MB procesu aplikacji)
                       └─ /verify/* + /oauth2/callback → aiohttp :8080

Discord Gateway → bot discord.py ─┐
RSO aiohttp callback ──────────────┴→ PostgreSQL na 127.0.0.1
```

Bot i callback są oddzielnymi użytkownikami systemowymi. Tylko `moon-poro-rso` czyta sekret
klienta RSO; tylko `moon-poro-bot` czyta token Discord i produkcyjny klucz Riot API. Wspólny jest
kod tylko do odczytu i baza z oddzielnymi loginami.

> [!IMPORTANT]
> Produkcja weryfikuje przez RSO (`VERIFICATION_MODE=rso`). Weryfikacja ikoną zostaje w kodzie
> tylko jako awaryjny powrót. Sama aktualizacja kodu nie zmienia `VERIFICATION_MODE` istniejącej
> instalacji. Produkcyjna VM `moon-poro-web` używa układu z README (`/opt/moon-poro-prod`,
> `moon-poro-prod-rso.service`); ten dokument opisuje ogólną instalację w `/opt/moon-poro`.

## 1. Warunki wstępne

- wspierany system Linux i Python 3.12 lub nowszy;
- Caddy 2.6 lub nowszy (pakiet z dystrybucji wystarczy);
- wspierany PostgreSQL (nie wdrażaj publicznego RSO na niewspieranym PostgreSQL 11);
- domena, której jesteś właścicielem, rekord `A` do stałego zewnętrznego IP VM;
- firewall GCP: Internet ma dostęp tylko do TCP 80/443; SSH ograniczony do zaufanych adresów;
- porty 5432 i 8080 nie są publiczne;
- snapshot i zweryfikowany backup bazy przed migracją.
- Na kanale audytu bot potrzebuje „Wyświetlanie kanału”, „Wysyłanie wiadomości”,
  „Zamieszczanie linków” (embedy) i „Czytanie historii wiadomości”. Historia pozwala
  odzyskać wpis po awarii bez wysyłania duplikatu; brak dostępu wstrzymuje finalizację RSO.

Migracja `20261017_0008` dodaje tylko dwa opcjonalne pola do sesji RSO: skrót
identyfikatora przeglądarki i kanał oczekującej publikacji audytu. Nie zmienia powiązań
ani danych rankingowych. Przed migracją zatrzymaj bot i callback; rozpoczęte wcześniej
logowania bez powiązania z przeglądarką wymagają nowego linku. Przy powrocie do legacy
nie cofaj migracji ani nie usuwaj nierozstrzygniętych zamiarów publikacji audytu.

Migracja `20261017_0009` dodaje metodę weryfikacji do sesji (dotychczasowe sesje pozostają RSO).
Weryfikacja ikoną również zapisuje trwałe zadanie finalizacji przed publikacją audytu.
Po przełączeniu na RSO bot może dokończyć już zaakceptowane wyzwania ikoną. Po powrocie
do legacy finalizuje tylko wyzwania ikoną, ale sprząta nierozstrzygnięte audyty obu metod.

Migracja `20261017_0010` dodaje tylko dwie opcjonalne nazwy do sesji: konto Discord i serwer.
Nie zmienia istniejących powiązań ani rangi. Stare sesje bez tych nazw nadal pokazują identyfikatory.

## 2. Budowa strony poza VM

Node.js jest potrzebny tylko na komputerze budującym. Produkcyjna VM otrzymuje katalog `dist`:

```bash
cd site
cp .env.example .env
# Uzupełnij wszystkie PUBLIC_* prawdziwymi danymi.
npm ci
npm run build
```

Prześlij repozytorium wraz z `site/dist` do `/opt/moon-poro`. Na VM nie instaluj Node ani npm.

## 3. Użytkownicy i sekrety

Utwórz użytkowników bez logowania: `moon-poro-bot` i `moon-poro-rso`. Kod w
`/opt/moon-poro` powinien należeć do `root:root` i być dla nich tylko do odczytu. Utwórz:

- `/etc/moon-poro/bot.env`, właściciel `moon-poro-bot`, tryb `0600`, na podstawie `.env.example`;
- `/etc/moon-poro/rso.env`, właściciel `moon-poro-rso`, tryb `0600`, na podstawie
  `.env.rso.example`.

Nie kopiuj sekretów do katalogu strony, repozytorium, Caddyfile, jednostki systemd ani zgłoszenia do Riot.
Po przyznaniu RSO wklej client assertion albo client secret tylko do `rso.env`.

## 4. Minimalne uprawnienia bazy dla callbacku

Po wykonaniu migracji przez konto właściciela bazy utwórz osobny login callbacku. Dostosuj nazwy
bazy i hasło:

```sql
CREATE ROLE moon_poro_rso LOGIN PASSWORD 'WYGENEROWANE-DLUGIE-HASLO'; -- pragma: allowlist secret
GRANT CONNECT ON DATABASE moon_poro TO moon_poro_rso;
GRANT USAGE ON SCHEMA public TO moon_poro_rso;
GRANT SELECT, INSERT, UPDATE, DELETE
  ON TABLE verification_links, verification_sessions TO moon_poro_rso;
GRANT USAGE, SELECT ON SEQUENCE verification_sessions_id_seq TO moon_poro_rso;
```

Bot nadal wykonuje migracje przy starcie, więc jego login musi być właścicielem schematu. Migracje
można też uruchomić bez połączenia z Discordem, korzystając z konfiguracji bota:

```bash
sudo -u moon-poro-bot /opt/moon-poro/.venv/bin/python -m moon_poro.migrate \
  --env-file /etc/moon-poro/bot.env \
  --legacy-audit-channel-id ID_HISTORYCZNEGO_KANALU
```

Nie używaj tutaj bezpośrednio `alembic upgrade head`: migracje projektu potrzebują dodatkowych
wartości z konfiguracji bota. Przed pierwszym uruchomieniem migracji `20260824_0007` sprawdź,
czy wszystkie istniejące wiadomości weryfikacyjne wskazane przez `verification_links.message_id`
powstały w jednym kanale. Jego potwierdzony identyfikator podaj w
`--legacy-audit-channel-id`; nie zakładaj, że jest to obecna wartość
`ZWERYFIKOWANI_CHANNEL_ID`. Gdy kanał nigdy nie był zmieniany, obie wartości będą takie same.
W produkcyjnej konfiguracji tę samą, potwierdzoną wartość można zapisać jako
`LEGACY_AUDIT_CHANNEL_ID`; bot odczyta ją podczas migracji wykonywanej przy starcie.

Jeśli historyczne wiadomości znajdują się w kilku kanałach, nie uruchamiaj tej migracji z jednym
identyfikatorem. Najpierw przygotuj osobny, zweryfikowany backfill przypisujący właściwy kanał do
każdego wpisu. Dla nowej, pustej bazy parametr `--legacy-audit-channel-id` można pominąć. Bot
uruchomiony przed bezpiecznym backfillem celowo przerwie migrację zamiast przypisać błędny kanał.

## 5. Python i usługi

W `/opt/moon-poro`:

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install uv==0.12.3
.venv/bin/uv sync --frozen --no-dev
```

Skopiuj jednostki z `deploy/` do `/etc/systemd/system/`. Plik `deploy/Caddyfile` jest już
skonfigurowany dla `moonporo.pl`. Sprawdź go przed skopiowaniem do `/etc/caddy/`:

```bash
caddy validate --config deploy/Caddyfile --adapter caddyfile
systemctl daemon-reload
systemctl enable --now moon-poro.service caddy.service
systemctl enable --now moon-poro-rso.service
```

Caddy automatycznie uzyska certyfikat po poprawnym DNS i dostępie z Internetu do portów 80/443.

## 6. Kolejność pierwszego wdrożenia

1. Przed zatrzymaniem starego bota wykonaj snapshot oraz zweryfikowany logiczny backup bazy.
2. Ustaw `VERIFICATION_MODE=legacy_icon` i potwierdź działanie weryfikacji ikoną. Nie uruchamiaj
   jeszcze usługi RSO.
3. Po otrzymaniu danych klienta wdróż kod RSO oraz zbudowany poza VM katalog `site/dist`.
4. Zatrzymaj legacy bota na czas finalnego przełączenia i uruchom migracje kontem właściciela
   schematu. Przy pierwszym przejściu przez migrację `20260824_0007` użyj pełnego polecenia z
   sekcji 4 wraz z potwierdzonym `--legacy-audit-channel-id`. Po zastosowaniu tej migracji parametr
   nie jest już potrzebny.
5. Uruchom nowego bota i nadaj minimalne granty loginowi `moon_poro_rso`.
6. Wpisz client assertion albo client secret wyłącznie do `/etc/moon-poro/rso.env`.
7. Sprawdź Caddy, uruchom callback i testuj pełny przepływ na prywatnym kanale.
8. Jeżeli Riot zażąda weryfikacji domeny, umieść dokładny tekst w `site/dist/riot.txt` i usuń
   plik po zakończeniu.
9. Dopiero po udanych testach ustaw `VERIFICATION_MODE=rso`, zrestartuj bota i opublikuj nowy panel
   `/weryfikacja`.
10. W razie niepowodzenia wyłącz RSO i przywróć legacy bota bez cofania migracji
    `20260810_0002`.

## 7. Kontrola działania

```bash
systemctl status moon-poro moon-poro-rso caddy
curl --fail https://moonporo.pl/healthz
curl --fail http://127.0.0.1:8080/readyz
journalctl -u moon-poro-rso --since today
```

Sprawdź przypadki: sukces, odmowa w Riot, link po 10 minutach, ponowne użycie, dwa konta Discord dla
jednego PUUID, ponowne kliknięcie, wyjście z serwera w trakcie, brak DM, chwilowy błąd Riot/Discord,
`/usun_weryfikacje` oraz ponowne wejście na serwer.

## 8. Budżet pamięci e2-micro

Orientacyjny cel, nie gwarantowany limit:

| Proces | Typowe dążenie | Limit systemd |
|---|---:|---:|
| bot Python | 150-280 MB | 400 MB |
| RSO aiohttp | 45-85 MB | 128 MB |
| PostgreSQL | 120-220 MB | konfiguracja DB |
| Caddy + system | 80-160 MB | system |

Dla PostgreSQL zacznij od `shared_buffers=64MB`, `work_mem=2MB`,
`maintenance_work_mem=32MB`, `max_connections=20` i monitoruj, zamiast ślepo zwiększać cache. Utrzymuj
pulę bota na maksymalnie 5 połączeń oraz callbacku na 2. Skonfiguruj 1-2 GB swap jako zabezpieczenie
przed OOM (nie jako zamiennik RAM), alert dla pamięci >85%, dysku >80% i restartów usług.

Static Astro nie zużywa pamięci aplikacyjnej. Caddy kompresuje pliki raz przy odpowiedzi, a strona
nie ładuje bibliotek JS, analityki ani zasobów zewnętrznych. Fonty są serwowane z tej samej domeny.

## 9. Odświeżanie rang

Migracja `20260814_0003` rozkłada istniejące powiązania równomiernie na pierwsze 24 godziny, a
`20260815_0005` addytywnie zapisuje pełny snapshot Solo/Duo potrzebny schedulerowi. Worker pobiera
najwyżej jeden najstarszy należny rekord co 10 sekund. Claimy są trwałe i po pięciu minutach mogą
zostać bezpiecznie przejęte przez proces uruchomiony ponownie.

`RANK_REFRESH_POLICY` ma trzy tryby:

- `fixed`, bezpieczna wartość domyślna, zachowuje `RANK_REFRESH_INTERVAL_HOURS`;
- `shadow`, zachowuje rzeczywisty interwał stały, ale zapisuje proponowaną klasę i powód;
- `adaptive`, stosuje 6, 12 albo 24 godziny wyłącznie na podstawie już pobranej odpowiedzi
  League-v4. Nie wykonuje Match-v5 ani dodatkowych zapytań.

W trybie adaptacyjnym 6 godzin otrzymują aktywni gracze Master i wyżej, świeża zmiana tieru oraz
aktywni gracze blisko granicy tieru. Wykryta aktywność, zmiana dywizji lub LP i stabilny Master lub
wyżej otrzymują 12 godzin. Stabilne i nieaktywne konta otrzymują 24 godziny. Deterministyczny jitter
do 10% tylko przyspiesza termin. `RANK_REFRESH_ROLLOUT_PERCENT` pozwala wdrażać politykę stopniowo.

Pierwsza poprawna odpowiedź `200` bez Solo/Duo nie usuwa zapisanej rangi. Bot potwierdza stan po
około godzinie i dopiero drugie zgodne `200` ustawia Unranked. Odpowiedź `404` pozostaje błędem.

Zmiana chronionej roli i ponowne wejście korzystają z cache bez dodatkowego zapytania do Riot.
Snapshot starszy niż godzinę po zmianie roli dostaje trwały priorytet najwyżej raz na godzinę.
Powrót użytkownika ze snapshotem starszym niż 24 godziny dodaje odświeżenie do kolejki. Brak
użytkownika na serwerze odkłada sprawdzenie o siedem dni. Przycisk odświeżania ma trwały cooldown
30 minut i tylko dodaje istniejące powiązanie do tej samej kolejki.

Sukces Riot i synchronizacja Discord są zapisywane osobno. Błąd nadawania roli korzysta z trwałego
retry na podstawie cache i nie ponawia zapytania Riot. Błędy sieciowe i 5xx mają wykładniczy backoff
z jitterem 20%. Regionalny breaker nadal respektuje `Retry-After` po 429. Odpowiedź 401 otwiera
globalną blokadę autoryzacji. Odpowiedź 403 blokuje rodzinę endpointów, która ją zwróciła,
bez zatrzymywania pozostałych API. Oba mechanizmy dopuszczają pojedynczą próbę kontrolną
co 15 minut. Kolejka rang i profil sprawdzają również blokadę League-v4.

Transport Riot ma limit 15 sekund na pojedyncze żądanie, 5 sekund na połączenie i 10 sekund
bez odbierania danych. Limit nie obejmuje oczekiwania limitera ani `Retry-After`; ponowienia
nadal wykonuje Pulsefire. Awaria transportu nie blokuje pojedynczą próbą workera na 5 minut.

Zmiana ról sprawdza pod blokadą zarówno tożsamość powiązania, jak i wersję snapshotu.
Lokalna blokada użytkownika jest zawsze pobierana przed blokadą rekordu bazy. Panel ról
korzysta ze świeżego stanu Discorda oraz jawnego wyniku wykonanych operacji, a nie zakłada,
że `add_roles` od razu zmieni obiekt `Member` w pamięci.

Jeśli nie ma jeszcze potwierdzonego snapshotu rangi, bot nie zgaduje rangi na podstawie
zdarzenia Discorda. Przywraca role wynikające z powiązania i usuwa niepotwierdzone role rang
do czasu odświeżenia przez istniejącą kolejkę. Opóźnione zdarzenie wywołane przez samego bota
nie odwraca wtedy poprzedniej operacji.

Publikacja audytu ma limit 10 sekund obejmujący oczekiwanie na blokady i operacje Discorda.
Trwała intencja publikacji jest zapisana wcześniej. Timeout nie potwierdza częściowego wyniku
ani nie kasuje intencji. Ponowienie nadal sprawdza historię, aby nie duplikować wiadomości.
Sprzątanie audytu działa w osobnej pętli asynchronicznej tego samego procesu, niezależnie od kolejki rang.

Po ośmiu nieudanych próbach finalizacja przechodzi do `DISCORD_RETRY_PAUSED` i zapisuje błąd
w logu operatora. Powiązanie i intencje audytu pozostają zachowane. Po naprawieniu uprawnień,
kanału lub konfiguracji użytkownik może kliknąć „Zweryfikuj konto”, aby wznowić tę samą
potwierdzoną weryfikację bez nowego logowania Riot. Usunięcie powiązania nadal anuluje tę pracę.

Publiczne Caddy zwraca 404 dla `/readyz`; sprawdzenie bazy jest dostępne bezpośrednio
na lokalnym porcie procesu RSO. `/healthz` pozostaje lekkim publicznym sprawdzeniem procesu.
Limiter RSO grupuje IPv6 po prefiksie /64, ma twardy limit 4096 wpisów i odrzuca nowe adresy
przy pełnym limicie do wygaśnięcia wpisów. Nie jest zabezpieczeniem przed rozproszonym atakiem DDoS.

Strona logowania pokazuje konto Discord i serwer zapisane przy wystawieniu linku oraz
ostrzeżenie przed linkami otrzymanymi od innych osób. To świadomie wybrana ochrona interfejsu,
nie uwierzytelnienie konta Discord w przeglądarce. Przekazany link pozostaje sekretem jednorazowym;
pełne sprawdzenie odbiorcy wymagałoby dodatkowego logowania Discord, którego nie wdrażamy.

Usuwanie powiązania najpierw zapisuje trwały stan oczekującego usunięcia. Ten stan blokuje nowe
odświeżenia i synchronizacje ról dla rekordu. Bot usuwa rolę Zweryfikowany, sprząta wiadomość
audytową i dopiero wtedy usuwa rekord. Błąd Discord jest ponawiany z bazy po restarcie, więc nie
pozostawia aktywnego linku bez możliwości dokończenia operacji.

## 10. Monitoring Riot API i kolejki rang

Bot zapisuje co 5 minut pojedynczy raport ze statusem odpowiedzi Riot i kolejki odświeżania rang:

```bash
journalctl -u moon-poro-bot.service --since today --grep='Riot monitoring:'
```

Liczniki odpowiedzi 429, 401, 403 i 5xx obejmują okres od ostatniego uruchomienia procesu. Raport
pokazuje także długość i wiek kolejki, klasy 6, 12 i 24 godziny, przewidywaną liczbę zapytań na dobę,
p50, p95 i maksymalny wiek snapshotu, zmiany tieru, resety liczników, oczekujące potwierdzenia
Unranked, retry ról Discord oraz stan breakera 401 i 403. `last_successful_riot_response_utc` pokazuje
czas ostatniej odpowiedzi 2xx. Częstotliwość raportu kontroluje
`RIOT_MONITORING_INTERVAL_SECONDS`, domyślnie 300 sekund.

## 11. Rollback

Po problemie z RSO ustaw `VERIFICATION_MODE=legacy_icon`, zatrzymaj `moon-poro-rso` i opublikuj panel
weryfikacji ikoną. Nie cofaj migracji `20260810_0002`, `20260814_0003` ani `20260815_0005`, jeżeli
istnieją już rekordy produkcyjne. Problem analizuj na kopii bazy.

Nie cofaj również migracji `0008`, `0009` i `0010` przy zmianie samego trybu weryfikacji.
Tryb `legacy_icon` kończy wyłącznie sesje z potwierdzeniem ikoną. Sesje RSO,
które już potwierdziły konto i czekają na dokończenie w Discordzie, pozostają w bazie:
ich finalizacja wróci po ponownym uruchomieniu trybu RSO. Nie rozpoczynaj dla nich
nowego powiązania ani nie kopiuj danych ręcznie. Jeśli nie planujesz powrotu do RSO,
usuń takie powiązanie zwykłą ścieżką usuwania z potwierdzeniem i dopiero potem
rozpocznij weryfikację ikoną. Sama kolejka rang nadal może synchronizować role
istniejącego powiązania; zmiana trybu nie cofa potwierdzonej wcześniej tożsamości.
