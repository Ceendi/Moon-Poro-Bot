# Riot Sign On — pakiet zgłoszeniowy Moon Poro Bot

Stan na 11 sierpnia 2026. Aplikacja produkcyjna widoczna na przekazanym zrzucie ma status
`Approved` i App ID `524635`. Riot przyznaje klienta RSO dopiero istniejącej, zatwierdzonej
aplikacji produkcyjnej. Dla starszej aplikacji, która nie otrzymała zaproszenia, oficjalna
instrukcja wskazuje kontakt z Developer Relations przez oficjalny serwis wsparcia.

Wartość `{{VERIFICATION_CHANNEL_NAME}}` uzupełnij dopiero w prywatnej instrukcji dla recenzenta,
jeżeli Riot o nią poprosi. Nie wysyłaj sekretów, klucza API, prywatnego klucza ani client assertion.

## Co musi być publiczne

- `https://moonporo.pl` — działająca strona na własnej domenie, nie repozytorium GitHub;
- `https://moonporo.pl/en/rso/` — angielski opis pełnego przepływu;
- `https://moonporo.pl/en/privacy/` i `https://moonporo.pl/en/terms/`;
- `https://moonporo.pl/moon-poro.png` — stabilny adres logo klienta;
- `https://moonporo.pl/riot.txt` — tylko na czas weryfikacji własności domeny, z dokładnym tekstem
  przydzielonym przez Riot;
- dostęp recenzencki do serwera Discord i instrukcja uruchomienia przycisku weryfikacji.

Oficjalne wymagania: [RSO FAQ](https://developer.riotgames.com/docs/faqs),
[League RSO](https://developer.riotgames.com/docs/lol),
[wymaganie strony dla bota Discord](https://support-developer.riotgames.com/hc/en-us/articles/22801383038867-Production-Key-Applications),
[weryfikacja `riot.txt`](https://developer.riotgames.com/how-to-verify-site.html) oraz
[General Policies](https://developer.riotgames.com/policies/general).

## Kopia wiadomości wysłanej do Developer Relations

```text
Hello Riot Developer Relations,
This is a follow-up to our Riot Sign On request from February 2022 for Application ID 524635.
Riot Lizz replied that an email with questions had been sent, but we did not complete that
process at the time. The product has now been rebuilt, documented and deployed on its own HTTPS
domain.


We operate Moon Poro Bot, an approved production application for League of Legends
(Application ID 524635). We would like to request a Riot Sign On client for this existing
application.

Moon Poro is a free Discord bot used by an established Polish League of Legends community.
Its purpose is to let a Discord member prove control of their own Riot account, prevent rank
impersonation in community events and LFG, and synchronise their official Solo/Duo rank and
platform roles. The current profile-icon verification will be fully replaced by RSO.

Public product URL: https://moonporo.pl
English product flow: https://moonporo.pl/en/rso/
Privacy policy: https://moonporo.pl/en/privacy/
Terms of service: https://moonporo.pl/en/terms/
Logo URI: https://moonporo.pl/moon-poro.png
Support contact: kontakt@moonporo.pl
Discord reviewer invite: https://discord.com/invite/lolpl

Requested redirect URI (exact match):
https://moonporo.pl/oauth2/callback

Requested scopes: openid cpid
We intentionally do not request offline_access and never store access, ID or refresh tokens.
After the authorization-code exchange, the isolated callback service calls /userinfo for cpid
and /riot/account/v1/accounts/me for PUUID and Riot ID. The access token exists only in process
memory for those requests. The Discord bot process cannot read the RSO client credential.

User flow:
1. A guild member clicks “Verify through Riot” in Discord.
2. The bot returns an ephemeral, one-time link valid for ten minutes.
3. The user deliberately continues to auth.riotgames.com and grants access.
4. Riot returns to our HTTPS callback; we validate and consume OAuth state.
5. We reserve a unique PUUID-to-Discord link and discard all RSO tokens.
6. The bot assigns verified, platform and official Solo/Duo rank roles and privately confirms
   completion to the user.
7. The user can remove the link and managed roles at any time with /usun_weryfikacje.

We store the Discord user and guild IDs, Riot PUUID, platform, verification method and timestamp.
During the short-lived verification session, we also temporarily store the Riot ID needed to
display and complete the confirmation. We maintain a limited audit log of authorized
administrator lookups. We do not run advertising or analytics and do not sell player data.
Verification-session metadata is deleted after seven days, and administrator lookup audit
records after 90 days.

Preferred client authentication: private_key_jwt. The implementation also supports Client Secret
Basic if Riot assigns that method to this client.

The public website, policies and redirect URI page are deployed. The pre-credential implementation
is complete and has been tested locally, but the end-to-end RSO authorization flow cannot be
enabled until Riot issues the client configuration. Once received, we will deploy the isolated
callback service, test the full flow in a private Discord channel, and then replace the production
profile-icon verification.

Please let us know if you need an additional locale, redirect URI, security detail, or live review
session. Thank you.
```

## Wartości do formularza RSO

| Pole | Wartość |
|---|---|
| Existing production App ID | `524635` |
| Client name | `Moon Poro Bot` |
| Game | `League of Legends` |
| Product URL | `https://moonporo.pl` |
| Redirect URI | `https://moonporo.pl/oauth2/callback` |
| Logo URI | `https://moonporo.pl/moon-poro.png` |
| Privacy URI | `https://moonporo.pl/en/privacy/` |
| Terms URI | `https://moonporo.pl/en/terms/` |
| Locales | `pl_PL`, `en_GB` |
| Scopes | `openid cpid` |
| Client authentication | `private_key_jwt` (preferowane) |
| Account endpoint | regionalny `/riot/account/v1/accounts/me` wybierany na podstawie `cpid` |
| UserInfo endpoint | `https://auth.riotgames.com/userinfo` |

## Nowy Product Description do Developer Portal

```text
Moon Poro Bot is a free Discord utility for an established Polish League of Legends community.
It uses Riot Sign On to let a Discord member explicitly link an account they control. The bot
stores the Riot PUUID, League platform and Discord user ID, then uses the official League API to
synchronise the member's Solo/Duo rank and region roles. This helps prevent rank impersonation in
LFG and community events. Users can remove their link and managed roles at any time. The product
does not display private match history, estimate MMR, provide gameplay advice, run advertising or
sell player data. RSO tokens are never persisted; administrative account-link lookups require a
stated moderation/support reason and are logged for audit.

APIs: RSO /authorize, /token and /userinfo; account-v1 /accounts/me and /accounts/by-puuid;
summoner-v4 /summoners/by-puuid; league-v4 /entries/by-puuid.
```

Po wdrożeniu zmień `Product URL` ze starego linku GitHub na publiczną domenę. Riot wprost podaje,
że repozytorium i kod źródłowy nie zastępują działającej strony produktu.

## Instrukcja dla recenzenta

Wyślij ją prywatnie razem z zaproszeniem, nie publikuj kanału testowego na stronie:

```text
1. Join https://discord.com/invite/lolpl.
2. Open channel {{VERIFICATION_CHANNEL_NAME}}.
3. Click “Zweryfikuj przez Riot” under the Moon Poro verification panel.
4. Only you can see the generated message and one-time link.
5. Open the link, review the data explanation, and click “Continue to Riot Sign On”.
6. Complete Riot authorization. The browser shows callback progress and a return-to-Discord link.
7. Moon Poro assigns Verified, platform, and Solo/Duo rank roles and sends a DM confirmation.
8. Run /usun_weryfikacje to verify self-service deletion.
```

## Checklista przed kliknięciem Submit

- [ ] własna domena wskazuje na GCP i ma poprawny HTTPS;
- [ ] operator, lokalizacja, e-mail i link Discord są prawdziwe na obu wersjach językowych;
- [ ] publiczna strona działa na telefonie i bez logowania;
- [ ] polityka, warunki, disclaimer Riot i opis RSO są dostępne ze stopki;
- [ ] `riot.txt` zawiera dokładny ciąg Riot bez BOM, spacji i dodatkowej linii;
- [ ] Product URL w Developer Portal nie wskazuje już na GitHub;
- [ ] redirect URI jest identyczny znak po znaku (HTTPS, host, ścieżka, brak dodatkowego slash);
- [ ] reviewer invite nie wygasa i prowadzi do przygotowanego kanału;
- [ ] bot ma uprawnienia do zarządzania rolami, a jego rola znajduje się nad rolami zarządzanymi;
- [ ] zademonstrowano powodzenie, anulowanie, wygaśnięcie linku, duplikat Riot i samodzielne usunięcie;
- [ ] sekrety znajdują się tylko w `/etc/moon-poro/*.env` z prawami `0600`;
- [ ] backup bazy i test odtworzenia wykonano przed migracją.

Akceptacji nie da się zagwarantować — decyzja należy do Riot, a polityki mogą się zmienić. Ten
pakiet pokrywa wymagania opublikowane przez Riot w dniu wskazanym na początku dokumentu.
