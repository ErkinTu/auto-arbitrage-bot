"""Фильтры уведомлений в профиле сайта — те же строки `user_filters`, что у бота.

Источник истины — сервер (решение владельца 12.11.2026): CRUD идёт через
функции `bot/db.py`, поэтому лимит, запрет дублей и «правка = UPDATE той же
строки» (история отправок и точка отсчёта сохраняются) — общие с ботом. Бот
читает базу на каждое нажатие, так что правка с сайта видна в нём сразу.

Доступ: смотреть, удалять и предпросмотр — вошедшему; создавать и править —
с доступом к пушам (роль subscriber, как в боте: группа / оплата / триал; без
доступа бот тоже не даёт трогать фильтры). Изменения — только со своего сайта
(`_same_origin`, как у устройств) и не чаще паузы/часового потолка на человека
(`_throttle`); сообщение в бота о правке — не чаще раза в N минут.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.api.auth import _same_origin
from app.api.roles import require_subscriber, require_user
from app.bot import db as bot_db
from app.bot.messages import format_filter_line
from app.core.cache import cached_call
from app.core.config import get_settings
from app.core.ratelimit import Policy, limiter
from app.db.session import get_session
from app.models import BotUser
from app.schemas import PushFilter, PushFilterIn, PushFilterPreview, PushFilters, PushFilterUpdate
from app.services import push_filters as pf
from app.services.filter_brands import MAX_FILTER_BRANDS
from app.services.regions import REGION_CHOICES
from app.services.site_auth import Viewer, role_at_least
from app.services.telegram import send_message

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/me/filters", tags=["push-filters"])

PREVIEW_CACHE_SECONDS = 300


def _problem(code: int, kind: str, message: str, **extra) -> HTTPException:
    """Ошибка, которую сайт показывает как есть: `detail.message` — для человека."""
    return HTTPException(code, {"code": kind, "message": message, **extra})


def _throttle(user_id: int) -> None:
    """Короткая пауза между изменениями и потолок в час — на человека, а не на IP.

    Бакеты общего лимитера (память процесса, как у лимита API; при нескольких
    воркерах — Redis). 429 с `retry_after`: сайт показывает «подождите N с».
    """
    settings = get_settings()
    key = str(user_id)
    pause = settings.site_filter_write_pause_seconds
    if pause > 0:
        decision = limiter.hit(Policy("me.filters.pause", 60 / pause, 1), key)
        if not decision.allowed:
            raise _too_often(decision.retry_after, "Слишком часто — подождите {} с")
    per_hour = settings.site_filter_writes_per_hour
    if per_hour > 0:
        decision = limiter.hit(Policy("me.filters.hour", per_hour / 60, per_hour), key)
        if not decision.allowed:
            minutes = max(1, -(-decision.retry_after // 60))
            raise _too_often(decision.retry_after, "Много изменений подряд — попробуйте через {} мин", minutes)


def _too_often(retry_after: int, template: str, value: int | None = None) -> HTTPException:
    exc = _problem(
        status.HTTP_429_TOO_MANY_REQUESTS, "too_often", template.format(value or retry_after), retry_after=retry_after
    )
    exc.headers = {"Retry-After": str(retry_after)}
    return exc


_notified_at: dict[int, float] = {}
_notified_lock = threading.Lock()


def _may_notify(telegram_id: int) -> bool:
    """Сообщение в бота о правке с сайта — не чаще раза в N минут на человека."""
    interval = get_settings().site_filter_bot_notify_minutes * 60
    now = time.monotonic()
    with _notified_lock:
        last = _notified_at.get(telegram_id)
        if last is not None and now - last < interval:
            return False
        _notified_at[telegram_id] = now
        if len(_notified_at) > 50_000:  # не копить память бесконечно
            _notified_at.clear()
        return True


def reset_limits() -> None:
    """Для тестов."""
    with _notified_lock:
        _notified_at.clear()


def _summary(item: bot_db.FilterDTO) -> str:
    return format_filter_line(
        brand_name=item.brand_name,
        brand_names=item.brand_names,
        model_name=item.model_name,
        year_from=item.year_from,
        year_to=item.year_to,
        mileage_max_km=item.mileage_max_km,
        price_max_usd=item.price_max_usd,
        below_market_only=item.below_market_only,
        below_market_percent=item.below_market_percent,
        skip_foreign=item.skip_foreign,
        region=item.region,
        brand_group=item.brand_group,
        max_age_years=item.max_age_years,
        fuel_groups=item.fuel_groups,
    )


def _serialize(item: bot_db.FilterDTO, telegram_id: int) -> dict:
    if item.brand_ids:
        brands = [{"id": i, "name": n} for i, n in zip(item.brand_ids, item.brand_names)]
    elif item.brand_id is not None:
        brands = [{"id": item.brand_id, "name": item.brand_name or ""}]
    else:
        brands = []
    stats = bot_db.filter_sent_stats(telegram_id, item.id)
    return {
        "id": item.id,
        "summary": _summary(item),
        "brands": brands,
        "model_id": item.car_model_id if len(brands) == 1 else None,
        "model_name": item.model_name if len(brands) == 1 else None,
        "year_from": item.year_from,
        "year_to": item.year_to,
        "max_age_years": item.max_age_years,
        "mileage_max_km": item.mileage_max_km,
        "price_max_usd": round(item.price_max_usd) if item.price_max_usd is not None else None,
        "below_market_percent": item.below_market_percent if item.below_market_only else None,
        "region": item.region,
        "fuel_groups": [g for g in (item.fuel_groups or "").split(",") if g],
        "legacy": (item.below_market_only and item.below_market_percent is None)
        or (item.skip_foreign and item.region is None)
        or (item.brand_group is not None and not item.brand_ids),
        "sent_7d": stats.count,
        "best_percent_7d": stats.best_percent,
        "updated_at": item.updated_at,
    }


def _spec(session: Session, payload: PushFilterIn) -> pf.PushFilterSpec:
    try:
        return pf.validate_spec(
            session,
            brand_ids=payload.brand_ids,
            model_id=payload.model_id,
            year_from=payload.year_from,
            year_to=payload.year_to,
            max_age_years=payload.max_age_years,
            mileage_max_km=payload.mileage_max_km,
            price_max_usd=payload.price_max_usd,
            below_market_percent=payload.below_market_percent,
            region=payload.region,
            fuel_groups=list(payload.fuel_groups),
        )
    except pf.SpecError as exc:
        raise _problem(status.HTTP_422_UNPROCESSABLE_CONTENT, "invalid", str(exc), field=exc.field) from exc


def _notify_bot(telegram_id: int, summary: str, *, edited: bool) -> None:
    """Подтверждение в боте. Ошибка доставки — не ошибка сохранения."""
    if not _may_notify(telegram_id):
        return
    try:
        send_message(telegram_id, pf.site_created_text(summary, edited=edited))
    except Exception:  # noqa: BLE001 — фоновая задача, фильтр уже сохранён
        logger.warning("Не удалось сообщить в бота о фильтре с сайта (%s)", telegram_id, exc_info=True)


def _user(viewer: Viewer) -> BotUser:
    assert viewer.user is not None  # гарантирует require_user / require_subscriber
    return viewer.user


@router.get("", response_model=PushFilters)
def list_filters(viewer: Annotated[Viewer, Depends(require_user)]) -> dict:
    """Фильтры уведомлений + лимит и справочники для редактора."""
    telegram_id = _user(viewer).telegram_id
    items = bot_db.list_filters(telegram_id)
    quota = bot_db.filters_quota(telegram_id)
    return {
        "items": [_serialize(item, telegram_id) for item in items],
        "used": quota.used,
        "limit": quota.limit,
        "can_edit": role_at_least(viewer.role, "subscriber"),
        "max_brands": MAX_FILTER_BRANDS,
        "brands": [{"id": b.id, "name": b.name} for b in bot_db.list_brands()],
        "market_percents": list(pf.MARKET_PERCENTS),
        "regions": [{"value": value, "label": label} for value, label in REGION_CHOICES],
    }


@router.post("", response_model=PushFilter, status_code=status.HTTP_201_CREATED)
def create_filter(
    payload: PushFilterIn,
    request: Request,
    background: BackgroundTasks,
    viewer: Annotated[Viewer, Depends(require_subscriber)],
    session: Session = Depends(get_session),
) -> dict:
    _same_origin(request)
    _throttle(_user(viewer).id)
    spec = _spec(session, payload)
    telegram_id = _user(viewer).telegram_id
    try:
        created = bot_db.create_filter(telegram_id, **spec.db_fields())
    except bot_db.FilterLimitError as exc:
        raise _problem(
            status.HTTP_409_CONFLICT, "limit", f"Уже {exc.used} из {exc.limit} фильтров — удалите лишний, чтобы добавить новый"
        ) from exc
    except bot_db.DuplicateFilterError as exc:
        raise _problem(status.HTTP_409_CONFLICT, "duplicate", "Такой фильтр уже есть", filter_id=exc.filter_id) from exc
    background.add_task(_notify_bot, telegram_id, _summary(created), edited=False)
    return _serialize(created, telegram_id)


@router.put("/{filter_id}", response_model=PushFilter)
def update_filter(
    filter_id: int,
    payload: PushFilterUpdate,
    request: Request,
    background: BackgroundTasks,
    viewer: Annotated[Viewer, Depends(require_subscriber)],
    session: Session = Depends(get_session),
) -> dict:
    _same_origin(request)
    _throttle(_user(viewer).id)
    spec = _spec(session, payload)
    telegram_id = _user(viewer).telegram_id
    try:
        updated = bot_db.update_filter(
            telegram_id, filter_id, expected_updated_at=payload.updated_at, **spec.db_fields()
        )
    except LookupError as exc:
        raise _problem(status.HTTP_404_NOT_FOUND, "not_found", "Фильтр уже удалён") from exc
    except bot_db.StaleFilterError as exc:
        raise _problem(
            status.HTTP_409_CONFLICT, "stale", "Фильтр изменили в боте — обновите страницу, чтобы не затереть правку"
        ) from exc
    except bot_db.DuplicateFilterError as exc:
        raise _problem(status.HTTP_409_CONFLICT, "duplicate", "Такой фильтр уже есть", filter_id=exc.filter_id) from exc
    background.add_task(_notify_bot, telegram_id, _summary(updated), edited=True)
    return _serialize(updated, telegram_id)


@router.delete("/{filter_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_filter(
    filter_id: int,
    request: Request,
    viewer: Annotated[Viewer, Depends(require_user)],
) -> None:
    """Удалить можно и без доступа: это своя настройка (освободить слот)."""
    _same_origin(request)
    _throttle(_user(viewer).id)
    if not bot_db.delete_filter(_user(viewer).telegram_id, filter_id):
        raise _problem(status.HTTP_404_NOT_FOUND, "not_found", "Фильтр уже удалён")


def _preview(session: Session, *, spec: pf.PushFilterSpec, user_id: int) -> dict:
    user = session.get(BotUser, user_id)
    return pf.preview(session, spec, user=user)


@router.post("/preview", response_model=PushFilterPreview)
def preview_filter(
    payload: PushFilterIn,
    viewer: Annotated[Viewer, Depends(require_user)],
    session: Session = Depends(get_session),
) -> dict:
    """«За 7 дней прислал бы N авто» — по черновику, до сохранения.

    POST, потому что это черновик формы, а не ресурс; ничего не пишет. Лимит —
    heavy (`core/ratelimit.py`), кэш 5 мин на человека и фильтр: ползунок
    редактора шлёт черновик с задержкой, а одинаковые черновики не пересчитываем.
    Вошедшему без подписки — тоже, но с ограничением (решение владельца
    12.11.2026: «желание на подписку», но без лишней нагрузки): не больше
    `SITE_FILTER_PREVIEW_FREE_PER_DAY` расчётов в сутки, и только цифры и
    столбики — сами машины (`samples`) видны с подпиской.
    """
    spec = _spec(session, payload)
    user = _user(viewer)
    free = not role_at_least(viewer.role, "subscriber")
    left: int | None = None
    if free:
        per_day = get_settings().site_filter_preview_free_per_day
        decision = limiter.hit(Policy("me.filters.preview_free", per_day / 1440, per_day), str(user.id))
        if not decision.allowed:
            exc = _problem(
                status.HTTP_429_TOO_MANY_REQUESTS,
                "preview_limit",
                "Бесплатные проверки на сегодня закончились — с подпиской без ограничений",
                retry_after=decision.retry_after,
            )
            exc.headers = {"Retry-After": str(decision.retry_after)}
            raise exc
        left = decision.remaining
    result = cached_call(
        "me.filters.preview",
        _preview,
        session,
        ttl=PREVIEW_CACHE_SECONDS,
        spec=spec,
        user_id=user.id,
    )
    if free:
        return {**result, "samples": [], "limited": True, "free_left": left}
    return {**result, "limited": False, "free_left": None}

