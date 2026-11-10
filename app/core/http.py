"""Сквозная HTTP-защита API: потолок тела, заголовки безопасности, кэш-политика,
X-Request-ID и лог медленных запросов.

Чистые ASGI-middleware, а не `BaseHTTPMiddleware`: тот буферизует тело ответа и
ломает стриминг, а тут нужен только доступ к заголовкам.
"""

from __future__ import annotations

import logging
import re
import time
import uuid

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.config import get_settings

logger = logging.getLogger("app.http")

# Ответы этих путей можно кэшировать браузеру/CDN: публичные справочники и
# агрегаты, одинаковые для всех. Админка и платежи — никогда (`no-store`).
# Одинаковы для всех — можно в общий кэш (CDN).
_PUBLIC_CACHEABLE = ("/api/v1/catalog/", "/api/v1/public/")
# Только подписчикам — кэш браузера этого человека, не общий.
_PRIVATE_CACHEABLE = ("/api/v1/dashboard/", "/api/v1/market/")
_NO_STORE = ("/api/v1/admin/", "/api/v1/payments/", "/api/v1/auth/")
# Ответ зависит от роли (кука сессии) — общий кэш обязан различать по куке.
_VARY_COOKIE = ("/api/v1/public/listings/",)

_SECURITY_HEADERS = [
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"referrer-policy", b"no-referrer"),
    # API отдаёт только JSON: запрещаем ему всё, что умеет браузер, на случай,
    # если ответ всё-таки откроют как страницу.
    (b"content-security-policy", b"default-src 'none'; frame-ancestors 'none'"),
]
# Swagger UI грузит скрипты с CDN — жёсткий CSP его сломал бы.
_DOCS_PATHS = ("/docs", "/redoc", "/openapi.json")

# Свой X-Request-ID от клиента принимаем, только если он похож на id: иначе
# через заголовок в наши логи можно было бы вписать что угодно.
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._-]{8,64}$")


def _cache_control(path: str, method: str, status: int) -> bytes | None:
    if path.startswith(_NO_STORE):
        return b"no-store"
    max_age = get_settings().api_public_max_age_seconds
    if method != "GET" or status != 200 or max_age <= 0:
        return None
    if path.startswith(_VARY_COOKIE):
        return f"private, max-age={max_age}".encode()
    if path.startswith(_PUBLIC_CACHEABLE):
        return f"public, max-age={max_age}".encode()
    if path.startswith(_PRIVATE_CACHEABLE):
        return f"private, max-age={max_age}".encode()
    return None


class BodySizeLimitMiddleware:
    """413 на тело больше `API_MAX_BODY_BYTES` — до того, как оно прочитано.

    Проверяем и `Content-Length`, и фактический поток: chunked-запрос заголовка
    длины не несёт, и без подсчёта байтов лимит обходился бы им.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        limit = get_settings().api_max_body_bytes
        for name, value in scope.get("headers", []):
            if name == b"content-length":
                try:
                    if int(value) > limit:
                        await _send_413(send)
                        return
                except ValueError:
                    await _send_413(send)
                    return

        received = 0

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    raise _BodyTooLarge
            return message

        try:
            await self.app(scope, limited_receive, send)
        except _BodyTooLarge:
            await _send_413(send)


class _BodyTooLarge(Exception):
    pass


async def _send_413(send: Send) -> None:
    body = b'{"detail":"Request body too large"}'
    await send(
        {
            "type": "http.response.start",
            "status": 413,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


class SecurityHeadersMiddleware:
    """Заголовки безопасности, Cache-Control, X-Request-ID и тайминг."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path: str = scope.get("path", "")
        method: str = scope.get("method", "GET")
        request_id = None
        for name, value in scope.get("headers", []):
            if name == b"x-request-id":
                candidate = value.decode("latin-1")
                if _REQUEST_ID_RE.match(candidate):
                    request_id = candidate
                break
        request_id = request_id or uuid.uuid4().hex
        scope.setdefault("state", {})["request_id"] = request_id

        started = time.perf_counter()
        status_holder = {"status": 0}

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                status = message["status"]
                status_holder["status"] = status
                headers = list(message.get("headers", []))
                present = {name.lower() for name, _ in headers}
                if not path.startswith(_DOCS_PATHS):
                    headers.extend(h for h in _SECURITY_HEADERS if h[0] not in present)
                else:
                    headers.extend(
                        h for h in _SECURITY_HEADERS[:3] if h[0] not in present
                    )
                cache = _cache_control(path, method, status)
                if cache and b"cache-control" not in present:
                    headers.append((b"cache-control", cache))
                if path.startswith(_VARY_COOKIE):
                    headers.append((b"vary", b"Cookie"))
                headers.append((b"x-request-id", request_id.encode()))
                elapsed_ms = (time.perf_counter() - started) * 1000
                headers.append((b"server-timing", f"app;dur={elapsed_ms:.1f}".encode()))
                message["headers"] = headers
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            elapsed_ms = (time.perf_counter() - started) * 1000
            slow_ms = get_settings().api_slow_request_ms
            if slow_ms > 0 and elapsed_ms >= slow_ms:
                # Query не логируем: в нём может оказаться что угодно от клиента.
                logger.warning(
                    "slow request %s %s -> %s in %.0f ms (request_id=%s)",
                    method,
                    path,
                    status_holder["status"],
                    elapsed_ms,
                    request_id,
                )
