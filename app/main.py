from contextlib import asynccontextmanager

from fastapi import FastAPI, Response
from sqlalchemy import event, text
from fastapi.middleware.cors import CORSMiddleware

from app.api.admin.auth import router as admin_auth_router
from app.api.admin.catalog import router as admin_catalog_router
from app.api.admin.listings import router as admin_listings_router
from app.api.admin.overview import router as admin_overview_router
from app.api.admin.users import router as admin_users_router
from app.api.auth import router as auth_router
from app.api.catalog import router as catalog_router
from app.api.dashboard import router as dashboard_router
from app.api.market import router as market_router
from app.api.payments import router as payments_router
from app.api.public.estimate import router as public_estimate_router
from app.api.public.feed import router as public_feed_router
from app.api.routes import router as analytics_router
from app.core.config import get_settings
from app.core.http import BodySizeLimitMiddleware, SecurityHeadersMiddleware
from app.core.logging_setup import install_token_filter
from app.core.ratelimit import RateLimitMiddleware
from app.db.session import engine

# Схему БД здесь НЕ трогаем: миграции делает одноразовый сервис `migrate`
# (docker-compose.yml), API стартует уже после него. Раньше init_db звали все
# три сервиса разом, и их ALTER-ы толкались за блокировку `listings`.
settings = get_settings()

# Потолок SQL-запроса — только для соединений этого процесса (см. настройку).
if engine.dialect.name == "postgresql" and settings.api_statement_timeout_ms > 0:

    @event.listens_for(engine, "connect")
    def _set_statement_timeout(dbapi_connection, _record) -> None:  # pragma: no cover
        with dbapi_connection.cursor() as cursor:
            cursor.execute(f"SET statement_timeout = {int(settings.api_statement_timeout_ms)}")
        # psycopg открывает транзакцию на SET; закрываем, чтобы соединение
        # вернулось в пул чистым.
        dbapi_connection.commit()


# Токен бота уходит в URL запросов к Telegram (вебхук оплаты) — не в логи.
install_token_filter()

@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Прогрев кэша главной сайта (services/warmup.py); в тестах выключен conftest-ом.
    from app.services.warmup import start_warmup

    stop = start_warmup()
    try:
        yield
    finally:
        if stop is not None:
            stop.set()


app = FastAPI(
    lifespan=lifespan,
    title="Car Analytics MVP",
    version="0.1.0",
    description="Price history and market estimates for selected car brands.",
    # Схема API — карта всех ручек. На проде выключаем (`API_DOCS_ENABLED=false`).
    docs_url="/docs" if settings.api_docs_enabled else None,
    redoc_url="/redoc" if settings.api_docs_enabled else None,
    openapi_url="/openapi.json" if settings.api_docs_enabled else None,
)

# Порядок: последний добавленный — внешний. Снаружи внутрь: заголовки и
# X-Request-ID (видны даже на 429/413) → лимит частоты (отказ до чтения тела и
# до базы) → потолок тела → CORS → ручки.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_allow_origins,
    allow_credentials=True,
    # Публичный API только читает; POST — вход/выход админки и webhook.
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type", "X-Request-ID"],
    expose_headers=[
        "X-Request-ID",
        "Retry-After",
        "RateLimit-Limit",
        "RateLimit-Remaining",
        "RateLimit-Reset",
    ],
    max_age=600,
)
app.add_middleware(BodySizeLimitMiddleware)
app.add_middleware(RateLimitMiddleware)
app.add_middleware(SecurityHeadersMiddleware)

app.include_router(analytics_router)
app.include_router(dashboard_router)
app.include_router(catalog_router)
app.include_router(market_router)
app.include_router(payments_router)
# Сайт: /api/v1/public/* — лента, карточка, избранное (только чтение, без ПДн).
app.include_router(public_feed_router)
app.include_router(public_estimate_router)
# Вход на сайт через бота и роль посетителя (guest/user/subscriber/admin).
app.include_router(auth_router)

# Админ-панель подключается, только когда задан её публичный адрес. Выключатель
# по умолчанию: на стенде без ADMIN_PANEL_URL ручек admin/* просто не существует
# — 404, а не «есть, но с гейтом». Меньше поверхности там, где она не нужна.
# Админские ручки нужны и старой панели, и разделу /admin сайта.
if settings.admin_panel_enabled or settings.site_url:
    app.include_router(admin_auth_router)
    app.include_router(admin_users_router)
    app.include_router(admin_listings_router)
    app.include_router(admin_overview_router)
    app.include_router(admin_catalog_router)


@app.get("/health", tags=["system"])
def health() -> dict[str, str]:
    """Liveness: процесс жив. Базу не трогает — иначе рестарт базы перезапускал
    бы и api, хотя чинить в нём нечего."""
    return {"status": "ok"}


@app.get("/health/ready", tags=["system"])
def ready(response: Response) -> dict[str, str]:
    """Readiness: api готов отвечать данными (база отвечает). Для балансировщика
    и мониторинга: 503 = не слать трафик, но и не перезапускать."""
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception:  # noqa: BLE001 — любая ошибка базы = не готов
        response.status_code = 503
        return {"status": "unavailable"}
    return {"status": "ok"}
