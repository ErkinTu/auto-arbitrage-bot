"""Защита внешнего API: лимит частоты, потолок тела, заголовки, кэш ответов.

Граница проверяется на настоящем `app.main:app` (порядок middleware — часть
поведения), база подменена — ручки каталога/дашборда мокаются на уровне роутера.
"""

from __future__ import annotations

import threading

import pytest
from fastapi.testclient import TestClient

from app.api import catalog as catalog_api
from app.api import dashboard as dashboard_api
from app.core.cache import TTLCache
from app.core.config import get_settings
from app.core.ratelimit import Policy, TokenBucketLimiter, classify
from app.db.session import get_session
from app.main import app


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    def _no_db():
        yield None

    app.dependency_overrides[get_session] = _no_db
    monkeypatch.setattr(catalog_api, "list_brands", lambda session: [])
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture()
def settings(monkeypatch: pytest.MonkeyPatch):
    s = get_settings()

    def set_(**values):
        for name, value in values.items():
            monkeypatch.setattr(s, name, value)

    return set_


# --- токен-бакет -------------------------------------------------------------


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_bucket_allows_burst_then_refuses_with_retry_after() -> None:
    clock = FakeClock()
    bucket = TokenBucketLimiter(clock=clock)
    policy = Policy("t", per_minute=60, burst=3)

    assert [bucket.hit(policy, "ip").allowed for _ in range(3)] == [True] * 3
    refused = bucket.hit(policy, "ip")
    assert not refused.allowed
    assert refused.retry_after == 1  # 60/мин = токен в секунду
    assert refused.remaining == 0

    clock.now += 1.0
    assert bucket.hit(policy, "ip").allowed


def test_buckets_are_per_client_and_per_policy() -> None:
    bucket = TokenBucketLimiter(clock=FakeClock())
    policy = Policy("t", per_minute=60, burst=1)
    other = Policy("o", per_minute=60, burst=1)
    assert bucket.hit(policy, "a").allowed
    assert not bucket.hit(policy, "a").allowed
    assert bucket.hit(policy, "b").allowed  # чужой IP не страдает
    assert bucket.hit(other, "a").allowed  # и другая политика тоже


def test_bucket_memory_is_bounded() -> None:
    clock = FakeClock()
    bucket = TokenBucketLimiter(max_keys=100, clock=clock)
    policy = Policy("t", per_minute=60, burst=5)
    for i in range(1000):
        bucket.hit(policy, f"ip{i}")
        clock.now += 0.001
    assert len(bucket) <= 100


@pytest.mark.parametrize(
    ("path", "kind"),
    [
        ("/health", None),
        ("/api/v1/payments/finik", None),
        ("/api/v1/admin/users", "admin"),
        ("/api/v1/admin/market/vehicle-summary", "admin"),
        ("/api/v1/market/vehicle-summary", "heavy"),
        ("/api/v1/estimate", "heavy"),
        ("/api/v1/price-trend", "heavy"),
        ("/api/v1/catalog/brands", "public"),
        ("/docs", "public"),
    ],
)
def test_route_classification(path: str, kind: str | None) -> None:
    assert classify(path) == kind


# --- middleware на настоящем приложении --------------------------------------


def test_public_route_gets_429_after_burst(client: TestClient, settings) -> None:
    settings(rate_limit_public_burst=2, rate_limit_public_per_minute=1)
    first = client.get("/api/v1/catalog/brands")
    assert first.status_code == 200
    assert first.headers["ratelimit-limit"] == "2"
    assert first.headers["ratelimit-remaining"] == "1"
    assert client.get("/api/v1/catalog/brands").status_code == 200

    refused = client.get("/api/v1/catalog/brands")
    assert refused.status_code == 429
    assert int(refused.headers["retry-after"]) >= 1
    assert refused.json()["retry_after"] >= 1
    # Заголовки безопасности и id запроса есть и на отказе.
    assert refused.headers["x-content-type-options"] == "nosniff"
    assert refused.headers["x-request-id"]


def test_health_and_webhook_are_never_rate_limited(client: TestClient, settings) -> None:
    settings(rate_limit_public_burst=1, rate_limit_public_per_minute=1)
    for _ in range(5):
        assert client.get("/health").status_code == 200
    # Webhook отвечает 400 (не JSON) — главное, что не 429: отбитый лимитом
    # платёж Finik повторит с задержкой до суток.
    for _ in range(5):
        assert client.post("/api/v1/payments/finik", content=b"x").status_code == 400


def test_rate_limit_switch_off(client: TestClient, settings) -> None:
    settings(rate_limit_enabled=False, rate_limit_public_burst=1)
    for _ in range(5):
        assert client.get("/api/v1/catalog/brands").status_code == 200


def test_heavy_global_ceiling_protects_db_from_many_ips(settings) -> None:
    from app.core.ratelimit import RateLimitMiddleware

    settings(
        rate_limit_heavy_burst=100,
        rate_limit_heavy_per_minute=100,
        rate_limit_heavy_global_per_minute=4,  # общий всплеск = 4 // 4 = 1
    )
    calls: list[int] = []

    async def inner(scope, receive, send):
        calls.append(1)
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    mw = RateLimitMiddleware(inner, bucket=TokenBucketLimiter())
    statuses: list[int] = []

    async def run(ip: str) -> None:
        async def receive():
            return {"type": "http.request", "body": b""}

        async def send(message):
            if message["type"] == "http.response.start":
                statuses.append(message["status"])

        scope = {"type": "http", "method": "GET", "path": "/api/v1/estimate", "client": (ip, 1)}
        await mw(scope, receive, send)

    import asyncio

    for i in range(3):
        asyncio.run(run(f"10.0.0.{i}"))
    assert statuses == [200, 429, 429]


def test_body_size_limit(client: TestClient, settings) -> None:
    settings(api_max_body_bytes=100)
    big = client.post("/api/v1/payments/finik", content=b"{" + b" " * 500 + b"}")
    assert big.status_code == 413


def test_security_and_cache_headers(client: TestClient) -> None:
    response = client.get("/api/v1/catalog/brands")
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert "default-src 'none'" in response.headers["content-security-policy"]
    assert response.headers["cache-control"].startswith("public, max-age=")
    assert "server-timing" in response.headers


def test_admin_and_payments_are_no_store(client: TestClient) -> None:
    assert client.post("/api/v1/payments/finik", content=b"x").headers["cache-control"] == (
        "no-store"
    )


def test_request_id_is_echoed_only_when_sane(client: TestClient) -> None:
    ok = client.get("/health", headers={"X-Request-ID": "abc12345-site"})
    assert ok.headers["x-request-id"] == "abc12345-site"
    bad = client.get("/health", headers={"X-Request-ID": "bad id\nwith newline"})
    assert bad.headers["x-request-id"] != "bad id\nwith newline"
    assert len(bad.headers["x-request-id"]) == 32


# --- кэш ответов ---------------------------------------------------------------


def test_dashboard_is_computed_once_per_ttl(client: TestClient, monkeypatch) -> None:
    calls: list[int] = []

    def fake_summary(session):
        calls.append(1)
        return {
            "total_listings": 1,
            "active_listings": 1,
            "analytics_eligible_listings": 1,
            "brand_count": 1,
            "model_count": 1,
            "average_price_usd": 1.0,
            "median_price_usd": 1.0,
            "last_seen_at": None,
        }

    from app.api.roles import require_subscriber

    monkeypatch.setattr(dashboard_api, "get_dashboard_summary", fake_summary)
    # Дашборд теперь только подписчикам — гейт здесь не предмет теста.
    app.dependency_overrides[require_subscriber] = lambda: None
    for _ in range(5):
        client.get("/api/v1/dashboard/summary")
    assert len(calls) == 1


def test_cache_disabled_with_zero_ttl() -> None:
    cache = TTLCache()
    calls: list[int] = []
    for _ in range(3):
        cache.get_or_compute(("k",), lambda: calls.append(1), ttl=0, max_entries=10)
    assert len(calls) == 3


def test_cache_expires_and_is_bounded() -> None:
    clock = FakeClock()
    cache = TTLCache(clock=clock)
    assert cache.get_or_compute(("a",), lambda: 1, ttl=10, max_entries=2) == 1
    assert cache.get_or_compute(("a",), lambda: 2, ttl=10, max_entries=2) == 1
    clock.now += 11
    assert cache.get_or_compute(("a",), lambda: 3, ttl=10, max_entries=2) == 3
    cache.get_or_compute(("b",), lambda: 0, ttl=10, max_entries=2)
    cache.get_or_compute(("c",), lambda: 0, ttl=10, max_entries=2)
    assert len(cache) == 2


def test_cache_errors_are_not_cached() -> None:
    cache = TTLCache()

    def boom():
        raise LookupError("nope")

    for _ in range(2):
        with pytest.raises(LookupError):
            cache.get_or_compute(("x",), boom, ttl=10, max_entries=10)
    assert len(cache) == 0


def test_cache_single_flight_under_concurrency() -> None:
    cache = TTLCache()
    calls: list[int] = []
    gate = threading.Event()

    def slow():
        calls.append(1)
        gate.wait(1)
        return 42

    threads = [
        threading.Thread(
            target=lambda: cache.get_or_compute(("k",), slow, ttl=10, max_entries=10)
        )
        for _ in range(8)
    ]
    for t in threads:
        t.start()
    gate.set()
    for t in threads:
        t.join()
    assert len(calls) == 1


def test_readiness_reports_db_state(client: TestClient, monkeypatch) -> None:
    import app.main as main

    class Broken:
        def connect(self):
            raise RuntimeError("db down")

    assert client.get("/health/ready").status_code in (200, 503)
    monkeypatch.setattr(main, "engine", Broken())
    down = client.get("/health/ready")
    assert down.status_code == 503
    assert down.json() == {"status": "unavailable"}
