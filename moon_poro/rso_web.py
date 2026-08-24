from __future__ import annotations

import html
import json
import logging
import re
import secrets
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
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
    SessionExpired,
    SessionNotFound,
    VerificationSessionRepository,
    as_utc,
)

logger = logging.getLogger("moon_poro.rso")

TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_-]{40,80}$")
STATE_PATTERN = re.compile(r"^[A-Za-z0-9_-]{40,200}$")
MAX_RIOT_RESPONSE_BYTES = 64 * 1024
SESSION_COOKIE = "moon_poro_rso"
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
                payload = await _read_riot_json(response, expected_status=200)
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
            userinfo = await self._get_json("https://auth.riotgames.com/userinfo", headers)
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
            account = await self._get_json(account_url, headers)
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

    async def _get_json(self, url: str, headers: dict[str, str]) -> dict[str, Any]:
        async with self._session.get(url, headers=headers, allow_redirects=False) as response:
            return await _read_riot_json(response, expected_status=200)


RSO_CLIENT_KEY = web.AppKey("rso_client", RiotRSOClient)


async def _read_riot_json(
    response: aiohttp.ClientResponse, *, expected_status: int
) -> dict[str, Any]:
    if response.status != expected_status:
        logger.warning("RSO upstream returned HTTP %s", response.status)
        raise RiotRSOError("RIOT_UPSTREAM_REJECTED")
    body = await response.content.read(MAX_RIOT_RESPONSE_BYTES + 1)
    if len(body) > MAX_RIOT_RESPONSE_BYTES:
        raise RiotRSOError("RIOT_RESPONSE_TOO_LARGE")
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RiotRSOError("INVALID_RIOT_RESPONSE") from error
    if not isinstance(payload, dict):
        raise RiotRSOError("INVALID_RIOT_RESPONSE")
    return payload


class RequestRateLimiter:
    """Small fixed-window limiter; no external cache is needed on a single e2-micro."""

    def __init__(self, *, limit: int = 60, window_seconds: int = 60) -> None:
        self._limit = limit
        self._window_seconds = window_seconds
        self._clients: dict[str, tuple[int, float]] = {}

    def allowed(self, client: str) -> bool:
        now = time.monotonic()
        count, started = self._clients.get(client, (0, now))
        if now - started >= self._window_seconds:
            count, started = 0, now
        count += 1
        self._clients[client] = (count, started)
        if len(self._clients) > 4096:
            cutoff = now - self._window_seconds
            self._clients = {
                key: value for key, value in self._clients.items() if value[1] >= cutoff
            }
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
            response: web.StreamResponse = web.Response(status=400, text="Invalid host")
        elif not limiter.allowed(_client_ip(request)):
            response = web.Response(status=429, text="Too many requests")
        else:
            try:
                response = await handler(request)
            except web.HTTPException as error:
                response = web.Response(
                    status=error.status,
                    reason=error.reason,
                    headers=error.headers,
                )
        response.headers.update(
            {
                "Cache-Control": "no-store, max-age=0",
                "Content-Security-Policy": (
                    "default-src 'none'; style-src 'unsafe-inline'; img-src data:; "
                    "form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
                ),
                "Cross-Origin-Opener-Policy": "same-origin",
                "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=()",
                "Referrer-Policy": "no-referrer",
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


async def _http_client_context(app: web.Application) -> AsyncIterator[None]:
    settings = app[SETTINGS_KEY]
    timeout = aiohttp.ClientTimeout(total=settings.rso_http_timeout_seconds)
    connector = aiohttp.TCPConnector(limit=4, limit_per_host=2, ttl_dns_cache=300)
    async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
        app[HTTP_CLIENT_KEY] = session
        app[RSO_CLIENT_KEY] = RiotRSOClient(settings, session)
        yield
    await app[DATABASE_KEY].close()


async def health(_request: web.Request) -> web.Response:
    return web.json_response({"status": "ok"})


async def ready(request: web.Request) -> web.Response:
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
        return _status_page("Nieprawidłowy link", "Poproś bota o nowy link weryfikacyjny.", "error")
    record = await request.app[REPOSITORY_KEY].get_by_start_token(token)
    if record is None:
        return _status_page("Link nie istnieje", "Poproś bota o nowy link weryfikacyjny.", "error")
    now = _utc_now()
    if as_utc(record.expires_at) <= now or record.status == VerificationSessionStatus.EXPIRED.value:
        return _status_page(
            "Link wygasł", "Wróć do Discorda i rozpocznij weryfikację ponownie.", "error"
        )
    if record.status != VerificationSessionStatus.CREATED.value:
        redirect_response = _see_other("/verify/result")
        _set_session_cookie(redirect_response, token, request.app[SETTINGS_KEY])
        return redirect_response

    minutes = max(1, int((as_utc(record.expires_at) - now).total_seconds() // 60))
    body = f"""
        <p class="eyebrow">Jednorazowe połączenie</p>
        <h1>Połącz konto przez Riot.</h1>
        <p class="lead">Za chwilę przejdziesz do oficjalnej strony Riot Games. Moon Poro nie zobaczy Twojego hasła. Link wygaśnie za około {minutes} min.</p>
        <ol class="steps">
          <li><span>1</span>Zaloguj się na stronie Riot.</li>
          <li><span>2</span>Potwierdź dostęp do Riot ID i regionu.</li>
          <li><span>3</span>Wróć do Discorda — role pojawią się automatycznie.</li>
        </ol>
        <form method="post" action="/verify/start/{html.escape(token)}">
          <button type="submit">Przejdź do logowania Riot <span aria-hidden="true">→</span></button>
        </form>
        <p class="fine">Kontynuując, akceptujesz <a href="/terms">warunki</a> i potwierdzasz zapoznanie się z <a href="/privacy">polityką prywatności</a>.</p>
    """
    response = _html_page("Połącz konto Riot", body)
    _set_session_cookie(response, token, request.app[SETTINGS_KEY])
    return response


async def begin_rso(request: web.Request) -> web.StreamResponse:
    token = request.match_info["token"]
    if not TOKEN_PATTERN.fullmatch(token) or not _same_origin(request):
        return _status_page(
            "Nie udało się rozpocząć", "Wróć do Discorda i użyj nowego linku.", "error"
        )
    state = secrets.token_urlsafe(48)
    try:
        await request.app[REPOSITORY_KEY].begin_oauth(token=token, state=state)
    except SessionExpired:
        return _status_page("Link wygasł", "Wróć do Discorda i rozpocznij ponownie.", "error")
    except (SessionNotFound, SessionAlreadyUsed):
        return _status_page("Link został już użyty", "Każdy link działa tylko raz.", "error")
    response = _see_other(request.app[RSO_CLIENT_KEY].authorization_url(state))
    _set_session_cookie(response, token, request.app[SETTINGS_KEY])
    return response


async def oauth_callback(request: web.Request) -> web.StreamResponse:
    state = request.query.get("state", "")
    if not STATE_PATTERN.fullmatch(state):
        return _status_page(
            "Nieprawidłowa odpowiedź",
            "Nie udało się potwierdzić logowania. Rozpocznij ponownie w Discordzie.",
            "error",
        )
    try:
        record = await request.app[REPOSITORY_KEY].claim_callback(state)
    except SessionExpired:
        return _status_page("Sesja wygasła", "Wróć do Discorda i rozpocznij ponownie.", "error")
    except (SessionNotFound, SessionAlreadyUsed):
        return _status_page(
            "Sesja została już użyta", "Dla bezpieczeństwa rozpocznij nową weryfikację.", "error"
        )

    provider_error = request.query.get("error")
    if provider_error:
        error_code = "USER_CANCELLED" if provider_error == "access_denied" else "RIOT_AUTH_REJECTED"
        await request.app[REPOSITORY_KEY].fail(record.id, error_code)
        return _see_other("/verify/result")

    code = request.query.get("code", "")
    if not code or len(code) > 4096:
        await request.app[REPOSITORY_KEY].fail(record.id, "MISSING_AUTHORIZATION_CODE")
        return _see_other("/verify/result")

    try:
        access_token = await request.app[RSO_CLIENT_KEY].exchange_code(code)
        identity = await request.app[RSO_CLIENT_KEY].get_identity(access_token)
    except RiotRSOError as error:
        await request.app[REPOSITORY_KEY].fail(record.id, error.code)
        return _see_other("/verify/result")

    result = await request.app[REPOSITORY_KEY].reserve_link(
        session_id=record.id,
        platform=identity.platform,
        puuid=identity.puuid,
        game_name=identity.game_name,
        tag_line=identity.tag_line,
    )
    if result is not LinkReservationResult.RESERVED:
        logger.info("RSO link reservation rejected with %s", result.value)
    return _see_other("/verify/result")


async def verification_result(request: web.Request) -> web.Response:
    token = request.cookies.get(SESSION_COOKIE, "")
    if not TOKEN_PATTERN.fullmatch(token):
        return _status_page(
            "Sprawdź Discorda",
            "Nie można odczytać tej sesji w przeglądarce. Status weryfikacji znajdziesz w Discordzie.",
            "neutral",
        )
    record = await request.app[REPOSITORY_KEY].get_by_start_token(token)
    if record is None:
        return _status_page("Sesja zakończona", "Wróć do Discorda, aby sprawdzić role.", "neutral")
    return _result_for_record(record)


def _result_for_record(record: VerificationSession) -> web.Response:
    status = record.status
    if status == VerificationSessionStatus.COMPLETED.value:
        riot_id = ""
        if record.riot_game_name and record.riot_tag_line:
            riot_id = f" Konto <strong>{html.escape(record.riot_game_name)}#{html.escape(record.riot_tag_line)}</strong> jest połączone."
        return _status_page(
            "Gotowe — jesteś zweryfikowany",
            f"{riot_id} Role regionu i rangi zostały zaktualizowane. Możesz wrócić do Discorda.",
            "success",
            button=("Wróć do Discorda", f"https://discord.com/channels/{record.guild_id}"),
        )
    if status in {
        VerificationSessionStatus.PROCESSING_RIOT.value,
        VerificationSessionStatus.VERIFIED_PENDING_DISCORD.value,
        VerificationSessionStatus.APPLYING_DISCORD.value,
    }:
        return _status_page(
            "Riot potwierdził konto",
            "Bot aktualizuje teraz role na Discordzie. Ta strona odświeży się automatycznie.",
            "pending",
            refresh_seconds=2,
        )
    if status == VerificationSessionStatus.CANCELLED.value or record.error_code == "USER_CANCELLED":
        return _status_page(
            "Logowanie anulowane",
            "Żadne konto nie zostało połączone. Możesz bezpiecznie zamknąć kartę.",
            "neutral",
        )
    if status == VerificationSessionStatus.EXPIRED.value:
        return _status_page(
            "Sesja wygasła", "Wróć do Discorda i rozpocznij weryfikację ponownie.", "error"
        )

    error_copy = {
        "DISCORD_ALREADY_LINKED": "To konto Discord ma już połączone konto Riot.",
        "RIOT_ALREADY_LINKED": "To konto Riot jest już połączone z innym kontem Discord.",
        "LINK_CONFLICT": "Konto zostało już połączone w innej rozpoczętej weryfikacji.",
        "UNSUPPORTED_PLATFORM": "Ten region League of Legends nie jest obecnie obsługiwany.",
        "MISSING_PLATFORM": (
            "Nie udało się ustalić obsługiwanego regionu League of Legends dla tego konta."
        ),
        "MEMBER_LEFT_GUILD": "Nie ma Cię już na serwerze Discord, więc powiązanie zostało cofnięte.",
        "USER_REMOVED_LINK": "Powiązanie zostało usunięte w Discordzie.",
    }
    message = error_copy.get(
        record.error_code or "",
        "Nie udało się potwierdzić konta. Wróć do Discorda i spróbuj ponownie za chwilę.",
    )
    return _status_page("Weryfikacja nie została ukończona", message, "error")


def _status_page(
    title: str,
    message: str,
    kind: str,
    *,
    button: tuple[str, str] | None = None,
    refresh_seconds: int | None = None,
) -> web.Response:
    icon = {"success": "✓", "error": "!", "pending": "···", "neutral": "↗"}[kind]
    action = ""
    if button:
        label, url = button
        action = f'<a class="button" href="{html.escape(url, quote=True)}">{html.escape(label)} <span aria-hidden="true">→</span></a>'
    body = f"""
        <div class="status-icon {kind}" aria-hidden="true">{icon}</div>
        <p class="eyebrow">Weryfikacja konta Riot</p>
        <h1>{html.escape(title)}</h1>
        <p class="lead">{message}</p>
        {action}
        <p class="fine">Moon Poro nigdy nie otrzymuje Twojego hasła Riot.</p>
    """
    response = _html_page(title, body)
    if refresh_seconds is not None:
        response.headers["Refresh"] = str(refresh_seconds)
    return response


def _html_page(title: str, body: str) -> web.Response:
    document = f"""<!doctype html>
<html lang="pl">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="color-scheme" content="dark">
  <title>{html.escape(title)} · Moon Poro</title>
  <style>
    :root {{ color-scheme: dark; --ink:#eef8fb; --muted:#a7bcc7; --night:#08131d; --panel:#102431; --ice:#8fe6f3; --berry:#ef6480; --line:#29404e; }}
    * {{ box-sizing:border-box; }}
    body {{ margin:0; min-height:100vh; display:grid; place-items:center; padding:24px; color:var(--ink); background:radial-gradient(circle at 70% 15%, #173547 0, var(--night) 42%, #050b11 100%); font:16px/1.65 system-ui,-apple-system,"Segoe UI",sans-serif; }}
    main {{ width:min(680px,100%); padding:clamp(28px,6vw,58px); border:1px solid var(--line); border-radius:30px 30px 30px 8px; background:linear-gradient(145deg,rgba(16,36,49,.96),rgba(8,22,31,.96)); box-shadow:0 28px 80px rgba(0,0,0,.35); }}
    .eyebrow {{ margin:0 0 10px; color:var(--ice); font:700 12px/1.2 ui-monospace,"Cascadia Mono",monospace; letter-spacing:.14em; text-transform:uppercase; }}
    h1 {{ max-width:14ch; margin:0 0 18px; font:700 clamp(36px,8vw,64px)/.98 "Trebuchet MS",system-ui,sans-serif; letter-spacing:-.045em; }}
    .lead {{ margin:0 0 28px; color:var(--muted); font-size:clamp(17px,3vw,20px); }}
    .steps {{ display:grid; gap:12px; margin:28px 0; padding:0; list-style:none; }}
    .steps li {{ display:flex; gap:12px; align-items:center; padding:12px 14px; border-top:1px solid var(--line); }}
    .steps span {{ display:grid; place-items:center; flex:0 0 28px; height:28px; border:1px solid #407083; border-radius:50%; color:var(--ice); font:700 12px ui-monospace,monospace; }}
    button,.button {{ display:inline-flex; align-items:center; justify-content:center; gap:12px; width:100%; min-height:54px; padding:14px 20px; border:0; border-radius:16px 16px 16px 4px; color:#07141b; background:var(--ice); font:800 16px system-ui,sans-serif; text-decoration:none; cursor:pointer; transition:transform .18s ease,background .18s ease; }}
    button:hover,.button:hover {{ transform:translateY(-2px); background:#b6f4fb; }}
    button:focus-visible,.button:focus-visible,a:focus-visible {{ outline:3px solid var(--berry); outline-offset:4px; }}
    a {{ color:var(--ice); }}
    .fine {{ margin:22px 0 0; color:#819aa7; font-size:13px; }}
    .status-icon {{ display:grid; place-items:center; width:58px; height:58px; margin-bottom:24px; border:1px solid var(--line); border-radius:50% 50% 50% 16px; color:var(--ice); background:#123140; font:800 24px ui-monospace,monospace; }}
    .status-icon.error {{ color:#ff9cad; background:#3a1c29; }} .status-icon.success {{ color:#a9f7cc; background:#123629; }}
    strong {{ color:var(--ink); }}
    @media (prefers-reduced-motion:reduce) {{ * {{ transition:none!important; }} }}
  </style>
</head>
<body><main>{body}</main></body>
</html>"""
    return web.Response(text=document, content_type="text/html", charset="utf-8")


def _set_session_cookie(response: web.StreamResponse, token: str, settings: RSOSettings) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=settings.rso_session_ttl_seconds,
        secure=settings.rso_public_base_url.scheme == "https",
        httponly=True,
        samesite="Lax",
        path="/verify",
    )


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
