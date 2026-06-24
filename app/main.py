from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.routes import router
from app.db.session import init_db


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="Car Analytics MVP",
    version="0.1.0",
    description="Price history and market estimates for selected car brands.",
    lifespan=lifespan,
)
app.include_router(router)


@app.get("/health", tags=["system"])
def health() -> dict[str, str]:
    return {"status": "ok"}
