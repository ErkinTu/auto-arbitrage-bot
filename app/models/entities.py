from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Source(Base, TimestampMixin):
    __tablename__ = "sources"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    base_url: Mapped[str | None] = mapped_column(Text)


class Brand(Base, TimestampMixin):
    __tablename__ = "brands"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True, nullable=False, index=True)
    slug: Mapped[str | None] = mapped_column(String(120), unique=True)


class CarModel(Base, TimestampMixin):
    __tablename__ = "car_models"
    __table_args__ = (UniqueConstraint("brand_id", "name", name="uq_car_model_brand_name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    brand_id: Mapped[int] = mapped_column(ForeignKey("brands.id"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    slug: Mapped[str | None] = mapped_column(String(150))


class Generation(Base, TimestampMixin):
    __tablename__ = "generations"
    __table_args__ = (UniqueConstraint("car_model_id", "name", name="uq_generation_model_name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    car_model_id: Mapped[int] = mapped_column(ForeignKey("car_models.id"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(160), nullable=False, index=True)
    code: Mapped[str | None] = mapped_column(String(80))
    is_facelift: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class ModelAlias(Base, TimestampMixin):
    __tablename__ = "model_aliases"
    __table_args__ = (
        UniqueConstraint("brand_id", "normalized_alias", name="uq_model_alias_brand_normalized"),
        Index("ix_model_alias_model", "car_model_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    brand_id: Mapped[int] = mapped_column(ForeignKey("brands.id", ondelete="CASCADE"), nullable=False)
    car_model_id: Mapped[int] = mapped_column(ForeignKey("car_models.id", ondelete="CASCADE"), nullable=False)
    alias: Mapped[str] = mapped_column(String(160), nullable=False)
    normalized_alias: Mapped[str] = mapped_column(String(180), nullable=False)
    source: Mapped[str] = mapped_column(String(30), default="manual", nullable=False)


class GenerationAlias(Base, TimestampMixin):
    __tablename__ = "generation_aliases"
    __table_args__ = (
        UniqueConstraint("car_model_id", "normalized_alias", name="uq_generation_alias_model_normalized"),
        Index("ix_generation_alias_generation", "generation_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    car_model_id: Mapped[int] = mapped_column(ForeignKey("car_models.id", ondelete="CASCADE"), nullable=False)
    generation_id: Mapped[int] = mapped_column(ForeignKey("generations.id", ondelete="CASCADE"), nullable=False)
    alias: Mapped[str] = mapped_column(String(200), nullable=False)
    normalized_alias: Mapped[str] = mapped_column(String(220), nullable=False)
    source: Mapped[str] = mapped_column(String(30), default="manual", nullable=False)


class NormalizationIssue(Base, TimestampMixin):
    __tablename__ = "normalization_issues"
    __table_args__ = (
        UniqueConstraint("brand_id", "normalized_source_model", name="uq_normalization_issue_brand_model"),
        Index("ix_normalization_issue_status_count", "status", "occurrence_count"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    brand_id: Mapped[int] = mapped_column(ForeignKey("brands.id", ondelete="CASCADE"), nullable=False)
    resolved_model_id: Mapped[int | None] = mapped_column(ForeignKey("car_models.id", ondelete="SET NULL"))
    source_model_text: Mapped[str] = mapped_column(String(160), nullable=False)
    normalized_source_model: Mapped[str] = mapped_column(String(180), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="open", nullable=False)
    occurrence_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


class Listing(Base, TimestampMixin):
    __tablename__ = "listings"
    __table_args__ = (
        UniqueConstraint("source_id", "external_id", name="uq_listing_source_external"),
        Index("ix_listing_similarity", "brand_id", "car_model_id", "generation_id", "year"),
        Index("ix_listing_active_price", "is_active", "current_price_usd"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"), nullable=False, index=True)
    external_id: Mapped[str] = mapped_column(String(100), nullable=False)

    brand_id: Mapped[int | None] = mapped_column(ForeignKey("brands.id"), index=True)
    car_model_id: Mapped[int | None] = mapped_column(ForeignKey("car_models.id"), index=True)
    generation_id: Mapped[int | None] = mapped_column(ForeignKey("generations.id"), index=True)

    title: Mapped[str | None] = mapped_column(Text)
    source_slug: Mapped[str | None] = mapped_column(String(255))
    source_make_text: Mapped[str | None] = mapped_column(String(100))
    source_model_text: Mapped[str | None] = mapped_column(String(160))
    source_generation_text: Mapped[str | None] = mapped_column(String(200))
    normalization_status: Mapped[str] = mapped_column(
        String(40), default="pending_catalog_resolution", nullable=False, index=True
    )
    normalization_confidence: Mapped[str | None] = mapped_column(String(40))
    quality_flags: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    analytics_eligible: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)

    year: Mapped[int | None] = mapped_column(Integer, index=True)
    mileage_km: Mapped[int | None] = mapped_column(Integer, index=True)
    body_type: Mapped[str | None] = mapped_column(String(80))
    engine_volume: Mapped[Decimal | None] = mapped_column(Numeric(4, 1))
    fuel_type: Mapped[str | None] = mapped_column(String(80))
    gearbox: Mapped[str | None] = mapped_column(String(80))
    drive_type: Mapped[str | None] = mapped_column(String(80))
    steering_wheel: Mapped[str | None] = mapped_column(String(40))
    city: Mapped[str | None] = mapped_column(String(80), index=True)
    customs_status: Mapped[str] = mapped_column(String(30), default="unknown", nullable=False, index=True)
    seller_type: Mapped[str | None] = mapped_column(String(30))
    is_verified_seller: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_official_dealer: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    current_price_amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    current_price_currency: Mapped[str | None] = mapped_column(String(10))
    current_price_kgs: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    current_price_usd: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))

    status: Mapped[str] = mapped_column(String(30), default="active", nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, index=True)
    source_created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source_upped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    deactivated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    missing_full_scans: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)


class ListingPriceHistory(Base):
    __tablename__ = "listing_price_history"
    __table_args__ = (Index("ix_price_history_listing_observed", "listing_id", "observed_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    listing_id: Mapped[int] = mapped_column(ForeignKey("listings.id", ondelete="CASCADE"), nullable=False)
    event_type: Mapped[str] = mapped_column(String(30), nullable=False)  # initial | price_changed
    price_amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    price_currency: Mapped[str | None] = mapped_column(String(10))
    price_kgs: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    price_usd: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), index=True)
    mileage_km_snapshot: Mapped[int | None] = mapped_column(Integer)
    customs_status_snapshot: Mapped[str] = mapped_column(String(30), default="unknown", nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


class ListingStatusHistory(Base):
    __tablename__ = "listing_status_history"
    __table_args__ = (Index("ix_status_history_listing_observed", "listing_id", "observed_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    listing_id: Mapped[int] = mapped_column(ForeignKey("listings.id", ondelete="CASCADE"), nullable=False)
    event_type: Mapped[str] = mapped_column(String(30), nullable=False)
    old_status: Mapped[str | None] = mapped_column(String(30))
    new_status: Mapped[str | None] = mapped_column(String(30))
    details: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


class SyncTarget(Base, TimestampMixin):
    __tablename__ = "sync_targets"
    __table_args__ = (
        UniqueConstraint("source_id", "brand_filter", "model_filter", "generation_filter", name="uq_sync_target"),
        Index("ix_sync_target_due", "enabled", "priority", "last_success_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"), nullable=False, index=True)
    brand_filter: Mapped[str] = mapped_column(String(100), nullable=False)
    model_filter: Mapped[str | None] = mapped_column(String(120))
    generation_filter: Mapped[str | None] = mapped_column(String(160))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    priority: Mapped[int] = mapped_column(Integer, default=100, nullable=False)
    fresh_period: Mapped[str] = mapped_column(String(20), default="1d", nullable=False)
    page_size: Mapped[int] = mapped_column(Integer, default=100, nullable=False)
    max_pages: Mapped[int] = mapped_column(Integer, default=5, nullable=False)
    max_items: Mapped[int] = mapped_column(Integer, default=500, nullable=False)
    sync_interval_minutes: Mapped[int] = mapped_column(Integer, default=60, nullable=False)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_full_scan_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ParserRun(Base):
    __tablename__ = "parser_runs"
    __table_args__ = (Index("ix_parser_run_source_started", "source_id", "started_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"), nullable=False)
    target_id: Mapped[int | None] = mapped_column(ForeignKey("sync_targets.id"))
    mode: Mapped[str] = mapped_column(String(20), nullable=False)  # fresh | full
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(20), default="running", nullable=False)
    new_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    updated_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    price_changed_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    reactivated_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    deactivated_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    skipped_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    processed_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error_message: Mapped[str | None] = mapped_column(Text)


class RawPayload(Base):
    __tablename__ = "raw_payloads"
    __table_args__ = (Index("ix_raw_payload_source_external", "source_id", "external_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"), nullable=False)
    listing_id: Mapped[int | None] = mapped_column(ForeignKey("listings.id", ondelete="CASCADE"))
    parser_run_id: Mapped[int | None] = mapped_column(ForeignKey("parser_runs.id", ondelete="SET NULL"))
    external_id: Mapped[str] = mapped_column(String(100), nullable=False)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class DailyMarketStat(Base):
    __tablename__ = "daily_market_stats"
    __table_args__ = (
        UniqueConstraint(
            "stat_date",
            "brand_id",
            "car_model_id",
            "generation_id",
            "year",
            "mileage_bucket",
            "customs_status",
            name="uq_daily_market_stat",
        ),
        Index("ix_daily_stat_vehicle_date", "brand_id", "car_model_id", "generation_id", "year", "stat_date"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    stat_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    brand_id: Mapped[int] = mapped_column(ForeignKey("brands.id"), nullable=False)
    car_model_id: Mapped[int] = mapped_column(ForeignKey("car_models.id"), nullable=False)
    generation_id: Mapped[int | None] = mapped_column(ForeignKey("generations.id"))
    year: Mapped[int | None] = mapped_column(Integer)
    mileage_bucket: Mapped[str] = mapped_column(String(30), nullable=False)
    customs_status: Mapped[str] = mapped_column(String(30), nullable=False)
    avg_price_usd: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    median_price_usd: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    min_price_usd: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    max_price_usd: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    sample_count: Mapped[int] = mapped_column(Integer, nullable=False)


class ExchangeRate(Base, TimestampMixin):
    """Официальный дневной курс НБКР: сколько KGS стоит 1 единица валюты."""

    __tablename__ = "exchange_rates"
    __table_args__ = (UniqueConstraint("currency", "rate_date", name="uq_exchange_rate_currency_date"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    currency: Mapped[str] = mapped_column(String(10), nullable=False, index=True)
    rate_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    rate_kgs: Mapped[Decimal] = mapped_column(Numeric(12, 4), nullable=False)


class BotUser(Base, TimestampMixin):
    __tablename__ = "bot_users"

    id: Mapped[int] = mapped_column(primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False, index=True)
    username: Mapped[str | None] = mapped_column(String(64))
    first_name: Mapped[str | None] = mapped_column(String(120))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    access_status: Mapped[str] = mapped_column(String(20), default="member", nullable=False)  # member | subscriber | trial | revoked
    subscription_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_access_check_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Когда юзеру ушло напоминание о продлении текущей подписки (чтобы не слать
    # повторно за один период). Сбрасывается в NULL при успешной оплате.
    renewal_reminded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # --- Бесплатный триал ---------------------------------------------------
    # Срок триала держим ОТДЕЛЬНОЙ колонкой, а не в subscription_expires_at:
    # иначе бесплатный доступ неотличим от оплаченного (метрики воронки врут,
    # напоминание о продлении уходит тем, кто ничего не покупал), а платёж
    # поверх триала пришлось бы отличать по «а платил ли он когда-нибудь».
    # trial_started_at — метка «триал уже выдавали» (одноразовость на telegram_id);
    # обнулять её нельзя, даже когда триал истёк.
    trial_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    trial_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Пейвол-сообщение об окончании триала — ровно одно на пользователя.
    trial_expired_notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # --- Рефералка ----------------------------------------------------------
    # Код в deep-link t.me/<bot>?start=ref_<code>; выдаётся лениво, при первом
    # /invite (у существующей базы иначе понадобился бы бэкфилл).
    invite_code: Mapped[str | None] = mapped_column(String(16), unique=True, index=True)
    referrer_id: Mapped[int | None] = mapped_column(
        ForeignKey("bot_users.id", ondelete="SET NULL"), index=True
    )
    # Сколько бонусов уже начислено (1 бонус = REFERRAL_BONUS_DAYS дней за
    # REFERRAL_FRIENDS_PER_BONUS засчитанных друзей). Счётчик, а не флаг:
    # начисление идемпотентно — сравниваем с числом заслуженных бонусов.
    referral_bonuses_granted: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class UserEvent(Base):
    """Лог событий воронки: старт → триал → оплата.

    `bot_users` хранит только текущее состояние, поэтому «отвал по дням» по нему
    не построить — нужен именно журнал переходов. Пишется из бота и из webhook,
    читается CLI-командой `funnel`.
    """

    __tablename__ = "user_events"
    __table_args__ = (Index("ix_user_event_event_created", "event", "created_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("bot_users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    event: Mapped[str] = mapped_column(String(32), nullable=False)
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )


class UserFilter(Base, TimestampMixin):
    __tablename__ = "user_filters"
    __table_args__ = (Index("ix_user_filter_enabled_brand", "enabled", "brand_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("bot_users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    brand_id: Mapped[int | None] = mapped_column(ForeignKey("brands.id"))  # NULL = любая марка
    car_model_id: Mapped[int | None] = mapped_column(ForeignKey("car_models.id"))  # NULL = любая модель
    year_from: Mapped[int | None] = mapped_column(Integer)
    year_to: Mapped[int | None] = mapped_column(Integer)
    mileage_max_km: Mapped[int | None] = mapped_column(Integer)
    price_max_usd: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    below_market_only: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # Порог «ниже рынка» в процентах (5/10/15/20); NULL = глобальный BOT_BELOW_AVG_FACTOR.
    below_market_percent: Mapped[int | None] = mapped_column(Integer)
    # Пропускать зарубежные объявления (city = страна: Китай, США...) — там цены без растаможки.
    # Legacy: до региональных фильтров. Новые фильтры вместо этого задают region.
    skip_foreign: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # Регион объявления (см. app/services/regions.py): код области/города/'foreign'.
    # NULL = «Все авто» (без ограничения по географии).
    region: Mapped[str | None] = mapped_column(String(16))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class SentNotification(Base):
    __tablename__ = "sent_notifications"
    __table_args__ = (
        UniqueConstraint("user_id", "listing_id", name="uq_sent_notification_user_listing"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("bot_users.id", ondelete="CASCADE"), nullable=False)
    listing_id: Mapped[int] = mapped_column(ForeignKey("listings.id", ondelete="CASCADE"), nullable=False)
    filter_id: Mapped[int | None] = mapped_column(ForeignKey("user_filters.id", ondelete="SET NULL"))
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Payment(Base, TimestampMixin):
    """Разовый платёж за подписку через Finik (finik.kg, Web SDK).

    Один ряд на попытку оплаты. `order_id` уходит в Finik как `PaymentId` (UUID)
    и возвращается в webhook в `fields.paymentId` — по нему находим платёж и
    пользователя. `finik_transaction_id` = `transactionId` из webhook (номер на
    стороне Finik, для сверок с их поддержкой). Статус двигают init_payment
    (pending) и webhook (paid); `expired` ставит джанитор в worker, потому что об
    неуспешных оплатах Finik не сообщает вовсе.
    """

    __tablename__ = "payments"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("bot_users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    order_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    finik_transaction_id: Mapped[str | None] = mapped_column(String(64))
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(10), default="KGS", nullable=False)
    period_days: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), default="pending", nullable=False, index=True
    )  # pending | paid | expired
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    raw_callback: Mapped[dict[str, Any] | None] = mapped_column(JSON)
