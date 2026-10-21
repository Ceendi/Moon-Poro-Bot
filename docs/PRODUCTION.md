# Produkcja na `moon-poro-web`

Bot, logowanie Riot (RSO), strona i baza działają na jednej VM GCP e2-micro (1 GB RAM), bez
Dockera i bez Node.js na serwerze:

```text
Internet → Caddy :443 ─┬─ statyczna strona Astro (/opt/moon-poro/site/dist)
                       └─ /verify/*, /oauth2/callback, /healthz → callback RSO 127.0.0.1:8080

Discord Gateway → bot discord.py ─┐
callback RSO (aiohttp) ───────────┴→ PostgreSQL 18, 127.0.0.1:5433
```

Bot i callback działają na osobnych kontach systemowych. Tylko `moon-poro-rso` czyta dane klienta
RSO, a tylko `moon-poro-prod` czyta token Discord i klucz Riot API. Wspólne są kod tylko do odczytu
i baza, każde z osobnym loginem.

## Układ na VM

| Część | Gdzie |
|---|---|
| Kod | czysty checkout Git w `/opt/moon-poro-prod` |
| Bot | `moon-poro-prod.service`, konto `moon-poro-prod`, ustawienia w `/etc/moon-poro/prod-bot.env` |
| Callback RSO | `moon-poro-prod-rso.service`, konto `moon-poro-rso`, ustawienia w `/etc/moon-poro/prod-rso.env` |
| Baza | klaster `18/moon-poro-prod` na porcie 5433, `postgresql-moon-poro-prod.service`, baza `moon_poro_prod` |
| Strona | `/opt/moon-poro/site/dist`, serwowana przez Caddy (`deploy/Caddyfile`) |
| Backupy | `/var/backups/moon-poro-prod/{daily,pre-deploy}` i bucket Cloud Storage |

Jednostka bota, obie jednostki backupu i test odtworzenia wymagają jednostki klastra. Pliki env mają tryb `0600`
i należą do kont, które z nich korzystają.

## Wdrażanie zmian

Po zielonym CI i scaleniu PR do `master`:

```bash
sudo /usr/local/sbin/deploy-moon-poro-prod
```

Skrypt odmawia przy lokalnych zmianach i aktualizacji innej niż fast-forward. Najpierw pokazuje
commity do wdrożenia i pyta o potwierdzenie (`y`). Sprawdź, czy znasz każdy z nich: wszystko z
`master` działa potem na VM. Bez terminala, np. przez `gcloud compute ssh --command`, potwierdź
flagą `--yes`. Przed zmianą kodu robi zweryfikowany backup, potem zatrzymuje callback, uruchamia
migracje na koncie `moon-poro-prod` (nie jako root), startuje bota, a na końcu callback. Zapisuje
ostatni uruchomiony commit i bezpiecznie wznawia przerwane wdrożenie.

Gdy zmienią się pliki w `deploy/`, zainstaluj pomocników jeszcze raz:

```bash
sudo /opt/moon-poro-prod/deploy/install-prod-automation.sh
```

Instalator nie restartuje bota. Jeśli zmieniła się jednostka `deploy/moon-poro-prod.service`,
wypisze polecenie restartu, a nowe ustawienia zadziałają po restarcie bota.

**Strona** nie jest częścią tego skryptu. Zbuduj ją na innym komputerze (w `site/`: `npm ci` i
`npm run build` z wszystkimi wartościami `PUBLIC_*`) i podmień `/opt/moon-poro/site/dist`.
Strony logowania Riot ładują z niej `/fonts/*` i `/moon-poro.png`, więc nowy build wgrywaj razem
ze zmianami w kodzie RSO.

**Caddy** pochodzi z oficjalnego repozytorium Caddy (`/etc/apt/sources.list.d/caddy-stable.list`),
bo Debian 12 ma Caddy 2.6 z ograniczonym wsparciem bezpieczeństwa. Przed podmianą
`/etc/caddy/Caddyfile` uruchom `sudo caddy validate --config <plik> --adapter caddyfile`, a potem
`sudo systemctl restart caddy`. `reload` nie działa, bo Caddyfile ma `admin off`.

## Aktualizacje systemu

`unattended-upgrades` codziennie instaluje poprawki Debiana, PostgreSQL z PGDG, agentów Google i
Caddy (`/etc/apt/apt.conf.d/52moon-poro-unattended-upgrades`). Nowa główna wersja PostgreSQL nie
przyjdzie sama, bo pakiety `postgresql`, `postgresql-client` i `postgresql-contrib` są wstrzymane.
Wstrzymany jest też `google-cloud-cli`: nic na VM go nie używa, a jego aktualizacja tak obciąża
e2-micro, że bot traci na chwilę połączenie z bazą. Aktualizacja PostgreSQL restartuje bazę, a bot
i callback po chwili łączą się z nią ponownie.

Gdy aktualizacja wymaga restartu (np. nowe jądro), VM restartuje się o 04:45 UTC, po codziennym
backupie. Bot, callback i Caddy startują same. Ostatnie aktualizacje:
`tail /var/log/unattended-upgrades/unattended-upgrades.log`.

## Kopie zapasowe

Backup to `pg_dump` bazy `moon_poro_prod` w formacie custom, sprawdzony przez `pg_restore --list`,
z plikiem `.sha256` obok. `moon-poro-prod-backup.timer` robi go codziennie o 04:15 UTC, a każde
wdrożenie najpierw robi własny. Na VM oba rodzaje zostają 90 dni; stare są usuwane dopiero po
udanym nowym backupie. Jednostki backupu nie mają dostępu do sieci.

### Kopie poza VM

Po każdym udanym backupie `moon-poro-prod-offsite-backup.service` wysyła do bucketu dumpy z
ostatnich 7 dni, których jeszcze tam nie ma, razem z plikami `.sha256`. Przed wysłaniem sprawdza
SHA-256, nigdy nie nadpisuje obiektu (`ifGenerationMatch=0`) i porównuje MD5 podane przez Cloud
Storage z lokalnym plikiem. Błąd wysyłki oznacza jako nieudaną tylko tę jednostkę; lokalny backup
i wdrożenia działają dalej.

Jedyne dane, których nie da się odtworzyć, to baza. Kod, strona, jednostki i system pochodzą z
repozytorium. Sekrety z `/etc/moon-poro/*.env` trzymaj w menedżerze haseł, nie w buckecie.

Jednorazowa konfiguracja (mieści się w darmowym limicie Cloud Storage w `us-east1`):

1. Konto usługi dla VM z jedną rolą w buckecie: `roles/storage.objectCreator`. VM może tylko
   dodawać pliki, nie może ich czytać ani usuwać.
2. Reguła cyklu życia: usuwanie obiektów starszych niż 83 dni. Razem z 7 dniami soft delete kopie
   znikają w ciągu 90 dni, tak jak mówi polityka prywatności.
3. Podpięcie konta do zatrzymanej VM z zakresem `storage-rw`.
4. Plik `/etc/moon-poro/prod-backup-offsite.env` (`root:root`, `0644`) z
   `BACKUP_GCS_BUCKET=<nazwa bucketu>`, ponowna instalacja pomocników i próba:
   `sudo systemctl start moon-poro-prod-offsite-backup.service`.

**Odtworzenie:** pobierz dump i jego `.sha256`, uruchom `sha256sum --check`, potem `pg_restore`
do pustej bazy, zanim wystartuje bot.

### Test odtworzenia

`moon-poro-prod-restore-test.timer` 1. dnia każdego miesiąca o 05:30 UTC uruchamia
`moon-poro-prod-restore-test.service`. Test bierze najnowszy dump z `daily`, który nie może być
starszy niż 2 dni, i sprawdza jego `.sha256`. Potem odtwarza go do tymczasowej bazy
`moon_poro_restore_test` na tym samym klastrze i porównuje liczbę wierszy każdej tabeli z dumpem
oraz wersję Alembic. Tabele `verification_links`, `warnings`, `moderation_stats` i
`guild_features` muszą być w dumpie, a `verification_links` nie może być pusta. Tymczasowa baza
jest usuwana zawsze, także po błędzie.

Test nigdy nie łączy się z `moon_poro_prod`, nie ma dostępu do sieci, katalog backupów widzi tylko
do odczytu i ma limit 128 MB. Po stronie serwera ogranicza `maintenance_work_mem` do 16 MB.
Ręczny przebieg:

```bash
sudo systemctl start moon-poro-prod-restore-test.service
journalctl -u moon-poro-prod-restore-test.service -n 20
```

### Powiadomienia o błędach

Gdy codzienny backup, backup przed wdrożeniem, kopia w Cloud Storage albo test odtworzenia skończy
się błędem, `OnFailure=` uruchamia `moon-poro-prod-alert@<jednostka>.service`. To samo dzieje się,
gdy bot albo callback RSO padnie kilka razy z rzędu i systemd przestanie go uruchamiać (bot: 3 razy,
callback: 5 razy w ciągu 5 minut). Zatrzymanie przy wdrożeniu albo restarcie VM nie jest błędem i
nie wysyła alertu. Ta jednostka
wysyła przez webhook Discorda krótką wiadomość: co się nie udało, nazwę jednostki, czas i
polecenie `journalctl`. Nie wysyła logów. Oznacza tylko osobę podaną w
`DISCORD_ALERT_MENTION_USER_ID`, nigdy `@everyone` ani roli; bez tej linii nikogo nie oznacza.
Dopóki nie ma pliku env, alert jest pomijany.

Jednorazowa konfiguracja:

1. Na prywatnym kanale, który widzisz tylko Ty (albo na kanale moderacji): Ustawienia kanału,
   Integracje, Webhooki, nowy webhook. Skopiuj jego adres.
2. Plik `/etc/moon-poro/prod-alert.env` (`root:root`, `0600`) z linią
   `DISCORD_ALERT_WEBHOOK_URL=<adres webhooka>`. Adres trzymaj też w menedżerze haseł; każdy, kto
   go zna, może pisać na tym kanale.
3. Żeby dostawać powiadomienie, dodaj linię `DISCORD_ALERT_MENTION_USER_ID=<Twoje ID>`. ID
   skopiujesz w Discordzie po włączeniu trybu dewelopera (Ustawienia, Zaawansowane): prawy klik na
   swój profil, „Kopiuj ID użytkownika”.
4. Próba: `sudo systemctl start moon-poro-prod-alert@test.service`. Na kanale pojawi się
   testowe powiadomienie.

## Kontrola działania

```bash
systemctl status moon-poro-prod moon-poro-prod-rso caddy postgresql-moon-poro-prod
curl --fail https://moonporo.pl/healthz
curl --fail http://127.0.0.1:8080/readyz
journalctl -u moon-poro-prod -u moon-poro-prod-rso --since today
```

Publicznie `/readyz` zwraca 404; sprawdzenie bazy jest dostępne tylko lokalnie.

Po zmianach w weryfikacji sprawdź na prywatnym kanale: sukces, odmowę w Riot, link po 10 minutach,
ponowne użycie linku, dwa konta Discord dla jednego PUUID, ponowne kliknięcie, wyjście z serwera w
trakcie, brak DM, chwilowy błąd Riot lub Discorda, `/usun_weryfikacje` i ponowne wejście na serwer.

### Monitoring Riot API

Co 5 minut bot zapisuje raport o odpowiedziach Riot i kolejce rang:

```bash
journalctl -u moon-poro-prod --since today --grep='Riot monitoring:'
```

Liczniki 429, 401, 403 i 5xx liczą się od startu procesu. Raport pokazuje też długość i wiek
kolejki, klasy 6, 12 i 24 godziny, przewidywaną liczbę zapytań na dobę, wiek snapshotów rang,
oczekujące potwierdzenia Unranked, ponowienia ról Discord, stan blokad 401 i 403 oraz czas ostatniej
odpowiedzi 2xx (`last_successful_riot_response_utc`). Częstotliwość ustawia
`RIOT_MONITORING_INTERVAL_SECONDS`.

### Pamięć

| Proces | Zwykle | Limit systemd |
|---|---:|---:|
| bot | ok. 190 MB | 400 MB (`MemoryHigh` 320 MB) |
| callback RSO | ok. 70 MB | 128 MB (`MemoryHigh` 96 MB) |
| PostgreSQL | 120-220 MB | ustawienia klastra |
| Caddy i system | 80-160 MB | brak |

`bootstrap-web.sh` zakłada 1 GB swapu jako zabezpieczenie przed OOM. Pula połączeń bota to
najwyżej 10 (domyślnie 5 + 5), callbacku 2.

## Awaryjny powrót do weryfikacji ikoną

Gdy RSO nie działa, ustaw `VERIFICATION_MODE=legacy_icon` w `prod-bot.env`, zatrzymaj
`moon-poro-prod-rso`, zrestartuj bota i opublikuj panel `/weryfikacja`. Nie cofaj żadnych migracji
przy zmianie trybu.

Tryb `legacy_icon` kończy tylko sesje potwierdzone ikoną. Sesje RSO, które potwierdziły już konto
i czekają na dokończenie w Discordzie, zostają w bazie i dokończą się po powrocie do RSO. Nie twórz
dla nich nowego powiązania ani nie kopiuj danych ręcznie. Jeśli nie planujesz powrotu do RSO, usuń
takie powiązanie zwykłą ścieżką usuwania i dopiero potem zacznij weryfikację ikoną. Kolejka rang
dalej synchronizuje role istniejących powiązań.

## Odbudowa VM od zera

Potrzebne: Debian 12, domena z rekordem `A` na stały adres IP, firewall GCP z dostępem z Internetu
tylko do TCP 80 i 443 oraz SSH tylko przez IAP. Porty 5433 i 8080 nie mogą być publiczne.

1. `sudo deploy/bootstrap-web.sh <site-dist> <Caddyfile>`: pakiety, Caddy z oficjalnego
   repozytorium, swap, SSH, nftables i automatyczne aktualizacje.
2. PostgreSQL 18 z repozytorium PGDG i klaster `18/moon-poro-prod` na porcie 5433
   (`pg_createcluster --port 5433 18 moon-poro-prod`). Login bota (`moon_poro_prod`) musi być
   właścicielem schematu, bo bot uruchamia migracje przy starcie.
3. Konta systemowe bez logowania: `moon-poro-prod` i `moon-poro-rso`
   (`useradd --system --no-create-home --shell /usr/sbin/nologin <konto>`).
4. Checkout repozytorium w `/opt/moon-poro-prod` (`root:root`), pliki
   `/etc/moon-poro/prod-bot.env` z `.env.example` i `/etc/moon-poro/prod-rso.env` z
   `.env.rso.example`, oba z `POSTGRES_PORT=5433` i `POSTGRES_DB=moon_poro_prod`.
5. Baza z kopii (sekcja „Kopie zapasowe”) albo pusta. Migracje bez łączenia z Discordem:
   `sudo -u moon-poro-prod /opt/moon-poro-prod/.venv/bin/python -m moon_poro.migrate --env-file /etc/moon-poro/prod-bot.env`.
   Nie używaj samego `alembic upgrade head`, bo migracje potrzebują ustawień bota.
6. Login callbacku z minimalnymi uprawnieniami. Hasło ustaw przez `\password moon_poro_rso`
   w `psql -p 5433`, nie w SQL:

   ```sql
   CREATE ROLE moon_poro_rso LOGIN;
   GRANT CONNECT ON DATABASE moon_poro_prod TO moon_poro_rso;
   GRANT USAGE ON SCHEMA public TO moon_poro_rso;
   GRANT SELECT, INSERT, UPDATE, DELETE
     ON TABLE verification_links, verification_sessions TO moon_poro_rso;
   GRANT USAGE, SELECT ON SEQUENCE verification_sessions_id_seq TO moon_poro_rso;
   ```

7. `sudo /opt/moon-poro-prod/deploy/install-prod-automation.sh`, potem
   `sudo /usr/local/sbin/deploy-moon-poro-prod`. Instalator kopiuje jednostki z `deploy/`, w tym
   bota (`moon-poro-prod.service`), i włącza jego start przy uruchomieniu VM. Bota uruchamia
   dopiero wdrożenie.
8. Sprawdź import jako callback:
   `sudo -u moon-poro-rso /opt/moon-poro-prod/.venv/bin/python -c 'import moon_poro.rso_web'`,
   potem `sudo systemctl enable --now moon-poro-prod-rso.service` i
   `curl --fail http://127.0.0.1:8080/readyz`.
9. Kopie poza VM według sekcji „Kopie poza VM”, potem powiadomienia według sekcji „Powiadomienia
   o błędach”.
10. Na koniec `VERIFICATION_MODE=rso` i `RSO_PUBLIC_BASE_URL` w `prod-bot.env`, restart bota i nowy
    panel `/weryfikacja`. Na kanale audytu bot potrzebuje uprawnień: wyświetlanie kanału, wysyłanie
    wiadomości, zamieszczanie linków i czytanie historii wiadomości.
