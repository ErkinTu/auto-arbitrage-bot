"""Чистая sync-логика подбора новых объявлений под фильтры пользователей.

Не зависит от aiogram: принимает Session, возвращает DTO. Вызывается poller-ом
через asyncio.to_thread и напрямую из тестов.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from app.models import (
    BotUser,
    Brand,
    DailyMarketStat,
    Listing,
    ListingStatusHistory,
    SentNotification,
    Source,
    UserFilter,
)
from app.services.access_schedule import active_tier, load_active_tiers
from app.services.blocklist import load_block_sets
from app.services.filter_presets import load_brand_groups, min_year_for_age
from app.services.normalizer import MIN_PLAUSIBLE_PRICE_USD
from app.services.plans import TIER_DELAYED, effective_tier
from app.services.regions import is_foreign, region_matches
from app.services.stats import mileage_bucket
from app.services.sync import utcnow
from app.services.vehicle_summary import _build_source_url as build_source_url


def _as_aware(dt: datetime) -> datetime:
    """Наивную дату из БД трактуем как UTC (SQLite теряет tz, Postgres — нет)."""
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


@dataclass(slots=True)
class PendingNotification:
    chat_id: int
    listing_id: int
    filter_id: int
    title: str | None
    year: int | None
    mileage_km: int | None
    price_usd: float | None
    city: str | None
    url: str
    below_market_percent: float | None
    market_avg_usd: float | None = None
    source_name: str = "mashina.kg"
    # Марка — для группировки нескольких авто в одно сообщение (notifier).
    brand_name: str | None = None


def find_new_listings(
    session: Session, since: datetime, *, max_age_hours: int = 12
) -> list[Listing]:
    """Активные объявления с известной ценой — кандидаты на пуш за окно since.

    Два типа кандидатов с РАЗНОЙ логикой свежести:
    - new: впервые увиденные. VIP-поднятые старые тоже дают событие new, поэтому
      пуш «новинка» — только если публикация на источнике свежее max_age_hours
      (NULL source_created_at отсекается сравнением: пуш по неполным данным хуже
      тишины, тот же принцип, что в filter_matches).
    - price_changed: подешевевшие уже после показа. Свежесть определяется ВРЕМЕНЕМ
      изменения цены (observed_at в окне since), а не датой публикации: старое
      объявление, только что упавшее ниже рынка, — легитимный кандидат, поэтому
      source_created_at здесь НЕ проверяем.
    """
    # DISTINCT по id в подзапросе: DISTINCT по всем колонкам Listing невозможен
    # в Postgres из-за JSON-колонки quality_flags (нет оператора равенства).
    new_listing_ids = (
        select(ListingStatusHistory.listing_id)
        .where(
            ListingStatusHistory.event_type == "new",
            ListingStatusHistory.observed_at >= since,
        )
        .distinct()
    )
    price_changed_ids = (
        select(ListingStatusHistory.listing_id)
        .where(
            ListingStatusHistory.event_type == "price_changed",
            ListingStatusHistory.observed_at >= since,
        )
        .distinct()
    )
    published_after = utcnow() - timedelta(hours=max_age_hours)
    return list(
        session.scalars(
            select(Listing).where(
                Listing.is_active.is_(True),
                Listing.current_price_usd.is_not(None),
                # Мусорный ввод цены («180 сом» за авто) — не повод для пуша.
                Listing.current_price_usd >= MIN_PLAUSIBLE_PRICE_USD,
                or_(
                    and_(
                        Listing.id.in_(new_listing_ids),
                        Listing.source_created_at >= published_after,
                    ),
                    Listing.id.in_(price_changed_ids),
                ),
            )
        ).all()
    )


def qualifying_event_times(session: Session, since: datetime) -> dict[int, datetime]:
    """Когда объявление стало кандидатом на пуш: MIN(observed_at) в окне since.

    Нужно для точки отсчёта фильтра: пуш уместен, только если событие случилось
    ПОСЛЕ того, как человек завёл фильтр. Иначе новый фильтр (и вообще любой
    новичок) забирает себе всё окно `bot_new_lookback_minutes` целиком —
    на проде это ~500 объявлений, то есть пачка в потолок и «капель» ещё
    несколько часов, пока окно не состарится.
    """
    rows = session.execute(
        select(
            ListingStatusHistory.listing_id,
            func.min(ListingStatusHistory.observed_at),
        )
        .where(
            ListingStatusHistory.event_type.in_(("new", "price_changed")),
            ListingStatusHistory.observed_at >= since,
        )
        .group_by(ListingStatusHistory.listing_id)
    )
    return {listing_id: observed_at for listing_id, observed_at in rows}


def price_change_times(session: Session, since: datetime) -> dict[int, datetime]:
    """Последнее снижение/изменение цены в окне since — по объявлению.

    Нужно точке отсчёта фильтра: подешевевшее объявление — новость, даже если
    само оно опубликовано до фильтра, но только если цена изменилась ПОСЛЕ него.
    """
    rows = session.execute(
        select(ListingStatusHistory.listing_id, func.max(ListingStatusHistory.observed_at))
        .where(
            ListingStatusHistory.event_type == "price_changed",
            ListingStatusHistory.observed_at >= since,
        )
        .group_by(ListingStatusHistory.listing_id)
    )
    return {listing_id: observed_at for listing_id, observed_at in rows}


def published_before_filter(
    listing: Listing, user_filter: UserFilter, price_changed_at: datetime | None
) -> bool:
    """Объявление — «накопленное» до фильтра, а не новость для этого человека.

    Сравниваем с датой ПУБЛИКАЦИИ, а не с моментом, когда мы его заметили:
    за ночную паузу, трёхчасовое окно и догон парсера копятся объявления,
    опубликованные задолго до регистрации, но замеченные нами после неё. Замер
    21.10.2026: новичок получил 48 таких одним циклом через 15 минут после
    фильтра. Накопленное отдаёт превью — разово и лучшее, без залпа.
    Подешевевшее после создания фильтра — новость независимо от публикации.
    """
    created_at = _as_aware(user_filter.created_at)
    if price_changed_at is not None and _as_aware(price_changed_at) >= created_at:
        return False
    published = listing.source_created_at
    return published is None or _as_aware(published) < created_at


def _build_pending(
    listing: Listing,
    *,
    chat_id: int,
    filter_id: int,
    below_percent: float | None,
    avg: float | None,
    source_names: dict[int, str],
    brand_names: dict[int, str],
) -> PendingNotification:
    """Сборка DTO уведомления — общая для рассылки и для превью нового фильтра."""
    return PendingNotification(
        chat_id=chat_id,
        listing_id=listing.id,
        filter_id=filter_id,
        title=listing.title,
        year=listing.year,
        mileage_km=listing.mileage_km,
        price_usd=float(listing.current_price_usd)
        if listing.current_price_usd is not None
        else None,
        city=listing.city,
        url=build_source_url(listing),
        below_market_percent=below_percent,
        market_avg_usd=avg,
        source_name=source_names.get(listing.source_id, "mashina.kg"),
        brand_name=brand_names.get(listing.brand_id) if listing.brand_id else None,
    )


# Пробег меньше этого у подержанной машины — почти всегда заглушка («100»,
# «400» км у машины 1998 года), а не реальное значение. У новых (не старше
# года) малый пробег законен: «2025, 27 км» — машина из салона.
MIN_PLAUSIBLE_MILEAGE_KM = 1000
MAX_PLAUSIBLE_MILEAGE_KM = 1_000_000


def is_complete_listing(listing: Listing, now: datetime | None = None) -> bool:
    """Полные ли данные у объявления: марка, модель, год и правдоподобный пробег.

    Без пробега «ниже рынка» сравнивается со средней по всем пробегам модели и
    года — для оценки это допустимо, а показывать такое как «находку» нельзя:
    человек не может понять, хорошая ли это цена.
    """
    if (
        listing.brand_id is None
        or listing.car_model_id is None
        or listing.year is None
        or listing.mileage_km is None
    ):
        return False
    if listing.mileage_km > MAX_PLAUSIBLE_MILEAGE_KM:
        return False
    current_year = (now or utcnow()).year
    return listing.mileage_km >= MIN_PLAUSIBLE_MILEAGE_KM or listing.year >= current_year - 1


def is_plausible_discount(below_percent: float | None, max_percent: float) -> bool:
    """Скидка к рынку правдоподобна (max_percent <= 0 — без отсечки)."""
    return max_percent <= 0 or below_percent is None or below_percent <= max_percent


def filter_matches(
    listing: Listing,
    user_filter: UserFilter,
    brand_groups: Mapping[str, frozenset[int]] | None = None,
    *,
    now: datetime | None = None,
) -> bool:
    """Проверка объявления против одного фильтра. NULL-поле фильтра = любое значение.

    Если фильтр задает границу, а у объявления поле неизвестно, считаем не-матчем:
    push-уведомление по неполным данным хуже, чем тишина.

    brand_groups — код группы -> id марок (`filter_presets.load_brand_groups`).
    Фильтр с группой без переданной карты не матчит ничего: та же логика
    «не доказано — не шлём».
    """
    if user_filter.brand_id is not None and listing.brand_id != user_filter.brand_id:
        return False
    if user_filter.brand_group is not None:
        group = (brand_groups or {}).get(user_filter.brand_group)
        if not group or listing.brand_id not in group:
            return False
    if user_filter.car_model_id is not None and listing.car_model_id != user_filter.car_model_id:
        return False
    if user_filter.year_from is not None and (
        listing.year is None or listing.year < user_filter.year_from
    ):
        return False
    if user_filter.year_to is not None and (
        listing.year is None or listing.year > user_filter.year_to
    ):
        return False
    if user_filter.max_age_years is not None and (
        listing.year is None or listing.year < min_year_for_age(user_filter.max_age_years, now)
    ):
        return False
    if user_filter.mileage_max_km is not None and (
        listing.mileage_km is None or listing.mileage_km > user_filter.mileage_max_km
    ):
        return False
    if user_filter.price_max_usd is not None and (
        listing.current_price_usd is None or listing.current_price_usd > user_filter.price_max_usd
    ):
        return False
    # skip_foreign отсекает только заведомо зарубежные: у местных цена уже "на руках",
    # у зарубежных — без растаможки и доставки. Неизвестный город считаем местным.
    # (Legacy-поле старых фильтров; новые задают регион ниже, а skip_foreign=False.)
    if user_filter.skip_foreign and is_foreign(listing.city):
        return False
    # Регион пользователя: NULL = «Все авто» (без ограничения). Иначе объявление
    # должно попасть в выбранный регион («Местный рынок» = любой не-зарубежный;
    # для области/города — точное совпадение, неизвестный/спорный город = не матч,
    # пуш по неполной геолокации хуже тишины — как и с прочими границами фильтра).
    if user_filter.region is not None and not region_matches(listing.city, user_filter.region):
        return False
    return True


def _match_signature(user_filter: UserFilter) -> tuple:
    """Поля, от которых зависит `filter_matches` — ключ мемоизации.

    `below_market_only`/`below_market_percent` сюда НЕ входят: они проверяются
    отдельно, после расчёта средней по рынку.
    """
    return (
        user_filter.brand_id,
        user_filter.car_model_id,
        user_filter.year_from,
        user_filter.year_to,
        user_filter.mileage_max_km,
        user_filter.price_max_usd,
        user_filter.skip_foreign,
        user_filter.region,
        user_filter.brand_group,
        user_filter.max_age_years,
    )


def market_average(
    session: Session,
    listing: Listing,
    *,
    min_samples: int,
) -> float | None:
    """Средняя цена по рынку из daily_market_stats для группы объявления.

    Возвращает None, когда статистики недостаточно для уверенного вывода
    (мало образцов, нет рядов, неполные данные объявления).
    """
    if (
        listing.brand_id is None
        or listing.car_model_id is None
        or listing.year is None
        or listing.current_price_usd is None
    ):
        return None
    # Зарубежное объявление (в городе — страна) сравнивать не с чем: агрегаты
    # считаются только по местному рынку (см. stats.py), а цена «в Китае» без
    # растаможки и доставки — другой товар. Иначе почти любая машина из-за
    # рубежа выглядела бы «на 30% ниже рынка» просто потому, что рынок чужой.
    if is_foreign(listing.city):
        return None

    conditions = [
        DailyMarketStat.brand_id == listing.brand_id,
        DailyMarketStat.car_model_id == listing.car_model_id,
        DailyMarketStat.year == listing.year,
    ]
    # При известном пробеге сравниваем внутри своего бакета. При NULL-пробеге
    # (у lalafo ~43%) bucket unknown оторван от парка mashina и почти никогда не
    # набирает min_samples → честнее агрегировать ВСЕ бакеты модель+года (взвешенно
    # по sample_count — суммирование ниже уже это делает). Безопасно только вместе
    # с отсевом выбросов в stats (иначе средняя заражается выбросом из unknown).
    if listing.mileage_km is not None:
        conditions.append(DailyMarketStat.mileage_bucket == mileage_bucket(listing.mileage_km))
    # По растаможке НЕ делим: это артефакт источника, а не сегмент рынка. mashina.kg
    # поле не отдаёт вовсе (весь её парк — customs_status='unknown'), lalafo парсит
    # «Растаможку» → её объявления с 'cleared' сравнивались лишь с другими lalafo-
    # 'cleared' (~11% парка) и были отрезаны от 42k mashina: 64% таких объявлений не
    # набирали min_samples и молча не попадали в рассылку (у всех фильтров включён
    # below_market_only, поэтому «нет статистики» = тишина). Рынок КР локальный, так
    # что растаможенные и нерастаможенные сравниваем вместе; понадобится различать —
    # добавим уточнение в фильтре бота, измерение в ключе daily_market_stats осталось.

    latest_date = session.scalar(select(func.max(DailyMarketStat.stat_date)).where(*conditions))
    if latest_date is None:
        return None

    rows = session.scalars(
        select(DailyMarketStat).where(*conditions, DailyMarketStat.stat_date == latest_date)
    ).all()
    total_samples = sum(row.sample_count for row in rows)
    if total_samples < min_samples:
        return None

    # Взвешенное по числу образцов среднее по поколениям (и группам растаможки при unknown).
    return sum(float(row.avg_price_usd) * row.sample_count for row in rows) / total_samples


def passes_below_market(
    listing: Listing, user_filter: UserFilter, avg: float | None, *, factor: float
) -> bool:
    """Тумблер «только ниже рынка»: отсутствие статистики = не матч.

    Порог — из фильтра пользователя (5/10/15/20%), иначе глобальный factor.
    Фильтр без тумблера проходит всегда.
    """
    if not user_filter.below_market_only:
        return True
    if avg is None or avg <= 0:
        return False
    threshold = (
        1 - user_filter.below_market_percent / 100
        if user_filter.below_market_percent is not None
        else factor
    )
    return float(listing.current_price_usd) <= avg * threshold


# Уровни приоритета фильтра при нехватке слотов за цикл (меньше = важнее).
PRIORITY_BELOW_MARKET = 1  # «ниже рынка» — всегда первый, независимо от охвата
PRIORITY_SPECIFIC = 2  # конкретная марка и/или модель без порога «ниже рынка»
PRIORITY_ALL_CARS = 3  # «все авто»: без марки, без модели, без порога


def filter_priority(user_filter: UserFilter) -> int:
    """Приоритет фильтра при конкуренции за слоты потолка на цикл.

    Классифицируется САМ фильтр, до матчинга: «ниже рынка» выигрывает у любого
    охвата (в том числе у «любая Toyota» без порога), конкретная марка/модель —
    у «все авто». Это не отдельные лимиты, а порядок отбора внутри одного.
    """
    if user_filter.below_market_only:
        return PRIORITY_BELOW_MARKET
    if (
        user_filter.brand_id is not None
        or user_filter.car_model_id is not None
        or user_filter.brand_group is not None
    ):
        return PRIORITY_SPECIFIC
    return PRIORITY_ALL_CARS


def _filter_order(pair: tuple[UserFilter, BotUser]) -> tuple[int, int]:
    """Порядок перебора фильтров по одному объявлению: приоритет, затем id.

    Первый подходящий фильтр «забирает» объявление для своего юзера, поэтому
    сильный должен стоять раньше слабого: иначе «все авто» с меньшим id уводил
    бы дешёвую машину в самый слабый уровень, хотя её же ловит и «ниже рынка».
    Ключ детерминирован: при равных правах всегда выигрывает один и тот же фильтр.
    """
    user_filter = pair[0]
    return (filter_priority(user_filter), user_filter.id)


@dataclass(slots=True)
class _Candidate:
    """Пара (объявление, фильтр) одного юзера, прошедшая все проверки, кроме потолка."""

    order: int  # порядок появления — стабильный хвост сортировки
    priority: int
    below_percent: float | None
    avg: float | None
    listing: Listing
    user_filter: UserFilter
    user: BotUser


def _candidate_rank(candidate: _Candidate) -> tuple[int, float, int]:
    """Сортировка кандидатов юзера: уровень приоритета, внутри — по выгоде.

    % ниже рынка уже посчитан для текста уведомления, так что внутри уровня
    первыми идут самые дешёвые относительно рынка; без статистики — в конец
    уровня, дальше исходный порядок. Отброшенное потолком не теряется: всплывёт
    в следующем цикле, пока событие в окне `bot_new_lookback_minutes`.
    """
    below = candidate.below_percent if candidate.below_percent is not None else float("-inf")
    return (candidate.priority, -below, candidate.order)


def match_new_listings(
    session: Session,
    since: datetime,
    *,
    factor: float,
    min_samples: int,
    max_age_hours: int = 12,
    max_per_user_full: int = 0,
    max_per_user_delayed: int = 0,
    delayed_lag_minutes: int = 0,
    flush_deadline: datetime | None = None,
    require_complete: bool = False,
    max_below_percent: float = 0,
) -> list[PendingNotification]:
    """Подбирает пары (пользователь, новое объявление), фиксирует отправку, возвращает DTO.

    Строки sent_notifications коммитятся ДО фактической отправки в Telegram:
    при падении между коммитом и отправкой теряется максимум одно уведомление,
    но дубль не возникает никогда.

    Два прохода, а не один жадный. Раньше объявление → первый подходящий фильтр
    → сразу в sent_notifications, пока не упрёмся в общий счётчик юзера. При
    такой схеме «все авто» (матчит буквально каждое объявление) забивал все слоты
    раньше, чем очередь доходила до генуинно дешёвой машины, которую ловил более
    узкий фильтр того же человека, — на проде у такого юзера строгие фильтры
    «ниже рынка» получали ноль отправок (разбор — `docs/planning/TODO.md`, 2026-09-06).
    Теперь: (1) собираем ВСЕХ кандидатов юзера без урезания, (2) сортируем по
    `filter_priority` («ниже рынка» → марка/модель → «все авто»), внутри уровня
    по выгоде, (3) режем по потолку тарифа и только отобранное фиксируем.

    max_per_user_full / max_per_user_delayed (>0) — потолок пушей одному
    пользователю за цикл по его скорости (`plans.effective_tier`): сверх лимита
    объявление НЕ фиксируется в sent_notifications и придёт в следующем цикле,
    защищая последовательную рассылку от блокировки одним чатом (per-chat delay).
    0 = без ограничения.

    delayed_lag_minutes (>0) — выдержка «дешёвых» тарифов: их объявление уходит
    только когда с квалифицирующего события прошло столько минут. 0 = выдержка
    не применяется вовсе (аварийный выключатель тарифов). Частота циклов при этом
    не меняется — сдвинуто только окно кандидатов.

    require_complete — только объявления с полными данными (`is_complete_listing`);
    max_below_percent (>0) — скидка больше этой считается мусором и не уходит.

    flush_deadline — момент последнего цикла на сегодня (см. `window_end_utc`).
    Всё, что не дозреет до него, отдаём отстающему сразу: до утра объявление
    успевают снять или продать, а уведомление на мёртвую страницу читается как
    сломанный бот, а не как дешёвый тариф. Один час из шестнадцати активных —
    осознанная цена за живые ссылки.
    """
    listings = find_new_listings(session, since, max_age_hours=max_age_hours)
    now = utcnow()
    if require_complete:
        listings = [listing for listing in listings if is_complete_listing(listing, now)]
    if not listings:
        return []
    event_times = qualifying_event_times(session, since)
    price_changes = price_change_times(session, since)

    filter_user_pairs = session.execute(
        select(UserFilter, BotUser)
        .join(BotUser, UserFilter.user_id == BotUser.id)
        .where(
            UserFilter.enabled.is_(True),
            BotUser.is_active.is_(True),
            # Получатели: суперюзеры-члены группы (access_status='member', как
            # раньше — при выключенном гейте по группе все тоже 'member') ИЛИ
            # платные подписчики с ещё не истёкшей подпиской ИЛИ юзеры на
            # действующем триале. 'revoked' — никогда.
            #
            # Здесь же и весь «пейвол на 4-й день»: с истечением trial_expires_at
            # юзер просто перестаёт попадать в выборку — отдельного выключателя
            # уведомлений не нужно (разовое сообщение шлёт worker).
            or_(
                BotUser.access_status == "member",
                and_(
                    BotUser.access_status == "subscriber",
                    BotUser.subscription_expires_at.is_not(None),
                    BotUser.subscription_expires_at > now,
                ),
                and_(
                    BotUser.access_status == "trial",
                    BotUser.trial_expires_at.is_not(None),
                    BotUser.trial_expires_at > now,
                ),
            ),
        )
    ).all()
    if not filter_user_pairs:
        return []

    listing_ids = [listing.id for listing in listings]
    already_sent: set[tuple[int, int]] = {
        (row.user_id, row.listing_id)
        for row in session.execute(
            select(SentNotification.user_id, SentNotification.listing_id).where(
                SentNotification.listing_id.in_(listing_ids)
            )
        )
    }

    avg_cache: dict[int, float | None] = {}
    source_names: dict[int, str] = {
        row.id: row.name for row in session.execute(select(Source.id, Source.name))
    }
    brand_names: dict[int, str] = {
        row.id: row.name for row in session.execute(select(Brand.id, Brand.name))
    }
    # Чёрные списки и группы марок — одним запросом на цикл, дальше в памяти.
    user_ids = {user.id for _, user in filter_user_pairs}
    block_sets = load_block_sets(session, user_ids)
    brand_groups = load_brand_groups(session)
    # Скорость тарифа — тоже одним запросом на цикл (как чёрные списки), а не
    # по юзеру: строк в access_blocks столько же, сколько платежей. Нужна всегда,
    # не только при включённой выдержке: от неё зависит потолок на цикл.
    lag = timedelta(minutes=delayed_lag_minutes) if delayed_lag_minutes > 0 else None
    active_tiers = load_active_tiers(session, user_ids, now)
    tier_by_user: dict[int, str] = {
        user.id: effective_tier(user, active_tiers.get(user.id), now)
        for _, user in filter_user_pairs
    }
    # Индекс фильтров по марке: у объявления марка ровно одна, и прогонять по
    # нему фильтры чужих марок незачем. Ключ None — фильтры «все марки», их
    # проверяем всегда. Порядок внутри группы — `_filter_order`: сильный
    # приоритет раньше слабого, при равном — по id фильтра.
    filters_by_brand: dict[int | None, list[tuple[UserFilter, BotUser]]] = {}
    for pair in filter_user_pairs:
        filters_by_brand.setdefault(pair[0].brand_id, []).append(pair)
    for group in filters_by_brand.values():
        group.sort(key=_filter_order)
    any_brand_pairs = filters_by_brand.get(None, [])

    # Проход 1: все кандидаты на юзера, без урезания по потолку. Пара
    # (юзер, объявление) — не больше одного раза: первый подходящий фильтр
    # в порядке приоритета её забирает.
    candidates_by_user: dict[int, list[_Candidate]] = {}
    claimed: set[tuple[int, int]] = set()
    order = 0

    for listing in listings:
        candidates = any_brand_pairs
        if listing.brand_id is not None and listing.brand_id in filters_by_brand:
            candidates = sorted(
                any_brand_pairs + filters_by_brand[listing.brand_id], key=_filter_order
            )
        # Одинаково настроенные фильтры разных людей дают одинаковый ответ по
        # одному и тому же объявлению — считаем его один раз на объявление.
        match_cache: dict[tuple, bool] = {}

        for user_filter, user in candidates:
            key = (user.id, listing.id)
            if key in already_sent or key in claimed:
                continue
            # Точка отсчёта фильтра: объявления, появившиеся ДО его создания, —
            # не новинки для этого человека, а backfill. Без этой проверки
            # свежесозданный фильтр забирал всё окно lookback разом.
            event_at = event_times.get(listing.id)
            if event_at is not None and event_at < user_filter.created_at:
                continue
            if published_before_filter(listing, user_filter, price_changes.get(listing.id)):
                continue
            # Отставание дешёвого тарифа. `event_at is None` не задерживаем:
            # случай недостижим (объявления отобраны по событиям в окне), а
            # «задержать навсегда» хуже, чем «отдать сразу» — окно конечно.
            if (
                lag is not None
                and event_at is not None
                and tier_by_user.get(user.id) == TIER_DELAYED
            ):
                ripe_at = _as_aware(event_at) + lag
                if ripe_at > now and (flush_deadline is None or ripe_at <= flush_deadline):
                    continue  # дозреет сегодня — придёт одним из следующих циклов
            signature = _match_signature(user_filter)
            matched = match_cache.get(signature)
            if matched is None:
                matched = filter_matches(listing, user_filter, brand_groups, now=now)
                match_cache[signature] = matched
            if not matched:
                continue
            # Чёрный список сильнее фильтра: человек сказал «никогда».
            blocked = block_sets.get(user.id)
            if blocked is not None and blocked.blocks(listing):
                continue

            if listing.id not in avg_cache:
                avg_cache[listing.id] = market_average(session, listing, min_samples=min_samples)
            avg = avg_cache[listing.id]

            below_percent: float | None = None
            if avg is not None and avg > 0:
                diff = round((1 - float(listing.current_price_usd) / avg) * 100, 1)
                if diff > 0:
                    below_percent = diff

            if not passes_below_market(listing, user_filter, avg, factor=factor):
                continue
            if not is_plausible_discount(below_percent, max_below_percent):
                continue

            claimed.add(key)
            candidates_by_user.setdefault(user.id, []).append(
                _Candidate(
                    order=order,
                    priority=filter_priority(user_filter),
                    below_percent=below_percent,
                    avg=avg,
                    listing=listing,
                    user_filter=user_filter,
                    user=user,
                )
            )
            order += 1

    # Проход 2: на юзера — сортировка по приоритету и обрезка по потолку тарифа.
    # Только отобранное фиксируется в sent_notifications; остальное всплывёт в
    # следующем цикле (в пределах bot_new_lookback_minutes).
    pending: list[PendingNotification] = []
    for user_id, user_candidates in candidates_by_user.items():
        cap = (
            max_per_user_delayed if tier_by_user.get(user_id) == TIER_DELAYED else max_per_user_full
        )
        user_candidates.sort(key=_candidate_rank)
        selected = user_candidates[:cap] if cap > 0 else user_candidates
        for candidate in selected:
            already_sent.add((user_id, candidate.listing.id))
            session.add(
                SentNotification(
                    user_id=user_id,
                    listing_id=candidate.listing.id,
                    filter_id=candidate.user_filter.id,
                    below_market_percent=_percent_column(candidate.below_percent),
                )
            )
            pending.append(
                _build_pending(
                    candidate.listing,
                    chat_id=candidate.user.telegram_id,
                    filter_id=candidate.user_filter.id,
                    below_percent=candidate.below_percent,
                    avg=candidate.avg,
                    source_names=source_names,
                    brand_names=brand_names,
                )
            )

    session.commit()
    return pending


def _percent_column(value: float | None) -> Decimal | None:
    return Decimal(str(value)) if value is not None else None


def preview_for_filter(
    session: Session,
    filter_id: int,
    *,
    limit: int = 10,
    max_age_hours: int = 24,
    factor: float = 0.95,
    min_samples: int = 5,
    delayed_lag_minutes: int = 0,
    max_below_percent: float = 0,
) -> list[PendingNotification]:
    """Разовая подборка сразу после создания фильтра.

    Нужна как противовес точке отсчёта: сама по себе она означает, что человек,
    настроив фильтр, сидит в тишине до первой новой машины — а это могут быть
    часы, и на триале это выглядит как «бот не работает». Поэтому показываем,
    что уже есть на рынке за последние сутки.

    Отобранное пишем в sent_notifications: иначе те же машины придут ещё раз
    обычной рассылкой. Сортировка — по величине отставания от рынка, чтобы в
    ограниченный лимит попало лучшее, а не случайное.

    Только объявления с полными данными (`is_complete_listing`) и без
    неправдоподобной скидки: это первое, что человек видит от фильтра, и
    «Civic 1998, пробег не указан, −51%» убеждает в обратном.

    Тариф с отставанием получает здесь верхнюю границу свежести: без неё обход
    тарифа сводился бы к «удалить фильтр и создать заново» — превью отдало бы
    свежак мгновенно (лимит фильтров и запрет дубликатов пересозданию не мешают).
    """
    user_filter = session.get(UserFilter, filter_id)
    if user_filter is None:
        return []
    user = session.get(BotUser, user_filter.user_id)
    if user is None:
        return []

    now = utcnow()
    published_after = now - timedelta(hours=max_age_hours)
    query = select(Listing).where(
        Listing.is_active.is_(True),
        Listing.current_price_usd.is_not(None),
        Listing.current_price_usd >= MIN_PLAUSIBLE_PRICE_USD,
        Listing.source_created_at >= published_after,
    )
    if (
        delayed_lag_minutes > 0
        and effective_tier(user, active_tier(session, user.id, now), now) == TIER_DELAYED
    ):
        query = query.where(
            Listing.source_created_at <= now - timedelta(minutes=delayed_lag_minutes)
        )
    brand_groups = load_brand_groups(session)
    if user_filter.brand_id is not None:
        query = query.where(Listing.brand_id == user_filter.brand_id)
    elif user_filter.brand_group is not None:
        query = query.where(
            Listing.brand_id.in_(brand_groups.get(user_filter.brand_group) or ())
        )
    candidates = list(session.scalars(query).all())
    if not candidates:
        return []

    already_sent = {
        row.listing_id
        for row in session.execute(
            select(SentNotification.listing_id).where(SentNotification.user_id == user.id)
        )
    }
    blocked = load_block_sets(session, {user.id}).get(user.id)
    source_names = {row.id: row.name for row in session.execute(select(Source.id, Source.name))}
    brand_names = {row.id: row.name for row in session.execute(select(Brand.id, Brand.name))}

    scored: list[tuple[float, Listing, float | None, float | None]] = []
    for listing in candidates:
        if listing.id in already_sent or not is_complete_listing(listing, now):
            continue
        if not filter_matches(listing, user_filter, brand_groups, now=now):
            continue
        if blocked is not None and blocked.blocks(listing):
            continue

        avg = market_average(session, listing, min_samples=min_samples)
        below_percent: float | None = None
        if avg is not None and avg > 0:
            diff = round((1 - float(listing.current_price_usd) / avg) * 100, 1)
            if diff > 0:
                below_percent = diff

        if not passes_below_market(listing, user_filter, avg, factor=factor):
            continue
        if not is_plausible_discount(below_percent, max_below_percent):
            continue

        # Без статистики ставим в конец: такие показываем, только если лучших
        # не набралось — «ниже рынка» у них не доказано.
        scored.append(
            (below_percent if below_percent is not None else -1.0, listing, below_percent, avg)
        )

    scored.sort(key=lambda item: item[0], reverse=True)

    pending: list[PendingNotification] = []
    for _, listing, below_percent, avg in scored[:limit]:
        session.add(
            SentNotification(
                user_id=user.id,
                listing_id=listing.id,
                filter_id=user_filter.id,
                below_market_percent=_percent_column(below_percent),
            )
        )
        pending.append(
            _build_pending(
                listing,
                chat_id=user.telegram_id,
                filter_id=user_filter.id,
                below_percent=below_percent,
                avg=avg,
                source_names=source_names,
                brand_names=brand_names,
            )
        )
    session.commit()
    return pending
