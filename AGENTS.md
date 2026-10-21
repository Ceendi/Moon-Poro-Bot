# Zasady pracy nad Moon Poro

Ten plik jest dla agentów i osób, które zmieniają kod, stronę albo teksty bota.

## Teksty (strona, bot, strony logowania Riot)

- Pisz naturalną polszczyzną, prosto i krótko, zwracając się do użytkownika na „Ty”. Humor tylko w 2-3 miejscach na całej stronie, np. Poro-Snaxy czy strona 404.
- Żadnych tekstów, które brzmią jak AI: bez sloganów, pustych obietnic i lania wody. Każde zdanie ma mówić, co się stanie albo co zrobić.
- Nigdzie nie używaj pauzy (em dash, U+2014) ani półpauzy (en dash, U+2013), także w kodzie, komentarzach i dokumentacji. Zamiast nich używaj przecinka, dwukropka, nawiasu albo zwykłego łącznika (-).
- Terminy z LoL-a i Riot, które gracze znają, są w porządku: Riot ID, PUUID, LP, Solo/Duo, boosting, ranga, region. Unikaj dziwnych anglicyzmów i żargonu z kodu, np. „orchestrator”, „widok”, „finalizacja”, „rekord w bazie”.
- Formy neutralne płciowo: „Ta osoba zaczęła…”, nie „Użytkownik rozpoczął…”.
- Przy liczbach pisz tak, żeby odmiana zawsze była poprawna, np. „Zmienione osoby: 3”, a nie „Zaktualizowano 3 użytkowników”.
- Notka prawna Riot w stopce strony zostaje po angielsku.
- Strona ma wersję angielską (`/en/`). Każdą zmianę treści wprowadzaj w obu wersjach.
- Przed otwarciem PR pokaż właścicielowi wszystkie zmienione teksty w formie „przed i po”.

## Role rang

Nazwy po angielsku, dokładnie jak na serwerze, w tej kolejności i z tymi kolorami:

| Rola | Kolor |
|---|---|
| Challenger | `#4fcae4` |
| GrandMaster | `#d65247` |
| Master | `#ba59e6` |
| Diamond | `#6279c2` |
| Emerald | `#3d905b` |
| Platinum | `#4b8f93` |
| Gold | `#b8905b` |
| Silver | `#8597a3` |
| Bronze | `#9f7361` |
| Iron | `#8b8078` |
| Unranked | `#9f9e9b` |

## Wygląd strony

Motyw to śliwkowa noc, biel i złoty księżyc, a na stronie głównej wspinaczka po rangach. Ten sam wygląd mają strona i strony logowania Riot w `moon_poro/rso_web.py`.

- Logo Moon Poro (`/moon-poro.png`) zostaje na każdej stronie.
- Kolory mają nazwy ról (`--bg`, `--text`, `--accent`...) w `site/src/styles/global.css`. W stronach i komponentach nie wpisuj gołych kolorów, tylko te zmienne.

  | Rola | Kolor | Do czego |
  |---|---|---|
  | `--bg` | `#1c1226` | tło |
  | `--surface` | `#2a1d38` | wyróżnione pola, kod |
  | `--line` | `#3d2e4f` | linie podziału |
  | `--line-strong` | `#8d7aa8` | ramka drugiego przycisku (min. 3:1 do tła) |
  | `--text` | `#f7f3fb` | tekst i nagłówki |
  | `--muted` | `#c4b6d6` | tekst drugiego planu |
  | `--quiet` | `#9d8fb0` | stopka, daty |
  | `--lilac` | `#a98bd0` | podkreślenia linków, znaczniki list, aktywna strona w menu |
  | `--accent` | `#f7f3fb` | główny przycisk (biały, tekst `--on-accent`) |
  | `--band` | `#f7f3fb` | jasny pas z najważniejszą treścią |
  | `--moon` | `#f6c04a` | księżyc, fazy księżyca i obwódka fokusu |

- Każda nowa para kolorów musi mieć kontrast min. 4,5:1 dla tekstu i 3:1 dla dużego tekstu i ramek (WCAG 2.2 AA).
- Odstępy tylko ze skali 8px (`--space-1` do `--space-8`). Od nagłówka do treści 48px, między sekcjami 96px.
- Klikalne elementy mają min. 44px wysokości (menu, stopka, przyciski). Na jednym widoku jest jeden wypełniony przycisk.

- Fonty: Big Shoulders do nagłówków, przycisków i nazw rang, Atkinson Hyperlegible Next do tekstu. Pliki są w `site/public/fonts` razem z licencjami OFL. Każdy font musi mieć polskie znaki (Fredoka nie ma ą, ę, ż). W Big Shoulders cyfra 1 wygląda jak „l”, więc numerowane nagłówki w politykach mają font tekstu.
- Najważniejszy element to drabinka rang na górze strony głównej (`site/src/components/RankLadder.astro`). Przy wejściu schodki wjeżdżają od dołu, Poro skacze po nich na szczyt i wschodzi księżyc. Po najechaniu na rangę Poro przeskakuje na jej schodek. Wszystkie schodki mają tę samą wysokość i odstęp, bo od tego zależy, gdzie ląduje Poro. Reszta strony ma być spokojna.
- Bez gradientów, poświat, cieni i kształtu pigułki. Poza drabinką ruch jest tylko w odpowiedzi na działanie (najechanie, rozwinięcie pytania).
- Zaokrąglenia zależą od roli: 4px dla rang i kodu, 8px dla przycisków i ramek.
- Kroki oznaczamy fazami księżyca (od nowiu do pełni), nie numerami w kółkach. Pytania mają sierp, gdy są zwinięte, i pełnię, gdy są rozwinięte.
- Nie kopiujemy wyglądu okien Discorda. Przyciski Discorda pokazujemy tylko jako małe etykiety, żeby było wiadomo, co kliknąć.

## Strona i polityki muszą zgadzać się z kodem

- Każde zdanie na stronie, w polityce prywatności i w warunkach sprawdzaj z kodem. Jeśli zmieniasz zapisywane dane, ciasteczka albo czas przechowywania, popraw też politykę (PL i EN).
- Stan produkcji:
  - weryfikacja tylko przez logowanie Riot (RSO); weryfikacja ikoną zostaje w kodzie jako awaryjny powrót i nie jest opisana na stronie;
  - filtr ofert boostingu i filtr Clash są wyłączone, więc nie ma ich w polityce; po ich włączeniu trzeba je dopisać;
  - logi wyjścia, wyrzucenia i bana z serwera są włączone;
  - ochrona przed multikontami jest wyłączona, ale admin może ją włączyć komendą `/multikonta`.
- Strona działa pod ścisłym CSP z Caddy (bez inline skryptów, stylów i fontów `data:`). Dlatego w `astro.config.mjs` są `inlineStylesheets: "never"` i `assetsInlineLimit: 0`.

## Git i PR

- Gałęzie nazywaj normalnie: `feat/...`, `fix/...`, bez słowa „claude”.
- Autor commitów: `Ceendi <ceendek@gmail.com>`. Nie dodawaj stopek `Co-Authored-By` ani `Claude-Session`.
- PR otwieraj dopiero, gdy właściciel zatwierdzi zmiany. Nie pushuj bezpośrednio na `master`.
- Przepisywanie historii i force-push tylko po wyraźnej zgodzie właściciela.

## Kontrole przed commitem

Te same co w CI (`.github/workflows/ci.yml`):

```bash
bash -n deploy/*.sh
uv run ruff format --check .
uv run ruff check .
uv run mypy moon_poro
uv run pytest --cov=moon_poro --cov-branch
uv run bandit -r moon_poro
uv run pylint --disable=all --enable=duplicate-code moon_poro
uv run detect-secrets-hook $(git ls-files)
uv run pip-audit --skip-editable
```

- Testy z Postgresem wymagają zmiennych `TEST_POSTGRES_*`. Bez nich są pomijane, więc uruchamiaj je z bazą.
- Wyścigi i blokady wierszy testuj na Postgresie. SQLite ignoruje `FOR UPDATE`, więc testy na nim nie wykażą takich błędów.
- Zmiany strony sprawdzaj przez `npm run build` w `site/` (wymaga zmiennych `PUBLIC_*`).

## Wdrożenie

- Wdrożenie produkcji jest tylko ręczne: `sudo /usr/local/sbin/deploy-moon-poro-prod` na VM `moon-poro-web`. Uruchamiaj je tylko na wyraźne polecenie właściciela.
- Caddy na VM pochodzi z pakietu dystrybucji (2.6), więc składnia Caddyfile musi działać na 2.6. Przed instalacją zawsze uruchom `caddy validate --adapter caddyfile`.
- Po zmianie Caddyfile użyj `systemctl restart caddy`. `reload` nie działa, bo Caddyfile ma `admin off`.
- Strony logowania Riot ładują `/fonts/*` i `/moon-poro.png` ze strony statycznej, więc nowy `site/dist` wgrywaj razem z kodem RSO.
