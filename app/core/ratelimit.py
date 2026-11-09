"""Ограничение частоты запросов к API: токен-бакет в памяти процесса.

Почему в памяти, а не в Redis: контейнер `api` один (uvicorn без `--workers`),
Redis в стеке нет, а лимит обязан работать с первого дня внешнего доступа.
Интерфейс (`TokenBucketLimiter.hit`) нарочно узкий — при переходе на несколько
воркеров или машин его реализацию меняют на Redis (`INCR`+`EXPIRE` или GCRA),
не трогая middleware и ручки. Порядок перехода — `docs/api/PUBLIC_API.md`.

Ключ — реальный IP клиента. Он появляется в `request.client` только если
uvicorn запущен с `--proxy-headers` и доверяет Caddy (`FORWARDED_ALLOW_IPS`):
без этого все внешние клиенты выглядят одним адресом контейнера Caddy и делят
один бакет — один шумный клиент отрезал бы всех (так и было у входа в админку).
"""

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass
from typing import Callable

from starlette.types import ASGIApp, Receive, Scope, Send

from app.core.config import Settings, get_settings


@dataclass(slots=True)
class Policy:
    """Средняя скорость (запросов в минуту) и допустимый всплеск."""

    name: str
    per_minute: int
    burst: int

    @property
    def rate_per_second(self) -> float:
        return self.per_minute / 60.0


@dataclass(slots=True)
class Decision:
    allowed: bool
    limit: int
    remaining: int
    # Через сколько секунд появится следующий токен (для Retry-After) и через
    # сколько бакет наполнится целиком (для RateLimit-Reset).
    retry_after: int
    reset_after: int


class TokenBucketLimiter:
    """Потокобезопасный набор бакетов с ограниченным числом ключей.

    Лок нужен: ручки FastAPI синхронные и крутятся в пуле потоков, а
    `admin_auth.too_many_attempts` зовёт лимитер оттуда.
    """

    def __init__(
        self,
        *,
        max_keys: int = 50_000,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._buckets: dict[tuple[str, str], tuple[float, float]] = {}
        self._lock = threading.Lock()
        self._max_keys = max_keys
        self._clock = clock

    def hit(self, policy: Policy, key: str, cost: float = 1.0) -> Decision:
        now = self._clock()
        capacity = float(max(policy.burst, 1))
        rate = policy.rate_per_second
        bucket_key = (policy.name, key)
        with self._lock:
            tokens, updated = self._buckets.get(bucket_key, (capacity, now))
            if rate > 0:
                tokens = min(capacity, tokens + (now - updated) * rate)
            allowed = tokens >= cost
            if allowed:
                tokens -= cost
            if bucket_key not in self._buckets and len(self._buckets) >= self._max_keys:
                self._prune(now)
            self._buckets[bucket_key] = (tokens, now)

        if rate > 0:
            retry_after = 0 if allowed else math.ceil((cost - tokens) / rate)
            reset_after = math.ceil((capacity - tokens) / rate)
        else:  # rate=0: бакет не пополняется, «когда» не наступит
            retry_after = reset_after = 0 if allowed else 3600
        return Decision(
            allowed=allowed,
            limit=int(capacity),
            remaining=max(0, int(tokens)),
            retry_after=max(retry_after, 0 if allowed else 1),
            reset_after=reset_after,
        )

    def _prune(self, now: float) -> None:
        """Выкидывает бакеты, простоявшие дольше 10 минут (они уже полны —
        хранить их незачем). Если и этого мало — самую старую половину: лучше
        простить часть клиентов, чем дать перебором ключей съесть память."""
        stale = [k for k, (_, updated) in self._buckets.items() if now - updated > 600]
        for k in stale:
            del self._buckets[k]
        if len(self._buckets) >= self._max_keys:
            ordered = sorted(self._buckets.items(), key=lambda item: item[1][1])
            for k, _ in ordered[: len(ordered) // 2]:
                del self._buckets[k]

    def reset(self) -> None:
        with self._lock:
            self._buckets.clear()

    def __len__(self) -> int:
        return len(self._buckets)


# Один лимитер на процесс; тесты сбрасывают его в conftest.
limiter = TokenBucketLimiter()

# Пути, которые лимитом не режем:
#   /health — его дёргают мониторинг и docker healthcheck;
#   /api/v1/payments/ — webhook Finik: защищён подписью, а отбитый 429 Finik
#   повторит с задержкой до суток — это «деньги списаны, доступа нет».
_EXEMPT_PREFIXES = ("/health", "/api/v1/payments/")
_HEAVY_PREFIXES = (
    "/api/v1/market/",
    "/api/v1/estimate",
    "/api/v1/price-trend",
    "/api/v1/public/feed/count",
    "/api/v1/public/feed/histograms",
)
_ADMIN_PREFIX = "/api/v1/admin/"


def classify(path: str) -> str | None:
    """Какая политика у пути. None — без лимита."""
    if path.startswith(_EXEMPT_PREFIXES):
        return None
    if path.startswith(_ADMIN_PREFIX):
        return "admin"
    if path.startswith(_HEAVY_PREFIXES):
        return "heavy"
    return "public"


def policies(settings: Settings) -> dict[str, Policy]:
    return {
        "public": Policy(
            "public", settings.rate_limit_public_per_minute, settings.rate_limit_public_burst
        ),
        "heavy": Policy(
            "heavy", settings.rate_limit_heavy_per_minute, settings.rate_limit_heavy_burst
        ),
        "admin": Policy(
            "admin", settings.rate_limit_admin_per_minute, settings.rate_limit_admin_burst
        ),
    }


def client_ip(scope: Scope) -> str:
    client = scope.get("client")
    return client[0] if client else "unknown"


class RateLimitMiddleware:
    """ASGI-middleware: 429 с `Retry-After` до того, как запрос дойдёт до базы.

    На каждый ответ — заголовки `RateLimit-Limit/Remaining/Reset` (черновик
    IETF RateLimit header fields): нормальный клиент сайта видит остаток и
    сбавляет темп сам, не дожидаясь отказа.
    """

    def __init__(self, app: ASGIApp, bucket: TokenBucketLimiter | None = None) -> None:
        self.app = app
        self.bucket = bucket or limiter

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        settings = get_settings()
        if scope["type"] != "http" or not settings.rate_limit_enabled:
            await self.app(scope, receive, send)
            return
        # Preflight CORS не считаем: браузер шлёт его сам, перед настоящим запросом.
        if scope.get("method") == "OPTIONS":
            await self.app(scope, receive, send)
            return

        kind = classify(scope.get("path", ""))
        if kind is None:
            await self.app(scope, receive, send)
            return

        policy = policies(settings)[kind]
        decision = self.bucket.hit(policy, client_ip(scope))
        if decision.allowed and kind == "heavy" and settings.rate_limit_heavy_global_per_minute > 0:
            global_policy = Policy(
                "heavy_global",
                settings.rate_limit_heavy_global_per_minute,
                max(settings.rate_limit_heavy_global_per_minute // 4, 1),
            )
            global_decision = self.bucket.hit(global_policy, "*")
            if not global_decision.allowed:
                decision = global_decision

        headers = [
            (b"ratelimit-limit", str(decision.limit).encode()),
            (b"ratelimit-remaining", str(decision.remaining).encode()),
            (b"ratelimit-reset", str(decision.reset_after).encode()),
        ]

        if not decision.allowed:
            body = (
                '{"detail":"Слишком много запросов. Повторите позже.",'
                f'"retry_after":{decision.retry_after}}}'
            ).encode()
            await send(
                {
                    "type": "http.response.start",
                    "status": 429,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"content-length", str(len(body)).encode()),
                        (b"retry-after", str(decision.retry_after).encode()),
                        *headers,
                    ],
                }
            )
            await send({"type": "http.response.body", "body": body})
            return

        async def send_with_headers(message):  # type: ignore[no-untyped-def]
            if message["type"] == "http.response.start":
                message.setdefault("headers", [])
                message["headers"] = [*message["headers"], *headers]
            await send(message)

        await self.app(scope, receive, send_with_headers)
