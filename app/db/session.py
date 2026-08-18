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
        # Подписка: renewal_reminded_at — новая колонка на существующей bot_users
        # (create_all не добавляет колонки в уже созданные таблицы). Таблица
        # payments — новая, её create_all создаёт сам, ALTER не нужен.
        "ALTER TABLE bot_users ADD COLUMN IF NOT EXISTS renewal_reminded_at TIMESTAMPTZ",
        # Триал и рефералка — колонки на существующей bot_users. Таблицу
        # user_events create_all создаёт сам (она новая), ALTER для неё не нужен.
        "ALTER TABLE bot_users ADD COLUMN IF NOT EXISTS trial_started_at TIMESTAMPTZ",
        "ALTER TABLE bot_users ADD COLUMN IF NOT EXISTS trial_expires_at TIMESTAMPTZ",
        "ALTER TABLE bot_users ADD COLUMN IF NOT EXISTS trial_expired_notified_at TIMESTAMPTZ",
        "ALTER TABLE bot_users ADD COLUMN IF NOT EXISTS invite_code VARCHAR(16)",
        "ALTER TABLE bot_users ADD COLUMN IF NOT EXISTS referrer_id INTEGER REFERENCES bot_users(id) ON DELETE SET NULL",
        "ALTER TABLE bot_users ADD COLUMN IF NOT EXISTS referral_bonuses_granted INTEGER NOT NULL DEFAULT 0",
        "CREATE UNIQUE INDEX IF NOT EXISTS ix_bot_users_invite_code ON bot_users (invite_code)",
        "CREATE INDEX IF NOT EXISTS ix_bot_users_referrer_id ON bot_users (referrer_id)",
        # Чёрный список — новая таблица, её создаёт create_all; ALTER не нужен.
        # Админ-панель: is_superuser — колонка на существующей bot_users, поэтому
        # ALTER обязателен. Значения не задаём: DEFAULT FALSE — единственно
        # безопасный старт, а права выдаются вручную (`docs/ADMIN.md`). Таблицу
        # admin_login_tokens create_all создаёт сам.
        "ALTER TABLE bot_users ADD COLUMN IF NOT EXISTS is_superuser BOOLEAN NOT NULL DEFAULT FALSE",
        # Тарифная сетка (4 плана): payments.plan_code — колонка на существующей
        # таблице, поэтому ALTER обязателен. Таблицу access_blocks (вместе с её
        # partial unique index по payment_id) создаёт create_all.
        "ALTER TABLE payments ADD COLUMN IF NOT EXISTS plan_code VARCHAR(20)",
        # Бэкфилл ОБЯЗАТЕЛЕН в том же релизе: NULL читается как 'month_full', но
        # доступ, выданный ДО тарифов, никакого блока не имеет — а без блока
        # `effective_tier` честно отвечает 'delayed' (fail-safe по умолчанию), и
        # заплатившие 2000 сом за полную скорость наутро получили бы отставание.
        "UPDATE payments SET plan_code = 'month_full' WHERE plan_code IS NULL",
        # Идемпотентно по NOT EXISTS, поэтому живёт здесь, а не ручным шагом на
        # сервере. Побочный эффект осознанный: реферальные и CLI-дни, выданные до
        # тарифов, тоже станут полными — их немного, а отбирать выданное задним
        # числом хуже.
        """
        INSERT INTO access_blocks (user_id, tier, starts_at, ends_at, origin)
        SELECT u.id, 'full', now(), u.subscription_expires_at, 'backfill'
          FROM bot_users u
         WHERE u.subscription_expires_at > now()
           AND NOT EXISTS (SELECT 1 FROM access_blocks b WHERE b.user_id = u.id)
        """,
    )
    with engine.begin() as connection:
        for statement in statements:
            connection.execute(text(statement))


def init_db() -> None:
    # Import models before create_all so SQLAlchemy knows every table.
    import app.models  # noqa: F401

    Base.metadata.create_all(bind=engine)
    _upgrade_postgres_schema()
