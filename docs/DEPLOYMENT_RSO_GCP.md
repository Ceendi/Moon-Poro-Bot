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
> Repozytorium zawiera tymczasową weryfikację ikoną oraz docelowe RSO. Do czasu otrzymania danych
> klienta Riot używaj `VERIFICATION_MODE=legacy_icon` i pozostaw `moon-poro-rso.service` wyłączone.

## 1. Warunki wstępne

- wspierany system Linux i Python 3.12 lub 3.13;
- wspierany PostgreSQL (nie wdrażaj publicznego RSO na niewspieranym PostgreSQL 11);
- domena, której jesteś właścicielem, rekord `A` do stałego zewnętrznego IP VM;
- firewall GCP: Internet ma dostęp tylko do TCP 80/443; SSH ograniczony do zaufanych adresów;
- porty 5432 i 8080 nie są publiczne;
- snapshot i zweryfikowany backup bazy przed migracją.

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

Bot nadal wykonuje migracje Alembic przy starcie, więc jego login musi być właścicielem schematu
albo deployment musi uruchomić `alembic upgrade head` oddzielnym kontem przed restartem.

## 5. Python i usługi

W `/opt/moon-poro`:

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install uv==0.12.3
.venv/bin/uv sync --frozen --no-dev
```

Skopiuj jednostki z `deploy/` do `/etc/systemd/system/`, a `deploy/Caddyfile` do `/etc/caddy/`.
Plik jest już skonfigurowany dla `moonporo.pl`; nie używa zmiennej z przykładową domeną.
Po przyznaniu danych klienta sprawdź konfigurację przed przeładowaniem:

```bash
caddy validate --config /etc/caddy/Caddyfile
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
4. Zatrzymaj legacy bota na czas finalnego przełączenia i uruchom migracje Alembic kontem
   właściciela schematu.
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
curl --fail https://moonporo.pl/readyz
journalctl -u moon-poro-rso --since today
```

Sprawdź przypadki: sukces, odmowa w Riot, link po 10 minutach, ponowne użycie, dwa konta Discord dla
jednego PUUID, ponowne kliknięcie, wyjście z serwera w trakcie, brak DM, chwilowy błąd Riot/Discord,
`/usun_weryfikacje` oraz ponowne wejście na serwer.

## 8. Budżet pamięci e2-micro

Orientacyjny cel, nie gwarantowany limit:

| Proces | Typowe dążenie | Limit systemd |
|---|---:|---:|
| bot Python | 150–280 MB | 400 MB |
| RSO aiohttp | 45–85 MB | 128 MB |
| PostgreSQL | 120–220 MB | konfiguracja DB |
| Caddy + system | 80–160 MB | system |

Dla PostgreSQL zacznij od `shared_buffers=64MB`, `work_mem=2MB`,
`maintenance_work_mem=32MB`, `max_connections=20` i monitoruj, zamiast ślepo zwiększać cache. Utrzymuj
pulę bota na maksymalnie 5 połączeń oraz callbacku na 2. Skonfiguruj 1–2 GB swap jako zabezpieczenie
przed OOM (nie jako zamiennik RAM), alert dla pamięci >85%, dysku >80% i restartów usług.

Static Astro nie zużywa pamięci aplikacyjnej. Caddy kompresuje pliki raz przy odpowiedzi, a strona
nie ładuje fontów, bibliotek JS, analityki ani zasobów zewnętrznych.

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
z jitterem 20%. Regionalny breaker nadal respektuje `Retry-After` po 429. Po 401 lub 403 globalny
breaker zatrzymuje masową kolejkę i dopuszcza pojedynczą próbę kontrolną co 15 minut.

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
