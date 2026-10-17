from __future__ import annotations

import asyncio
import contextlib
import html
import ipaddress
import json
import logging
import re
import secrets
import time
from collections import OrderedDict
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode, urlsplit

import aiohttp
from aiohttp import web
from sqlalchemy import text

from moon_poro.database import Database
from moon_poro.models import VerificationSession, VerificationSessionStatus
from moon_poro.settings import RSOSettings
from moon_poro.verification_sessions import (
    LinkReservationResult,
    SessionAlreadyUsed,
    SessionBrowserMismatch,
    SessionExpired,
    SessionNotFound,
    VerificationSessionRepository,
    as_utc,
    browser_matches,
)

logger = logging.getLogger("moon_poro.rso")

TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_-]{40,80}$")
STATE_PATTERN = re.compile(r"^[A-Za-z0-9_-]{40,200}$")
MAX_RIOT_RESPONSE_BYTES = 64 * 1024
MAX_RIOT_ERROR_BYTES = 4096
SESSION_COOKIE = "moon_poro_rso"
NEW_LINK_HINT = "Kliknij „Zweryfikuj konto” na Discordzie, a bot wyśle nowy link."
# The browser identity cookie. Over HTTPS it carries the __Host- prefix: the
# browser then accepts it only with Secure, Path=/ and no Domain, so a sibling
# subdomain cannot plant ("toss") an identity of its own. The prefix requires
# Secure, which plain HTTP local development cannot set, so local development
# keeps the unprefixed name. Read it through _browser_cookie_name().
SECURE_BROWSER_COOKIE = "__Host-moon_poro_rso_browser"
BROWSER_COOKIE = "moon_poro_rso_browser"
RATE_LIMIT_REFRESH_SECONDS = 10
# After this long the bot is still expected to finish, but fast polling is pointless.
DISCORD_PENDING_SLOW_AFTER = timedelta(minutes=3)
# Runs of URL-safe characters this long may be tokens or codes; never log them.
_SECRET_LIKE = re.compile(r"[A-Za-z0-9._~+/=-]{20,}")
_BACKGROUND_TASKS: set[asyncio.Task[None]] = set()
# Shutdown waits this long for pending session failure updates.
BACKGROUND_DRAIN_TIMEOUT_SECONDS = 5.0
INTERNAL_ERROR_COPY = (
    "Coś poszło nie tak",
    "Spróbuj ponownie za chwilę. Jeśli problem się powtarza, kliknij "
    "„Zweryfikuj konto” na Discordzie, a bot wyśle nowy link.",
)
ACCOUNT_CLUSTER_BY_PLATFORM = {
    "EUN1": "europe",
    "EUW1": "europe",
    "NA1": "americas",
}

SETTINGS_KEY = web.AppKey("settings", RSOSettings)
DATABASE_KEY = web.AppKey("database", Database)
REPOSITORY_KEY = web.AppKey("repository", VerificationSessionRepository)
HTTP_CLIENT_KEY = web.AppKey("http_client", aiohttp.ClientSession)


class RiotRSOError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class RiotIdentity:
    puuid: str
    game_name: str
    tag_line: str
    platform: str


class RiotRSOClient:
    def __init__(self, settings: RSOSettings, session: aiohttp.ClientSession) -> None:
        self._settings = settings
        self._session = session

    def authorization_url(self, state: str) -> str:
        query = urlencode(
            {
                "client_id": self._settings.rso_client_id,
                "redirect_uri": self._settings.rso_callback_url,
                "response_type": "code",
                "scope": self._settings.rso_scope,
                "state": state,
                "ui_locales": "pl-PL en-GB",
            }
        )
        return f"https://auth.riotgames.com/authorize?{query}"

    async def exchange_code(self, code: str) -> str:
        form = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self._settings.rso_callback_url,
        }
        auth: aiohttp.BasicAuth | None = None
        if self._settings.rso_client_auth_method == "private_key_jwt":
            assertion = self._settings.rso_client_assertion
            if assertion is None:  # guarded by settings validation
                raise RiotRSOError("RSO_CONFIGURATION_ERROR")
            form.update(
                {
                    "client_assertion_type": (
                        "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
                    ),
                    "client_assertion": assertion.get_secret_value(),
                }
            )
        else:
            client_secret = self._settings.rso_client_secret
            if client_secret is None:  # guarded by settings validation
                raise RiotRSOError("RSO_CONFIGURATION_ERROR")
            auth = aiohttp.BasicAuth(
                self._settings.rso_client_id,
                client_secret.get_secret_value(),
            )

        try:
            async with self._session.post(
                "https://auth.riotgames.com/token",
                data=form,
                auth=auth,
                allow_redirects=False,
            ) as response:
                payload = await _read_riot_json(response, expected_status=200, endpoint="token")
        except (aiohttp.ClientError, TimeoutError) as error:
            raise RiotRSOError("RIOT_AUTH_UNAVAILABLE") from error

        access_token = payload.get("access_token")
        scheme = str(payload.get("token_type", "")).casefold()
        if not isinstance(access_token, str) or not access_token or scheme != "bearer":
            raise RiotRSOError("INVALID_TOKEN_RESPONSE")
        return access_token

    async def get_identity(self, access_token: str) -> RiotIdentity:
        headers = {"Authorization": f"Bearer {access_token}", "Accept": "application/json"}
        try:
            userinfo = await self._get_json(
                "https://auth.riotgames.com/userinfo", headers, endpoint="userinfo"
            )
        except (aiohttp.ClientError, TimeoutError) as error:
            raise RiotRSOError("RIOT_IDENTITY_UNAVAILABLE") from error

        platform = str(userinfo.get("cpid", "")).strip().upper()
        if platform not in self._settings.rso_allowed_platforms:
            raise RiotRSOError("UNSUPPORTED_PLATFORM" if platform else "MISSING_PLATFORM")

        account_url = (
            f"https://{ACCOUNT_CLUSTER_BY_PLATFORM[platform]}.api.riotgames.com/"
            "riot/account/v1/accounts/me"
        )
        try:
            account = await self._get_json(account_url, headers, endpoint="account")
        except (aiohttp.ClientError, TimeoutError) as error:
            raise RiotRSOError("RIOT_IDENTITY_UNAVAILABLE") from error

        puuid = account.get("puuid")
        game_name = account.get("gameName")
        tag_line = account.get("tagLine")
        if (
            not isinstance(puuid, str)
            or not puuid
            or not isinstance(game_name, str)
            or not game_name
            or not isinstance(tag_line, str)
            or not tag_line
        ):
            raise RiotRSOError("INVALID_ACCOUNT_RESPONSE")
        return RiotIdentity(
            puuid=puuid,
            game_name=game_name,
            tag_line=tag_line,
            platform=platform,
        )

    async def _get_json(
        self, url: str, headers: dict[str, str], *, endpoint: str
    ) -> dict[str, Any]:
        async with self._session.get(url, headers=headers, allow_redirects=False) as response:
            return await _read_riot_json(response, expected_status=200, endpoint=endpoint)


RSO_CLIENT_KEY = web.AppKey("rso_client", RiotRSOClient)


async def _read_riot_json(
    response: aiohttp.ClientResponse, *, expected_status: int, endpoint: str
) -> dict[str, Any]:
    if response.status != expected_status:
        error, description = await _riot_error_details(response)
        logger.warning(
            "RSO %s endpoint returned HTTP %s (error=%s, error_description=%s)",
            endpoint,
            response.status,
            error or "-",
            description or "-",
        )
        raise RiotRSOError("RIOT_UPSTREAM_REJECTED")
    body = bytearray()
    while True:
        chunk = await response.content.read(min(8192, MAX_RIOT_RESPONSE_BYTES + 1 - len(body)))
        if not chunk:
            break
        body.extend(chunk)
        if len(body) > MAX_RIOT_RESPONSE_BYTES:
            raise RiotRSOError("RIOT_RESPONSE_TOO_LARGE")
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RiotRSOError("INVALID_RIOT_RESPONSE") from error
    if not isinstance(payload, dict):
        raise RiotRSOError("INVALID_RIOT_RESPONSE")
    return payload


async def _riot_error_details(response: aiohttp.ClientResponse) -> tuple[str | None, str | None]:
    """Best-effort OAuth error fields for logs; never raises."""

    try:
        body = bytearray()
        while len(body) < MAX_RIOT_ERROR_BYTES:
            chunk = await response.content.read(MAX_RIOT_ERROR_BYTES - len(body))
            if not chunk:
                break
            body.extend(chunk)
        payload = json.loads(body)
    except (aiohttp.ClientError, TimeoutError, ValueError):
        return None, None
    if not isinstance(payload, dict):
        return None, None
    return (
        _safe_log_text(payload.get("error"), 64),
        _safe_log_text(payload.get("error_description"), 200),
    )


def _safe_log_text(value: object, limit: int) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    printable = "".join(character if character.isprintable() else " " for character in value)
    return _SECRET_LIKE.sub("[redacted]", printable)[:limit]


class RequestRateLimiter:
    """Small fixed-window limiter; no external cache is needed on a single e2-micro.

    Buckets are kept in window start order, so expired ones are dropped from
    the front in amortized O(1). When the table is full, the bucket with the
    oldest window is evicted: a flood of new addresses can never lock out new
    legitimate clients, and known clients keep their counters until then.
    """

    def __init__(
        self, *, limit: int = 60, window_seconds: int = 60, max_clients: int = 4096
    ) -> None:
        if limit < 1 or window_seconds < 1 or max_clients < 1:
            raise ValueError("Rate limit, window and capacity must be positive")
        self._limit = limit
        self._window_seconds = window_seconds
        self._max_clients = max_clients
        self._clients: OrderedDict[str, tuple[int, float]] = OrderedDict()

    def allowed(self, client: str) -> bool:
        # IPv6 privacy addresses within one /64 share a budget. IPv4-mapped
        # addresses must use the same budget as their plain IPv4 spelling.
        if len(client) > 64:
            return False
        try:
            address = ipaddress.ip_address(client)
        except ValueError:
            return False
        if isinstance(address, ipaddress.IPv6Address):
            if address.ipv4_mapped is not None:
                client = str(address.ipv4_mapped)
            else:
                client = str(ipaddress.IPv6Network((address, 64), strict=False))
        else:
            client = str(address)
        now = time.monotonic()
        cutoff = now - self._window_seconds
        clients = self._clients
        while clients:
            oldest = next(iter(clients))
            if clients[oldest][1] > cutoff:
                break
            del clients[oldest]
        entry = clients.get(client)
        if entry is None:
            if len(clients) >= self._max_clients:
                clients.popitem(last=False)
            clients[client] = (1, now)
            return True
        count, started = entry
        count = min(count + 1, self._limit + 1)
        clients[client] = (count, started)
        return count <= self._limit


def create_app(settings: RSOSettings, database: Database) -> web.Application:
    limiter = RequestRateLimiter()

    @web.middleware
    async def request_guards(
        request: web.Request,
        handler: Callable[[web.Request], Awaitable[web.StreamResponse]],
    ) -> web.StreamResponse:
        allowed_hosts = {
            urlsplit(settings.rso_base_url).netloc.casefold(),
            f"127.0.0.1:{settings.rso_port}",
            f"localhost:{settings.rso_port}",
        }
        if request.host.casefold() not in allowed_hosts:
            response: web.StreamResponse = web.Response(
                status=400,
                text="Nieprawidłowy adres.",
            )
        elif not limiter.allowed(_client_ip(request)):
            response = web.Response(
                status=429,
                text="Za dużo prób w krótkim czasie. Poczekaj chwilę, strona odświeży się sama.",
                # The result page polls; Refresh lets it recover on its own.
                headers={"Refresh": str(RATE_LIMIT_REFRESH_SECONDS)},
            )
        else:
            try:
                response = await handler(request)
            except web.HTTPException as error:
                response = _http_error_response(error)
            except Exception:
                # Never log request.path or the query: /verify/start/{token}
                # carries a live start token and the callback an OAuth code.
                logger.exception(
                    "Unhandled error in RSO handler for %s %s",
                    request.method,
                    _route_for_log(request),
                )
                response = _status_page(*INTERNAL_ERROR_COPY, "error", status=500)
        response.headers.update(
            {
                "Cache-Control": "no-store, max-age=0",
                "Content-Security-Policy": (
                    "default-src 'none'; style-src 'unsafe-inline'; img-src 'self' data:; "
                    "font-src 'self'; "
                    "form-action 'self' https://auth.riotgames.com "
                    "https://authenticate.riotgames.com; "
                    "base-uri 'none'; frame-ancestors 'none'"
                ),
                "Cross-Origin-Opener-Policy": "same-origin",
                "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=()",
                "Referrer-Policy": "same-origin",
                "X-Content-Type-Options": "nosniff",
                "X-Frame-Options": "DENY",
            }
        )
        return response

    app = web.Application(middlewares=[request_guards], client_max_size=1024)
    app[SETTINGS_KEY] = settings
    app[DATABASE_KEY] = database
    app[REPOSITORY_KEY] = VerificationSessionRepository(database.session_factory)
    app.cleanup_ctx.append(_http_client_context)
    app.router.add_get("/healthz", health)
    app.router.add_get("/readyz", ready)
    app.router.add_get("/verify/start/{token}", show_start)
    app.router.add_post("/verify/start/{token}", begin_rso)
    app.router.add_get("/oauth2/callback", oauth_callback)
    app.router.add_get("/verify/result", verification_result)
    return app


def _route_for_log(request: web.Request) -> str:
    resource = request.match_info.route.resource
    return resource.canonical if resource is not None else "unmatched route"


def _http_error_response(error: web.HTTPException) -> web.Response:
    if error.status < 400:
        # Redirects keep Location; aiohttp's plain text body is not needed.
        headers = {
            name: value
            for name, value in error.headers.items()
            if name.casefold() != "content-type"
        }
        return web.Response(status=error.status, reason=error.reason, headers=headers)
    if error.status == 404:
        copy = (
            "Nie ma takiej strony",
            f"Sprawdź adres. Jeśli weryfikujesz konto, {_lowercase_first(NEW_LINK_HINT)}",
        )
    elif error.status < 500:
        copy = (
            "Nie udało się otworzyć strony",
            "Wróć do Discorda i otwórz link od bota jeszcze raz.",
        )
    else:
        copy = INTERNAL_ERROR_COPY
    response = _status_page(*copy, "error", status=error.status)
    for header in ("Allow", "Location"):
        if header in error.headers:
            response.headers[header] = error.headers[header]
    return response


async def _http_client_context(app: web.Application) -> AsyncIterator[None]:
    settings = app[SETTINGS_KEY]
    budget = settings.rso_http_timeout_seconds
    # Token and userinfo share auth.riotgames.com, so a burst of callbacks can
    # queue for one of its 8 pooled connections. aiohttp counts that queue
    # time in both connect and total. connect bounds the queue plus opening
    # the socket, sock_connect the TCP and TLS handshake alone and sock_read
    # each pause in Riot's answer. total is connect plus the same budget again,
    # so a call that waited in the queue still has a full budget for Riot's
    # answer. With Riot answering in about a second and the default 10 s
    # budget, the queue alone reaches the budget only at about 80 parallel
    # calls (10 rounds of 8 connections); past that, failing
    # fast is better than leaving the browser hanging. One Riot call takes at
    # most 2 x budget, a callback (three calls in a row) at most 6 x budget.
    timeout = aiohttp.ClientTimeout(
        total=budget * 2, connect=budget, sock_connect=budget, sock_read=budget
    )
    connector = aiohttp.TCPConnector(limit=16, limit_per_host=8, ttl_dns_cache=300)
    try:
        async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
            app[HTTP_CLIENT_KEY] = session
            app[RSO_CLIENT_KEY] = RiotRSOClient(settings, session)
            yield
            # Requests cancelled by the shutdown still mark their sessions as
            # failed in the background; let that finish before the database
            # engine closes.
            await _drain_background_tasks()
    finally:
        await app[DATABASE_KEY].close()


async def _drain_background_tasks() -> None:
    pending = set(_BACKGROUND_TASKS)
    if not pending:
        return
    _done, still_running = await asyncio.wait(pending, timeout=BACKGROUND_DRAIN_TIMEOUT_SECONDS)
    if still_running:
        logger.warning(
            "RSO shut down before %s session failure update(s) finished", len(still_running)
        )


async def health(_request: web.Request) -> web.Response:
    return web.json_response({"status": "ok"})


async def ready(request: web.Request) -> web.Response:
    # Caddy blocks /readyz, but aiohttp normalizes paths such as
    # /verify/%2e%2e/readyz. Proxied requests carry forwarding headers.
    if any(
        header in request.headers
        for header in ("X-Forwarded-For", "X-Forwarded-Host", "X-Forwarded-Proto", "Forwarded")
    ):
        raise web.HTTPNotFound
    try:
        async with request.app[DATABASE_KEY].session() as session:
            await session.execute(text("SELECT 1"))
    except Exception:
        logger.exception("RSO readiness check failed")
        return web.json_response({"status": "unavailable"}, status=503)
    return web.json_response({"status": "ready"})


def _see_other(location: str) -> web.Response:
    return web.Response(status=303, headers={"Location": location})


async def show_start(request: web.Request) -> web.Response:
    token = request.match_info["token"]
    if not TOKEN_PATTERN.fullmatch(token):
        return _status_page("Nieprawidłowy link", NEW_LINK_HINT, "error")
    record = await request.app[REPOSITORY_KEY].get_by_start_token(token)
    if record is None:
        return _status_page("Ten link nie działa", NEW_LINK_HINT, "error")
    now = _utc_now()
    if as_utc(record.expires_at) <= now or record.status == VerificationSessionStatus.EXPIRED.value:
        return _status_page("Link wygasł", NEW_LINK_HINT, "error")
    settings = request.app[SETTINGS_KEY]
    browser_cookie = request.cookies.get(_browser_cookie_name(settings), "")
    has_browser_identity = TOKEN_PATTERN.fullmatch(browser_cookie) is not None
    # The bound browser sees the start page again (for example after Back
    # from Riot). Its button issues a fresh OAuth state; GET changes nothing.
    retry_in_bound_browser = (
        record.status == VerificationSessionStatus.AWAITING_RIOT.value
        and has_browser_identity
        and browser_matches(record, browser_cookie)
    )
    if record.status != VerificationSessionStatus.CREATED.value and not retry_in_bound_browser:
        redirect_response = _see_other("/verify/result")
        _set_session_cookie(redirect_response, token, settings)
        return redirect_response

    minutes = max(1, int((as_utc(record.expires_at) - now).total_seconds() // 60))
    user_id = html.escape(str(record.discord_user_id))
    guild_id = html.escape(str(record.guild_id))
    if record.discord_username:
        user = f"<strong>{html.escape(record.discord_username)}</strong> (ID: {user_id})"
    else:
        user = f"o ID <strong>{user_id}</strong>"
    if record.discord_guild_name:
        guild = f"<strong>{html.escape(record.discord_guild_name)}</strong> (ID: {guild_id})"
    else:
        guild = f"o ID <strong>{guild_id}</strong>"
    body = f"""
        {_avatar("start")}
        <h1>Połącz konto przez Riot</h1>
        <p class="lead">Zalogujesz się na stronie Riot Games. Moon Poro nie widzi Twojego hasła, a Riot przekaże nam Twoje Riot ID i region.</p>
        <p class="identity">Łączysz konto Riot z kontem Discord {user} na serwerze {guild}.</p>
        <p class="warning">Jeśli to nie Twoje konto Discord lub ktoś przesłał Ci ten link, zamknij stronę. Rozpocznij weryfikację samodzielnie w panelu bota.</p>
        <form method="post" action="/verify/start/{html.escape(token)}">
          <button type="submit">Przejdź do logowania Riot</button>
        </form>
        <p class="fine">Link wygaśnie za około {minutes} min. Klikając, akceptujesz <a href="/terms/">warunki</a> i <a href="/privacy/">politykę prywatności</a>.</p>
    """
    response = _html_page("Połącz konto Riot", body)
    _set_session_cookie(response, token, settings)
    # Send the browser identity before the first POST, so a double click sends
    # the same identity twice instead of two random ones. An existing identity
    # is sent again with a fresh lifetime, so it cannot expire between this
    # page and the click.
    _set_browser_cookie(
        response,
        browser_cookie if has_browser_identity else secrets.token_urlsafe(32),
        settings,
    )
    return response


async def begin_rso(request: web.Request) -> web.StreamResponse:
    token = request.match_info["token"]
    if not TOKEN_PATTERN.fullmatch(token) or not _same_origin(request):
        return _status_page(
            "Nie udało się rozpocząć", "Wróć do Discorda i użyj nowego linku.", "error"
        )
    settings = request.app[SETTINGS_KEY]
    state = secrets.token_urlsafe(48)
    browser_token = request.cookies.get(_browser_cookie_name(settings), "")
    if not TOKEN_PATTERN.fullmatch(browser_token):
        browser_token = secrets.token_urlsafe(32)
    try:
        await request.app[REPOSITORY_KEY].begin_oauth(
            token=token, state=state, browser_token=browser_token
        )
    except SessionExpired:
        return _status_page("Link wygasł", NEW_LINK_HINT, "error")
    except SessionBrowserMismatch:
        return _status_page(
            "Logowanie trwa w innej przeglądarce",
            "Dokończ logowanie w przeglądarce, w której zaczęła się weryfikacja. "
            f"Jeśli to niemożliwe, {_lowercase_first(NEW_LINK_HINT)}",
            "error",
        )
    except SessionNotFound:
        return _status_page("Ten link nie działa", NEW_LINK_HINT, "error")
    except SessionAlreadyUsed:
        return _status_page("Link został już użyty", NEW_LINK_HINT, "error")
    response = _see_other(request.app[RSO_CLIENT_KEY].authorization_url(state))
    _set_session_cookie(response, token, settings)
    _set_browser_cookie(response, browser_token, settings)
    return response


async def oauth_callback(request: web.Request) -> web.StreamResponse:
    state = request.query.get("state", "")
    if not STATE_PATTERN.fullmatch(state):
        return _status_page(
            "Nieprawidłowa odpowiedź",
            "Nie udało się potwierdzić logowania. Zacznij jeszcze raz na Discordzie.",
            "error",
        )
    repository = request.app[REPOSITORY_KEY]
    try:
        record = await repository.claim_callback(
            state,
            browser_token=request.cookies.get(_browser_cookie_name(request.app[SETTINGS_KEY]), ""),
        )
    except SessionBrowserMismatch:
        return _status_page(
            "Wróć do poprzedniej przeglądarki",
            "Dokończ logowanie w przeglądarce, w której zaczęła się weryfikacja. "
            f"Jeśli to niemożliwe, {_lowercase_first(NEW_LINK_HINT)}",
            "error",
        )
    except SessionExpired:
        return _status_page("Sesja wygasła", NEW_LINK_HINT, "error")
    except SessionAlreadyUsed:
        # claim_callback checks the browser first, so this is the bound browser
        # reloading the callback. The first request still finishes the work.
        return _see_other("/verify/result")
    except SessionNotFound:
        return _status_page(
            "Ten link logowania jest nieaktualny",
            "Użyj najnowszej karty z logowaniem Riot. Jeśli jej nie masz, "
            f"{_lowercase_first(NEW_LINK_HINT)}",
            "error",
        )

    # From here on the session is PROCESSING_RIOT and only this request can
    # move it forward. Every exit must leave it in a state the result page
    # can explain, or the page would poll until the session expires.
    try:
        return await _finish_callback(request, record)
    except asyncio.CancelledError:
        _start_failure(repository, record.id)
        raise
    except Exception:
        logger.exception("RSO callback failed after the OAuth state was claimed")
        await _record_failure(repository, record.id)
        return _see_other("/verify/result")


async def _finish_callback(request: web.Request, record: VerificationSession) -> web.Response:
    repository = request.app[REPOSITORY_KEY]
    provider_error = request.query.get("error")
    if provider_error:
        error_code = "USER_CANCELLED" if provider_error == "access_denied" else "RIOT_AUTH_REJECTED"
        await repository.fail(record.id, error_code)
        return _see_other("/verify/result")

    code = request.query.get("code", "")
    if not code or len(code) > 4096:
        await repository.fail(record.id, "MISSING_AUTHORIZATION_CODE")
        return _see_other("/verify/result")

    try:
        access_token = await request.app[RSO_CLIENT_KEY].exchange_code(code)
        identity = await request.app[RSO_CLIENT_KEY].get_identity(access_token)
    except RiotRSOError as error:
        await repository.fail(record.id, error.code)
        return _see_other("/verify/result")

    try:
        result = await repository.reserve_link(
            session_id=record.id,
            platform=identity.platform,
            puuid=identity.puuid,
            game_name=identity.game_name,
            tag_line=identity.tag_line,
        )
    except SessionExpired:
        await repository.fail(
            record.id,
            "SESSION_EXPIRED",
            expected_status=VerificationSessionStatus.PROCESSING_RIOT.value,
        )
        return _see_other("/verify/result")
    except (SessionNotFound, SessionAlreadyUsed):
        # A newer link or a removal replaced this session during the Riot calls.
        return _status_page(
            "Ta weryfikacja nie jest już aktywna",
            "Wróć do Discorda i użyj najnowszego linku.",
            "error",
        )
    if result is not LinkReservationResult.RESERVED:
        logger.info("RSO link reservation rejected with %s", result.value)
    return _see_other("/verify/result")


async def _record_failure(repository: VerificationSessionRepository, session_id: int) -> None:
    # Shielded: a cancelled request must not abandon the status update halfway.
    # Errors are logged by the task callback.
    with contextlib.suppress(Exception):
        await asyncio.shield(_start_failure(repository, session_id))


def _start_failure(
    repository: VerificationSessionRepository, session_id: int
) -> asyncio.Task[None]:
    # Only PROCESSING_RIOT is failed: if an earlier commit (for example the
    # link reservation) did succeed, its result must not be overwritten.
    task = asyncio.ensure_future(
        repository.fail(
            session_id,
            "INTERNAL_ERROR",
            expected_status=VerificationSessionStatus.PROCESSING_RIOT.value,
        )
    )
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_forget_failure_task)
    return task


def _forget_failure_task(task: asyncio.Task[None]) -> None:
    _BACKGROUND_TASKS.discard(task)
    if not task.cancelled() and (error := task.exception()) is not None:
        logger.error("RSO could not mark a session as failed", exc_info=error)


async def verification_result(request: web.Request) -> web.Response:
    token = request.cookies.get(SESSION_COOKIE, "")
    if not TOKEN_PATTERN.fullmatch(token):
        return _status_page(
            "Sprawdź Discorda",
            "Ta przeglądarka nie ma danych tej weryfikacji. Jej stan sprawdzisz na Discordzie.",
            "neutral",
        )
    record = await request.app[REPOSITORY_KEY].get_by_start_token(token)
    if record is None:
        return _status_page("Sesja zakończona", "Wróć do Discorda, aby sprawdzić role.", "neutral")
    return _result_for_record(record)


def _result_for_record(record: VerificationSession, *, now: datetime | None = None) -> web.Response:
    now = now or _utc_now()
    status = record.status
    if status == VerificationSessionStatus.COMPLETED.value:
        account = "Twoje konto Riot"
        if record.riot_game_name and record.riot_tag_line:
            riot_id = f"{html.escape(record.riot_game_name)}#{html.escape(record.riot_tag_line)}"
            account = f"Konto Riot <strong>{riot_id}</strong>"
        return _status_page(
            "Konto połączone",
            f"{account} jest już połączone z Twoim kontem Discord. "
            "Twoje role znajdziesz na serwerze.",
            "success",
            button=("Wróć do Discorda", f"https://discord.com/channels/{record.guild_id}"),
        )
    if (
        status
        in {
            VerificationSessionStatus.CREATED.value,
            VerificationSessionStatus.AWAITING_RIOT.value,
            VerificationSessionStatus.PROCESSING_RIOT.value,
        }
        and as_utc(record.expires_at) <= now
    ):
        # The daily purge may not have run yet; a dead session must not poll forever.
        return _status_page("Sesja wygasła", NEW_LINK_HINT, "error")
    if status == VerificationSessionStatus.PROCESSING_RIOT.value:
        return _status_page(
            "Potwierdzamy logowanie w Riot",
            "Poczekaj chwilę, strona odświeży się sama.",
            "pending",
            refresh_seconds=2,
        )
    if status == VerificationSessionStatus.AWAITING_RIOT.value:
        return _status_page(
            "Logowanie w Riot jeszcze trwa",
            "Dokończ je w karcie, w której zaczęła się weryfikacja. Ta strona odświeży się sama.",
            "pending",
            refresh_seconds=10,
        )
    if status == VerificationSessionStatus.CREATED.value:
        return _status_page(
            "Logowanie jeszcze się nie zaczęło",
            "Otwórz link od bota i kliknij „Przejdź do logowania Riot”.",
            "neutral",
        )
    if status in {
        VerificationSessionStatus.VERIFIED_PENDING_DISCORD.value,
        VerificationSessionStatus.APPLYING_DISCORD.value,
    }:
        # Waiting on the bot is legitimate; never report it as a failure.
        confirmed_at = as_utc(record.verification_link_created_at or record.updated_at)
        if now - confirmed_at >= DISCORD_PENDING_SLOW_AFTER:
            return _status_page(
                "Riot potwierdził konto",
                "Bot jeszcze kończy weryfikację na Discordzie. Możesz zamknąć tę kartę, "
                "role pojawią się same. Jeśli nie zobaczysz ich w ciągu kilku minut, "
                "napisz do moderacji.",
                "pending",
                refresh_seconds=30,
            )
        return _status_page(
            "Riot potwierdził konto",
            "Kończymy weryfikację na Discordzie. Strona odświeży się sama.",
            "pending",
            refresh_seconds=2,
        )
    if status == VerificationSessionStatus.DISCORD_RETRY_PAUSED.value:
        return _status_page(
            "Weryfikacja wymaga pomocy",
            "Nie możemy teraz dokończyć weryfikacji. Kliknij ponownie „Zweryfikuj konto” "
            "na Discordzie. Jeśli problem się powtarza, skontaktuj się z moderacją.",
            "error",
        )
    if status == VerificationSessionStatus.EXPIRED.value or record.error_code == "SESSION_EXPIRED":
        return _status_page("Sesja wygasła", NEW_LINK_HINT, "error")
    link_changed = (
        "Połączenie konta się zmieniło",
        "W trakcie weryfikacji zmieniło się połączenie konta. Sprawdź „Moje konto” na Discordzie.",
    )
    cancelled = (
        "Logowanie anulowane",
        "Nic nie zostało połączone. Możesz zamknąć tę kartę albo zacząć jeszcze raz na Discordzie.",
    )
    if status == VerificationSessionStatus.CANCELLED.value:
        cancelled_copy = {
            "SUPERSEDED": (
                "Masz już nowszy link",
                "Ten link już nie działa. Użyj najnowszego linku od bota na Discordzie.",
            ),
            "USER_REMOVED_LINK": (
                "Połączenie zostało usunięte",
                "Połączenie konta usunięto na Discordzie. Możesz zamknąć tę kartę.",
            ),
            "VERIFICATION_LINK_CHANGED": link_changed,
        }
        title, message = cancelled_copy.get(record.error_code or "", cancelled)
        return _status_page(title, message, "neutral")
    if record.error_code == "USER_CANCELLED":
        return _status_page(*cancelled, "neutral")

    riot_unavailable = "Riot teraz nie odpowiada. Spróbuj ponownie za kilka minut."
    error_copy = {
        "DISCORD_ALREADY_LINKED": (
            "Twoje konto Discord jest już połączone z kontem Riot. Aby połączyć inne, "
            "najpierw usuń obecne w sekcji „Moje konto”."
        ),
        "RIOT_ALREADY_LINKED": "To konto Riot jest już połączone z innym kontem Discord.",
        "LINK_CONFLICT": "To konto Riot zostało przed chwilą połączone w innej weryfikacji.",
        "UNSUPPORTED_PLATFORM": "Nie obsługujemy jeszcze tego regionu League of Legends.",
        "MISSING_PLATFORM": "Nie udało się ustalić regionu League of Legends dla tego konta.",
        "MEMBER_LEFT_GUILD": "Nie ma Cię już na serwerze, więc połączenie zostało cofnięte.",
        "USER_REMOVED_LINK": "Połączenie konta usunięto na Discordzie.",
        "RIOT_AUTH_UNAVAILABLE": riot_unavailable,
        "RIOT_IDENTITY_UNAVAILABLE": riot_unavailable,
        # Set by the bot (fail_discord) when the link changed mid-verification.
        "VERIFICATION_LINK_CHANGED": link_changed[1],
    }
    message = error_copy.get(
        record.error_code or "",
        "Coś poszło nie tak po drodze. Wróć do Discorda i spróbuj ponownie za chwilę.",
    )
    return _status_page("Nie udało się połączyć konta", message, "error")


def _status_page(
    title: str,
    message: str,
    kind: str,
    *,
    button: tuple[str, str] | None = None,
    refresh_seconds: int | None = None,
    status: int = 200,
) -> web.Response:
    action = ""
    if button:
        label, url = button
        action = f'<a class="button" href="{html.escape(url, quote=True)}">{html.escape(label)}</a>'
    body = f"""
        {_avatar(kind)}
        <h1>{html.escape(title)}</h1>
        <p class="lead">{message}</p>
        {action}
    """
    response = _html_page(title, body, status=status)
    if refresh_seconds is not None:
        response.headers["Refresh"] = str(refresh_seconds)
    return response


def _avatar(kind: str) -> str:
    badge = {
        "success": '<span class="badge success">✓</span>',
        "error": '<span class="badge error">!</span>',
        "pending": '<span class="badge pending"><i></i><i></i><i></i></span>',
    }.get(kind, "")
    return (
        f'<div class="avatar" aria-hidden="true">'
        f'<img src="/moon-poro.png" alt="" width="96" height="96">{badge}</div>'
    )


def _html_page(title: str, body: str, *, status: int = 200) -> web.Response:
    document = f"""<!doctype html>
<html lang="pl">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="color-scheme" content="dark">
  <title>{html.escape(title)} · Moon Poro</title>
  <style>
    @font-face {{ font-family:"Nunito"; font-weight:200 1000; font-display:swap; src:url("/fonts/nunito-latin.woff2") format("woff2"); unicode-range:U+0000-00FF,U+0131,U+0152-0153,U+02BB-02BC,U+02C6,U+02DA,U+02DC,U+0304,U+0308,U+0329,U+2000-206F,U+20AC,U+2122,U+2191,U+2193,U+2212,U+2215,U+FEFF,U+FFFD; }}
    @font-face {{ font-family:"Nunito"; font-weight:200 1000; font-display:swap; src:url("/fonts/nunito-latin-ext.woff2") format("woff2"); unicode-range:U+0100-02BA,U+02BD-02C5,U+02C7-02CC,U+02CE-02D7,U+02DD-02FF,U+0304,U+0308,U+0329,U+1D00-1DBF,U+1E00-1E9F,U+1EF2-1EFF,U+2020,U+20A0-20AB,U+20AD-20C0,U+2113,U+2C60-2C7F,U+A720-A7FF; }}
    :root {{ color-scheme:dark; --night:#0d0b1e; --text:#efeaf8; --muted:#b3abcf; --quiet:#8a82aa; --moon:#ffe2a3; --pink:#ff8fae; --lavender:#b9a3f5; --line:#2f2856; }}
    * {{ box-sizing:border-box; }}
    body {{ margin:0; min-height:100vh; display:grid; grid-template-rows:1fr auto; justify-items:center; padding:24px 20px; color:var(--text); background:radial-gradient(48rem 30rem at 50% -6rem, rgba(124,98,214,.34), transparent 70%), var(--night); font:17px/1.6 "Nunito",system-ui,-apple-system,"Segoe UI",sans-serif; }}
    main {{ align-self:center; display:grid; justify-items:center; width:min(460px,100%); padding-block:32px; text-align:center; }}
    .avatar {{ position:relative; width:96px; height:96px; margin-bottom:26px; }}
    .avatar img {{ display:block; width:96px; height:96px; border-radius:50%; box-shadow:0 0 0 8px rgba(255,226,163,.08), 0 0 60px rgba(255,226,163,.22); }}
    .badge {{ position:absolute; right:-6px; bottom:-4px; display:grid; place-items:center; width:36px; height:36px; border:4px solid var(--night); border-radius:50%; font-weight:900; font-size:17px; line-height:1; }}
    .badge.success {{ color:#123629; background:#7ee2b0; }}
    .badge.error {{ color:#3d1d36; background:var(--pink); }}
    .badge.pending {{ display:flex; gap:3px; justify-content:center; align-items:center; background:var(--lavender); }}
    .badge.pending i {{ width:5px; height:5px; border-radius:50%; background:#241a3f; animation:blink 1.2s infinite ease-in-out; }}
    .badge.pending i:nth-child(2) {{ animation-delay:.2s; }} .badge.pending i:nth-child(3) {{ animation-delay:.4s; }}
    @keyframes blink {{ 0%,80%,100% {{ opacity:.25; }} 40% {{ opacity:1; }} }}
    h1 {{ margin:0 0 12px; font-size:clamp(28px,6vw,36px); font-weight:800; line-height:1.15; text-wrap:balance; }}
    .lead {{ margin:0 0 28px; color:var(--muted); text-wrap:pretty; }}
    .identity {{ width:100%; margin:0 0 16px; overflow-wrap:anywhere; }}
    .warning {{ margin:0 0 24px; padding:14px 16px; border:1px solid var(--pink); border-radius:16px; color:var(--text); text-wrap:pretty; }}
    form {{ width:100%; margin:0; }}
    button,.button {{ display:inline-flex; align-items:center; justify-content:center; width:100%; min-height:52px; padding:12px 24px; border:0; border-radius:999px; color:#241a3f; background:linear-gradient(180deg,#fff0cc,var(--moon)); box-shadow:0 6px 24px rgba(255,226,163,.18); font:800 17px "Nunito",system-ui,sans-serif; text-decoration:none; cursor:pointer; transition:transform .15s ease; }}
    button:hover,.button:hover {{ transform:translateY(-2px); }}
    :focus-visible {{ outline:3px solid var(--moon); outline-offset:4px; }}
    a {{ color:var(--lavender); }}
    .fine {{ margin:18px 0 0; color:var(--quiet); font-size:14px; }}
    strong {{ color:var(--text); }}
    footer {{ color:var(--quiet); font-size:14px; }}
    footer a {{ color:var(--quiet); margin-left:14px; }}
    @media (prefers-reduced-motion:reduce) {{ * {{ transition:none!important; animation:none!important; }} }}
  </style>
</head>
<body><main>{body}</main><footer>Moon Poro<a href="/privacy/">Prywatność</a><a href="/terms/">Warunki</a></footer></body>
</html>"""
    return web.Response(text=document, status=status, content_type="text/html", charset="utf-8")


def _uses_https(settings: RSOSettings) -> bool:
    return settings.rso_public_base_url.scheme == "https"


def _browser_cookie_name(settings: RSOSettings) -> str:
    return SECURE_BROWSER_COOKIE if _uses_https(settings) else BROWSER_COOKIE


def _set_session_cookie(response: web.StreamResponse, token: str, settings: RSOSettings) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=settings.rso_session_ttl_seconds,
        secure=_uses_https(settings),
        httponly=True,
        samesite="Lax",
        path="/verify",
    )


def _set_browser_cookie(
    response: web.StreamResponse, browser_token: str, settings: RSOSettings
) -> None:
    response.set_cookie(
        _browser_cookie_name(settings),
        browser_token,
        max_age=settings.rso_session_ttl_seconds,
        # __Host- requires exactly this: Secure, Path=/ and no Domain.
        secure=_uses_https(settings),
        httponly=True,
        samesite="Lax",
        path="/",
    )


def _lowercase_first(sentence: str) -> str:
    return sentence[:1].lower() + sentence[1:]


def _same_origin(request: web.Request) -> bool:
    expected = request.app[SETTINGS_KEY].rso_base_url.casefold()
    origin = request.headers.get("Origin")
    if origin is not None:
        return origin.rstrip("/").casefold() == expected
    referer = request.headers.get("Referer")
    if referer is not None:
        parts = urlsplit(referer)
        return f"{parts.scheme}://{parts.netloc}".casefold() == expected
    return True


def _client_ip(request: web.Request) -> str:
    peer = request.transport.get_extra_info("peername") if request.transport else None
    peer_ip = str(peer[0]) if isinstance(peer, tuple) and peer else (request.remote or "unknown")
    if peer_ip in {"127.0.0.1", "::1"}:
        forwarded = request.headers.get("X-Forwarded-For", "").split(",", 1)[0].strip()
        if forwarded:
            return forwarded[:64]
    return peer_ip[:64]


def _utc_now() -> datetime:
    return datetime.now(UTC)


def run() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] [%(levelname)-8s] %(name)s: %(message)s",
    )
    settings = RSOSettings()
    database = Database(settings)
    web.run_app(
        create_app(settings, database),
        host=settings.rso_host,
        port=settings.rso_port,
        access_log=None,
        print=None,
    )
