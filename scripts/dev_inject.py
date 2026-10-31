"""ТОЛЬКО ЛОКАЛЬНО: тестовые «новые» объявления для проверки рассылки без парса.

Клонирует подходящие пользователю объявления из локальной базы в новые строки
`devtest-*` с публикацией «сейчас» и событием `new` (или `price_changed`) —
следующий цикл бота разошлёт их как свежие. Отбор теми же функциями, что у
рассылки (фильтры пользователя, «ниже рынка», полнота, чёрный список), так что
что впрыснули — то бот и обязан прислать (с поправкой на потолок за цикл).

Клоны: `analytics_eligible=false` (не попадают в статистику и витрину), ссылка
— на настоящее объявление (тот же slug). Убрать: `--cleanup`.

    PYTHONPATH=. uv run python scripts/dev_inject.py --telegram-id 7229265363 --count 10
    ... --count 3 --photo none        # без фото — текстовые карточки
    ... --count 3 --photo broken      # битая ссылка — откат на текст
    ... --count 6 --brand Toyota      # одна марка — для дайджеста
    ... --count 60                    # больше потолка 50 за цикл
    ... --max-percent 35              # без «мусорных» скидок: реалистичная пачка
    ... --min-year 2012 --min-price 6000   # «витринная» пачка для показа заказчику
    ... --cleanup
"""

from __future__ import annotations

import argparse
import sys
import uuid

from sqlalchemy import delete, select

from app.bot.matching import (
    filter_matches,
    is_complete_listing,
    is_plausible_discount,
    passes_below_market,
)
from app.bot.showcase import market_estimates
from app.core.config import get_settings
from app.db.session import SessionLocal
from app.models import Brand, BotUser, Listing, ListingStatusHistory, SentNotification, UserFilter
from app.services.blocklist import load_block_sets
from app.services.filter_presets import load_brand_groups
from app.services.normalizer import MIN_PLAUSIBLE_PRICE_USD
from app.services.regions import is_foreign
from app.services.sync import utcnow

PREFIX = "devtest-"
BROKEN_PHOTO = "https://img5.lalafo.com/i/posters/original/00/00/00/devtest-broken-photo.jpeg"
POOL_LIMIT = 20_000

# Колонки, которые клон берёт у оригинала как есть.
COPIED = (
    "source_id", "brand_id", "car_model_id", "generation_id", "title", "source_slug",
    "source_make_text", "source_model_text", "source_generation_text",
    "normalization_status", "normalization_confidence", "year", "mileage_km",
    "body_type", "engine_volume", "fuel_type", "gearbox", "drive_type",
    "steering_wheel", "city", "customs_status", "seller_type", "current_price_amount",
    "current_price_currency", "current_price_kgs", "current_price_usd", "image_url",
)


def _guard() -> None:
    url = get_settings().database_url
    if "localhost" not in url and "127.0.0.1" not in url:
        sys.exit(f"Отказ: DATABASE_URL не локальный ({url.split('@')[-1]}). Скрипт только для локальной базы.")


def cleanup() -> None:
    with SessionLocal() as session:
        ids = list(session.scalars(select(Listing.id).where(Listing.external_id.like(f"{PREFIX}%"))))
        if ids:
            session.execute(delete(Listing).where(Listing.id.in_(ids)))
            session.commit()
        print(f"Удалено тестовых объявлений: {len(ids)} (с их уведомлениями и историей)")


def inject(args: argparse.Namespace) -> None:
    settings = get_settings()
    now = utcnow()
    with SessionLocal() as session:
        user = session.scalar(select(BotUser).where(BotUser.telegram_id == args.telegram_id))
        if user is None:
            sys.exit("Пользователя нет в базе — сначала /start в боте.")
        filters = list(
            session.scalars(
                select(UserFilter).where(UserFilter.user_id == user.id, UserFilter.enabled.is_(True))
            )
        )
        if not filters:
            sys.exit("У пользователя нет фильтров — бот ему ничего не пришлёт. Включите триал или /add_filter.")

        query = select(Listing).where(
            Listing.is_active.is_(True),
            ~Listing.external_id.like(f"{PREFIX}%"),
            Listing.current_price_usd >= MIN_PLAUSIBLE_PRICE_USD,
            Listing.brand_id.is_not(None),
            Listing.car_model_id.is_not(None),
        )
        if args.photo == "keep":
            query = query.where(Listing.image_url.is_not(None))
        if args.brand:
            brand = session.scalar(select(Brand).where(Brand.name == args.brand))
            if brand is None:
                sys.exit(f"Марки {args.brand!r} нет в каталоге.")
            query = query.where(Listing.brand_id == brand.id)
        pool = [
            listing
            for listing in session.scalars(query.order_by(Listing.last_seen_at.desc()).limit(POOL_LIMIT))
            if is_complete_listing(listing, now) and not is_foreign(listing.city)
        ]

        # Машины, которые человек уже получал (в том числе клонами), не повторяем:
        # для него клон выглядит как та же самая машина второй раз.
        already = set(
            session.scalars(
                select(Listing.source_slug)
                .join(SentNotification, SentNotification.listing_id == Listing.id)
                .where(SentNotification.user_id == user.id)
            )
        ) | set(
            session.scalars(
                select(Listing.source_slug).where(Listing.external_id.like(f"{PREFIX}%"))
            )
        )
        brand_groups = load_brand_groups(session)
        blocked = load_block_sets(session, {user.id}).get(user.id)
        scored: list[tuple[float, Listing]] = []
        for listing, estimate in market_estimates(
            session, pool, min_samples=settings.bot_below_avg_min_samples
        ).items():
            if listing.source_slug in already or (blocked is not None and blocked.blocks(listing)):
                continue
            avg = estimate.avg_usd if estimate is not None else None
            below = (1 - float(listing.current_price_usd) / avg) * 100 if avg else None
            if not is_plausible_discount(below, settings.bot_max_below_market_percent):
                continue
            if args.max_percent and below is not None and below > args.max_percent:
                continue
            if args.min_year and (listing.year or 0) < args.min_year:
                continue
            if args.min_price and float(listing.current_price_usd) < args.min_price:
                continue
            if any(
                filter_matches(listing, item, brand_groups, now=now)
                and passes_below_market(listing, item, avg, factor=settings.bot_below_avg_factor)
                for item in filters
            ):
                scored.append((below or 0.0, listing))
        scored.sort(key=lambda pair: pair[0], reverse=True)
        chosen = scored[: args.count]
        if len(chosen) < args.count:
            print(f"Внимание: подходящих нашлось только {len(chosen)} из {args.count}")

        for below, original in chosen:
            clone = Listing(
                external_id=f"{PREFIX}{original.id}-{uuid.uuid4().hex[:6]}",
                **{name: getattr(original, name) for name in COPIED},
                quality_flags=[],
                analytics_eligible=False,
                status="active",
                is_active=True,
                source_created_at=now,
                first_seen_at=now,
                last_seen_at=now,
                payload_hash="devtest",
            )
            if args.photo == "none":
                clone.image_url = None
            elif args.photo == "broken":
                clone.image_url = BROKEN_PHOTO
            session.add(clone)
            session.flush()
            session.add(
                ListingStatusHistory(
                    listing_id=clone.id,
                    event_type=args.event,
                    old_status=None,
                    new_status="active",
                    observed_at=now,
                )
            )
            print(
                f"  +{clone.id}  {original.title}  ${float(original.current_price_usd):,.0f}"
                f"  −{below:.0f}%  фото: {'да' if clone.image_url else 'нет'}"
            )
        session.commit()
    print(f"Впрыснуто {len(chosen)}. Придут следующим циклом бота (BOT_FRESH_INTERVAL_MINUTES).")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--telegram-id", type=int)
    parser.add_argument("--count", type=int, default=3)
    parser.add_argument("--photo", choices=("keep", "none", "broken"), default="keep")
    parser.add_argument("--brand")
    parser.add_argument("--max-percent", type=float, default=0, help="отсечь скидки больше этой")
    parser.add_argument("--min-year", type=int, default=0, help="только не старше этого года выпуска")
    parser.add_argument("--min-price", type=float, default=0, help="только не дешевле, USD")
    parser.add_argument("--event", choices=("new", "price_changed"), default="new")
    parser.add_argument("--cleanup", action="store_true")
    args = parser.parse_args()
    _guard()
    if args.cleanup:
        cleanup()
    elif args.telegram_id is None:
        parser.error("--telegram-id обязателен")
    else:
        inject(args)


if __name__ == "__main__":
    main()
