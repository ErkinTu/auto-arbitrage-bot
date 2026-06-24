from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.config import get_settings
from app.db.base import Base

settings = get_settings()
engine = create_engine(
    settings.database_url,
    pool_pre_ping=True,
    pool_size=10,
    max_overflow=10,
)
SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False, expire_on_commit=False)


def get_session():
    with SessionLocal() as session:
        yield session


def init_db() -> None:
    # Import models before create_all so SQLAlchemy knows every table.
    import app.models  # noqa: F401

    Base.metadata.create_all(bind=engine)
