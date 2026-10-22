from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.admin.auth import router as admin_auth_router
from app.api.admin.catalog import router as admin_catalog_router
from app.api.admin.listings import router as admin_listings_router
from app.api.admin.overview import router as admin_overview_router
from app.api.admin.users import router as admin_users_router
from app.api.catalog import router as catalog_router
from app.api.dashboard import router as dashboard_router
from app.api.market import router as market_router
from app.api.payments import router as payments_router
from app.api.routes import router as analytics_router
from app.core.config import get_settings

# Схему БД здесь НЕ трогаем: миграции делает одноразовый сервис `migrate`
# (docker-compose.yml), API стартует уже после него. Раньше init_db звали все
# три сервиса разом, и их ALTER-ы толкались за блокировку `listings`.
settings = get_settings()

app = FastAPI(
    title="Car Analytics MVP",
    version="0.1.0",
    description="Price history and market estimates for selected car brands.",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_allow_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(analytics_router)
app.include_router(dashboard_router)
app.include_router(catalog_router)
app.include_router(market_router)
app.include_router(payments_router)

# Админ-панель подключается, только когда задан её публичный адрес. Выключатель
# по умолчанию: на стенде без ADMIN_PANEL_URL ручек admin/* просто не существует
# — 404, а не «есть, но с гейтом». Меньше поверхности там, где она не нужна.
if settings.admin_panel_enabled:
    app.include_router(admin_auth_router)
    app.include_router(admin_users_router)
    app.include_router(admin_listings_router)
    app.include_router(admin_overview_router)
    app.include_router(admin_catalog_router)


@app.get("/health", tags=["system"])
def health() -> dict[str, str]:
    return {"status": "ok"}
