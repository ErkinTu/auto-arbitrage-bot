from sqlalchemy import create_engine, text
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


def _upgrade_postgres_schema() -> None:
    """Small idempotent upgrade path for the MVP before Alembic is introduced."""
    if engine.dialect.name != "postgresql":
        return
    statements = (
        "ALTER TABLE listings ADD COLUMN IF NOT EXISTS source_make_text VARCHAR(100)",
        "ALTER TABLE listings ADD COLUMN IF NOT EXISTS source_model_text VARCHAR(160)",
        "ALTER TABLE listings ADD COLUMN IF NOT EXISTS source_generation_text VARCHAR(200)",
        "ALTER TABLE listings ADD COLUMN IF NOT EXISTS normalization_status VARCHAR(40) NOT NULL DEFAULT 'pending_catalog_resolution'",
        "ALTER TABLE listings ADD COLUMN IF NOT EXISTS normalization_confidence VARCHAR(40)",
        "ALTER TABLE listings ADD COLUMN IF NOT EXISTS quality_flags JSON NOT NULL DEFAULT '[]'::json",
        "ALTER TABLE listings ADD COLUMN IF NOT EXISTS analytics_eligible BOOLEAN NOT NULL DEFAULT FALSE",
        "CREATE INDEX IF NOT EXISTS ix_listing_normalization_status ON listings (normalization_status)",
        "CREATE INDEX IF NOT EXISTS ix_listing_analytics_eligible ON listings (analytics_eligible)",
        # NULL brand_id в фильтре бота = "все марки".
        "ALTER TABLE user_filters ALTER COLUMN brand_id DROP NOT NULL",
    )
    with engine.begin() as connection:
        for statement in statements:
            connection.execute(text(statement))


def init_db() -> None:
    # Import models before create_all so SQLAlchemy knows every table.
    import app.models  # noqa: F401

    Base.metadata.create_all(bind=engine)
    _upgrade_postgres_schema()
