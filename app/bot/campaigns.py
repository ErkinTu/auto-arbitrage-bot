"""Разовые рассылки и замеры объёма для решений по фильтрам (зовёт `cli.py`).

Sync, без aiogram: отправка — `services/telegram.send_message`, как у worker-а.
Рассылка по умолчанию — сухой прогон (размер сегмента и пример текста);
реальная отправка только с `--send`, только днём и строго один раз на человека
(событие в `user_events`): повторный запуск после обрыва досылает остальным.
"""

from __future__ import annotations

import logging
import time
from collections import Counter
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.bot.keyboards import base_filter_kb
from app.bot.matching import (
    filter_matches,
    is_complete_listing,
    is_plausible_discount,
    passes_below_market,
)
from app.bot.messages import burned_broadcast_text, nudge_filter_text
from app.bot.showcase import market_averages
from app.core.config import get_settings
from app.models import BotUser, Listing, Payment, UserEvent, UserFilter
from app.services.blocklist import load_block_sets
from app.services.events import log_event
from app.services.filter_presets import load_brand_groups, presets
from app.services.normalizer import MIN_PLAUSIBLE_PRICE_USD
from app.services.regions import LOCAL
from app.services.sync import utcnow
from app.services.telegram import SendOutcome, send_message

logger = logging.getLogger(__name__)

SEGMENT_BURNED = "burned"
SEGMENT_NO_FILTER = "no-filter"
SEGMENTS = (SEGMENT_BURNED, SEGMENT_NO_FILTER)

# Пауза между отправками: ~20/с при лимите Telegram ~30/с суммарно — с запасом
# на рассылку бота, которая идёт параллельно.
SEND_PAUSE_SECONDS = 0.05
WEEK = timedelta(days=7)


def _listings_published(session: Session, since) -> list[Listing]:
    """Всё, что появилось за окно, включая уже снятое: замер про поток рынка,
    а не про то, что ещё висит.

    Правила полноты и правдоподобной скидки — те же, что у рассылки
    (`BOT_REQUIRE_COMPLETE_LISTINGS`, `BOT_MAX_BELOW_MARKET_PERCENT`): замер и
    обещание «N машин по вашему фильтру» не должны считать то, что бот не шлёт.
    """
    settings = get_settings()
    now = utcnow()
    listings = list(
        session.scalars(
            select(Listing).where(
                Listing.source_created_at >= since,
                Listing.current_price_usd.is_not(None),
                Listing.current_price_usd >= MIN_PLAUSIBLE_PRICE_USD,
            )
        ).all()
    )
    if settings.bot_require_complete_listings:
        listings = [listing for listing in listings if is_complete_listing(listing, now)]
    return listings


def _plausible(listing: Listing, avg: float | None) -> bool:
    if avg is None or avg <= 0:
        return True  # без средней скидки нет — нечего и отсекать
    percent = (1 - float(listing.current_price_usd) / avg) * 100
    return is_plausible_discount(percent, get_settings().bot_max_below_market_percent)


# --------------------------------------------------------------------------
# Замер объёма: сколько авто в сутки проходит порог / пресет
# --------------------------------------------------------------------------


@dataclass(slots=True)
class VolumeRow:
    label: str
    per_day: float


def preset_volume(session: Session, *, days: int) -> tuple[int, list[VolumeRow]]:
    """Сколько объявлений в сутки прошло бы базовый фильтр при 5/10/15/20% и каждый пресет.

    Нужен ДО смены порога базового фильтра: 20% выбраны как самый тихий из
    популярных вариантов, но тишина новичка — та же проблема, что и спам.
    Средняя — по текущей статистике (`market_average` берёт последнюю дату), то
    есть оценка «как было бы с сегодняшним рынком», а не точная история пушей.
    """
    settings = get_settings()
    since = utcnow() - timedelta(days=days)
    listings = _listings_published(session, since)
    averages = market_averages(session, listings, min_samples=settings.bot_below_avg_min_samples)
    brand_groups = load_brand_groups(session)

    rows: list[VolumeRow] = []
    for percent in (5, 10, 15, 20, 25):
        probe = UserFilter(region=LOCAL, below_market_only=True, below_market_percent=percent)
        count = sum(
            1
            for listing, avg in averages.items()
            if filter_matches(listing, probe, brand_groups)
            and passes_below_market(listing, probe, avg, factor=settings.bot_below_avg_factor)
            and _plausible(listing, avg)
        )
        rows.append(VolumeRow(f"КР, ниже рынка на {percent}%", count / days))
    for preset in presets():
        probe = UserFilter(
            region=preset.region,
            below_market_only=True,
            below_market_percent=preset.below_market_percent,
            price_max_usd=preset.price_max_usd,
            brand_group=preset.brand_group,
            max_age_years=preset.max_age_years,
        )
        count = sum(
            1
            for listing, avg in averages.items()
            if filter_matches(listing, probe, brand_groups)
            and passes_below_market(listing, probe, avg, factor=settings.bot_below_avg_factor)
            and _plausible(listing, avg)
        )
        rows.append(VolumeRow(f"пресет «{preset.summary}»", count / days))
    return len(listings), rows


# --------------------------------------------------------------------------
# Разовые рассылки
# --------------------------------------------------------------------------


def campaign_event(segment: str) -> str:
    return f"broadcast_{segment.replace('-', '_')}"


@dataclass(slots=True)
class Recipient:
    user: BotUser
    text: str
    reply_markup: dict | None = None


def _not_yet_sent(segment: str):
    return BotUser.id.not_in(
        select(UserEvent.user_id).where(UserEvent.event == campaign_event(segment))
    )


def burned_recipients(session: Session) -> list[Recipient]:
    """Триал сгорел, пейвол ушёл, к оплате не подступались.

    Текст с содержанием: сколько машин ниже рынка было за неделю по ИХ
    фильтрам (той же проверкой, что и рассылка, с чёрным списком). Без фильтра
    или с нулём — общий текст: «0 машин» продаёт против нас.
    """
    settings = get_settings()
    users = list(
        session.scalars(
            select(BotUser).where(
                BotUser.is_active.is_(True),
                BotUser.access_status == "revoked",
                BotUser.trial_expired_notified_at.is_not(None),
                BotUser.id.not_in(select(Payment.user_id)),
                _not_yet_sent(SEGMENT_BURNED),
            )
        ).all()
    )
    if not users:
        return []

    user_ids = {user.id for user in users}
    filters_by_user: dict[int, list[UserFilter]] = {}
    for item in session.scalars(
        select(UserFilter).where(UserFilter.user_id.in_(user_ids), UserFilter.enabled.is_(True))
    ):
        filters_by_user.setdefault(item.user_id, []).append(item)

    averages: dict[Listing, float | None] = {}
    if filters_by_user:
        listings = _listings_published(session, utcnow() - WEEK)
        averages = market_averages(
            session, listings, min_samples=settings.bot_below_avg_min_samples
        )
    brand_groups = load_brand_groups(session)
    block_sets = load_block_sets(session, set(filters_by_user))

    recipients = []
    for user in users:
        matches = 0
        user_filters = [item for item in filters_by_user.get(user.id, []) if item.below_market_only]
        blocked = block_sets.get(user.id)
        for listing, avg in averages.items() if user_filters else ():
            if blocked is not None and blocked.blocks(listing):
                continue
            # Считаем только фильтры «ниже рынка»: обещание в тексте — про них.
            if any(
                filter_matches(listing, user_filter, brand_groups)
                and passes_below_market(
                    listing, user_filter, avg, factor=settings.bot_below_avg_factor
                )
                and _plausible(listing, avg)
                for user_filter in user_filters
            ):
                matches += 1
        recipients.append(Recipient(user, burned_broadcast_text(matches=matches or None)))
    return recipients


def no_filter_recipients(session: Session) -> list[Recipient]:
    """Доступ есть (триал или подписка), фильтров нет — бот для них молчит.

    Кнопка «поставить базовый фильтр» работает только с доступом, поэтому
    истёкшие триалы сюда не входят — они в сегменте `burned`.
    """
    now = utcnow()
    has_filter = select(UserFilter.user_id).where(UserFilter.enabled.is_(True))
    users = session.scalars(
        select(BotUser).where(
            BotUser.is_active.is_(True),
            BotUser.id.not_in(has_filter),
            (BotUser.trial_expires_at > now) | (BotUser.subscription_expires_at > now),
            _not_yet_sent(SEGMENT_NO_FILTER),
        )
    ).all()
    markup = base_filter_kb().model_dump(exclude_none=True)
    return [Recipient(user, nudge_filter_text(1), markup) for user in users]


def recipients_for(session: Session, segment: str) -> list[Recipient]:
    if segment == SEGMENT_BURNED:
        return burned_recipients(session)
    if segment == SEGMENT_NO_FILTER:
        return no_filter_recipients(session)
    raise ValueError(f"unknown segment {segment!r}")


def send_campaign(session: Session, segment: str, recipients: list[Recipient]) -> Counter:
    """Отправляет и журналит каждого сразу (обрыв посередине не даст дублей).

    FAILED (429/сеть) не журналим — повторный запуск дошлёт. Заблокировавших
    бота журналим и помечаем неактивными, как это делает рассылка бота.
    """
    outcomes: Counter = Counter()
    event = campaign_event(segment)
    for recipient in recipients:
        outcome = send_message(
            recipient.user.telegram_id, recipient.text, reply_markup=recipient.reply_markup
        )
        outcomes[outcome.name] += 1
        if outcome is not SendOutcome.FAILED:
            log_event(
                session,
                recipient.user.id,
                event,
                {"delivered": outcome is SendOutcome.DELIVERED},
            )
            if outcome is SendOutcome.UNDELIVERABLE:
                recipient.user.is_active = False
            session.commit()
        time.sleep(SEND_PAUSE_SECONDS)
    return outcomes

