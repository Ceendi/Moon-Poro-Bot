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
> Ten branch usuwa stary panel weryfikacji ikoną. Przed otrzymaniem danych klienta RSO nie
> uruchamiaj go jako produkcyjnego bota. Podczas konsolidacji VM zachowaj działającą wersję
> legacy w oddzielnym katalogu i usłudze; `moon-poro-rso.service` pozostaw wyłączone.

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
CREATE ROLE moon_poro_rso LOGIN PASSWORD 'WYGENEROWANE-DLUGIE-HASLO';
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
2. Podczas migracji VM przenieś działającą wersję legacy do oddzielnego katalogu i potwierdź
   działanie starej weryfikacji. Nie uruchamiaj jeszcze kodu RSO.
3. Po otrzymaniu danych klienta wdróż kod RSO oraz zbudowany poza VM katalog `site/dist`.
4. Zatrzymaj legacy bota na czas finalnego przełączenia i uruchom migracje Alembic kontem
   właściciela schematu.
5. Uruchom nowego bota i nadaj minimalne granty loginowi `moon_poro_rso`.
6. Wpisz client assertion albo client secret wyłącznie do `/etc/moon-poro/rso.env`.
7. Sprawdź Caddy, uruchom callback i testuj pełny przepływ na prywatnym kanale.
8. Jeżeli Riot zażąda weryfikacji domeny, umieść dokładny tekst w `site/dist/riot.txt` i usuń
   plik po zakończeniu.
9. Dopiero po udanych testach opublikuj nowy panel `/weryfikacja` i usuń panel z ikonką.
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

## 9. Rollback

Kod starego panelu ikonkowego został celowo usunięty; nie przywracaj go po publicznym przejściu na
RSO bez ponownej oceny bezpieczeństwa. Techniczny rollback aplikacji może użyć poprzedniego wydania,
ale migracji `20260810_0002` nie cofaj, jeżeli istnieją już rekordy RSO. Wyłącz nowy panel, zatrzymaj
`moon-poro-rso`, przywróć poprzednią usługę bota i zbadaj problem na kopii bazy.
