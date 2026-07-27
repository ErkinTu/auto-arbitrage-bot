from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.catalog import router as catalog_router
from app.api.dashboard import router as dashboard_router
from app.api.market import router as market_router
from app.api.payments import router as payments_router
from app.api.routes import router as analytics_router
from app.core.config import get_settings
from app.db.session import init_db


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    yield


settings = get_settings()

app = FastAPI(
    title="Car Analytics MVP",
    version="0.1.0",
    description="Price history and market estimates for selected car brands.",
    lifespan=lifespan,
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


@app.get("/health", tags=["system"])
def health() -> dict[str, str]:
    return {"status": "ok"}
