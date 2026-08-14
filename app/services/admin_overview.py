"""Главный экран панели: состояние базы, сбора и людей.

Порядок блоков повторяет порядок вопросов: «данные собираются?» → «сколько
людей платит?» → «как идёт воронка?». Каждый блок — один-два запроса; медиана
считается в БД через percentile_cont, а не выгрузкой цен в память.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import BotUser, Listing, ParserRun, Source
from app.services.funnel import funnel_steps


def _aware(value: datetime | None) -> datetime | None:
    return value.replace(tzinfo=timezone.utc) if value is not None and value.tzinfo is None else value


def get_overview(session: Session, *, funnel_days: int = 30) -> dict:
    now = datetime.now(timezone.utc)

    listings = session.execute(
        select(
            func.count(Listing.id),
            func.count(Listing.id).filter(Listing.is_active.is_(True)),
            func.count(Listing.id).filter(
                Listing.is_active.is_(True),
                Listing.analytics_eligible.is_(True),
                Listing.current_price_usd.is_not(None),
            ),
            func.count(Listing.id).filter(Listing.first_seen_at >= now - timedelta(days=1)),
        )
    ).one()

    # Средняя и медиана — по той же выборке, что идёт в аналитику. Медиану
    # считает Postgres: 53k цен в память ради одного числа тянуть незачем.
    prices = session.execute(
        select(
            func.avg(Listing.current_price_usd),
            func.percentile_cont(0.5).within_group(Listing.current_price_usd),
        ).where(
            Listing.is_active.is_(True),
            Listing.analytics_eligible.is_(True),
            Listing.current_price_usd.is_not(None),
        )
    ).one()

    users = {
        row[0]: int(row[1])
        for row in session.execute(
            select(BotUser.access_status, func.count(BotUser.id)).group_by(BotUser.access_status)
        ).all()
    }

    return {
        "listings": {
            "total": int(listings[0] or 0),
            "active": int(listings[1] or 0),
            "analytics_eligible": int(listings[2] or 0),
            "new_last_day": int(listings[3] or 0),
            "average_price_usd": round(float(prices[0]), 2) if prices[0] else None,
            "median_price_usd": round(float(prices[1]), 2) if prices[1] else None,
            "last_seen_at": _aware(session.scalar(select(func.max(Listing.last_seen_at)))),
        },
        "users": {
            "total": sum(users.values()),
            "member": users.get("member", 0),
            "subscriber": users.get("subscriber", 0),
            "trial": users.get("trial", 0),
            "revoked": users.get("revoked", 0),
        },
        "sources": last_runs(session),
        "funnel": {"days": funnel_days, "steps": funnel_steps(session, days=funnel_days)},
    }


def last_runs(session: Session) -> list[dict]:
    """Последний прогон по каждому источнику — ответ на «почему нет новых».

    DISTINCT ON вместо запроса на источник: источников сейчас два, но список
    задаётся конфигом и может вырасти, а лишние запросы в цикле — привычка,
    которая потом кусается.
    """
    rows = session.execute(
        select(ParserRun, Source.name)
        .join(Source, Source.id == ParserRun.source_id)
        .distinct(ParserRun.source_id)
        .order_by(ParserRun.source_id, ParserRun.started_at.desc())
    ).all()

    return [
        {
            "source": source_name,
            "mode": run.mode,
            "status": run.status,
            "started_at": _aware(run.started_at),
            "finished_at": _aware(run.finished_at),
            "new_count": run.new_count,
            "updated_count": run.updated_count,
            "error_count": run.error_count,
            "error_message": run.error_message,
        }
        for run, source_name in rows
    ]


def list_parser_runs(session: Session, *, limit: int = 20) -> list[dict]:
    limit = max(1, min(limit, 100))
    rows = session.execute(
        select(ParserRun, Source.name)
        .join(Source, Source.id == ParserRun.source_id)
        .order_by(ParserRun.started_at.desc())
        .limit(limit)
    ).all()

    return [
        {
            "id": run.id,
            "source": source_name,
            "mode": run.mode,
            "status": run.status,
            "started_at": _aware(run.started_at),
            "finished_at": _aware(run.finished_at),
            "new_count": run.new_count,
            "updated_count": run.updated_count,
            "price_changed_count": run.price_changed_count,
            "deactivated_count": run.deactivated_count,
            "error_count": run.error_count,
            "error_message": run.error_message,
        }
        for run, source_name in rows
    ]
