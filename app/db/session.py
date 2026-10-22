import logging
import re
import time

from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker

from app.core.config import get_settings
from app.db.base import Base

logger = logging.getLogger(__name__)

settings = get_settings()


def _connect_args() -> dict:
    """Серверные таймауты Postgres на каждое соединение.

    `idle_in_transaction_session_timeout` — страховка от «забытой» транзакции:
    21.10.2026 выкатка повисла на ALTER TABLE listings, потому что чья-то
    транзакция стояла открытой, а ждущий ALTER, в свою очередь, блокировал все
    запросы к таблице. Сервер сам рвёт такую сессию через N секунд.
    """
    if not settings.database_url.startswith("postgresql"):
        return {}
    timeout_ms = settings.db_idle_in_transaction_timeout_seconds * 1000
    if timeout_ms <= 0:
        return {}
    return {"options": f"-c idle_in_transaction_session_timeout={timeout_ms}"}


engine = create_engine(
    settings.database_url,
    pool_pre_ping=True,
    pool_size=10,
    max_overflow=10,
    connect_args=_connect_args(),
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
        # безопасный старт, а права выдаются вручную (`docs/admin/ADMIN.md`). Таблицу
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
        # Пресеты фильтров (services/filter_presets.py): группа марок и «не старше
        # N лет» — колонки на существующей user_filters.
        "ALTER TABLE user_filters ADD COLUMN IF NOT EXISTS brand_group VARCHAR(16)",
        "ALTER TABLE user_filters ADD COLUMN IF NOT EXISTS max_age_years INTEGER",
        # Итоги триала: % ниже рынка на момент отправки.
        "ALTER TABLE sent_notifications ADD COLUMN IF NOT EXISTS below_market_percent NUMERIC(5, 1)",
        # Карточка с фото: главное фото объявления. raw_payloads галерею не хранят
        # (safe_payload её отбрасывает), поэтому бэкфилла нет — колонку заполняют
        # парсеры при следующей встрече объявления.
        "ALTER TABLE listings ADD COLUMN IF NOT EXISTS image_url TEXT",
    )
    for statement in statements:
        _apply(statement)


# `ALTER TABLE t ADD COLUMN IF NOT EXISTS c ...` / `CREATE [UNIQUE] INDEX IF NOT
# EXISTS i ...` — разбираем, чтобы проверить наличие ДО запроса блокировки.
_ADD_COLUMN_RE = re.compile(
    r"^\s*ALTER TABLE (\w+) ADD COLUMN IF NOT EXISTS (\w+)", re.IGNORECASE
)
_DROP_NOT_NULL_RE = re.compile(
    r"^\s*ALTER TABLE (\w+) ALTER COLUMN (\w+) DROP NOT NULL", re.IGNORECASE
)
_CREATE_INDEX_RE = re.compile(
    r"^\s*CREATE (?:UNIQUE )?INDEX IF NOT EXISTS (\w+)", re.IGNORECASE
)

# Сколько ждать блокировку на DDL и сколько раз повторить. Пять секунд — с
# запасом для обычного запроса, но мало, чтобы очередь за ждущим ALTER успела
# парализовать бота и API.
DDL_LOCK_TIMEOUT = "5s"
DDL_ATTEMPTS = 6
DDL_RETRY_PAUSE_SECONDS = 5.0


def _already_applied(connection, statement: str) -> bool:
    """Нужен ли DDL вообще — по каталогу, без блокировки таблицы.

    `IF NOT EXISTS` сам по себе не спасает: Postgres берёт `ACCESS EXCLUSIVE`
    ДО проверки наличия колонки. Любая долгая транзакция на `listings` держит
    этот ALTER, а он — все запросы к таблице за собой (выкатка 21.10.2026).
    Чтение information_schema/pg_indexes блокировок на таблицу не берёт.
    """
    if match := _ADD_COLUMN_RE.match(statement):
        table, column = match.groups()
        return (
            connection.execute(
                text(
                    "SELECT 1 FROM information_schema.columns "
                    "WHERE table_schema = current_schema() "
                    "AND table_name = :table AND column_name = :column"
                ),
                {"table": table, "column": column},
            ).first()
            is not None
        )
    if match := _DROP_NOT_NULL_RE.match(statement):
        table, column = match.groups()
        return (
            connection.execute(
                text(
                    "SELECT 1 FROM information_schema.columns "
                    "WHERE table_schema = current_schema() "
                    "AND table_name = :table AND column_name = :column "
                    "AND is_nullable = 'YES'"
                ),
                {"table": table, "column": column},
            ).first()
            is not None
        )
    if match := _CREATE_INDEX_RE.match(statement):
        return (
            connection.execute(
                text(
                    "SELECT 1 FROM pg_indexes "
                    "WHERE schemaname = current_schema() AND indexname = :name"
                ),
                {"name": match.group(1)},
            ).first()
            is not None
        )
    return False  # DML-бэкфиллы идемпотентны сами и таблицу целиком не блокируют


def _apply(statement: str) -> None:
    """Один шаг апгрейда в своей транзакции, с `lock_timeout` и повтором.

    Не дождались блокировки за DDL_LOCK_TIMEOUT — откатываемся и пробуем снова:
    короткое ожидание не даёт ждущему DDL собрать очередь из всех запросов.
    """
    for attempt in range(1, DDL_ATTEMPTS + 1):
        try:
            with engine.begin() as connection:
                if _already_applied(connection, statement):
                    return
                connection.execute(text(f"SET LOCAL lock_timeout = '{DDL_LOCK_TIMEOUT}'"))
                connection.execute(text(statement))
                return
        except OperationalError as exc:
            # 55P03 lock_not_available — таблицу держит чужая транзакция.
            if getattr(exc.orig, "sqlstate", None) != "55P03" or attempt == DDL_ATTEMPTS:
                raise
            logger.warning(
                "schema upgrade: lock timeout (attempt %s/%s), retrying: %s",
                attempt,
                DDL_ATTEMPTS,
                " ".join(statement.split())[:120],
            )
            time.sleep(DDL_RETRY_PAUSE_SECONDS)


def init_db() -> None:
    # Import models before create_all so SQLAlchemy knows every table.
    import app.models  # noqa: F401

    Base.metadata.create_all(bind=engine)
    _upgrade_postgres_schema()
