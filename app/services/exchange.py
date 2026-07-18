"""Официальный курс USD/KGS с nbkr.kg (дневной XML).

Курс нужен lalafo-объявлениям с ценой в сомах: у них нет цены в USD,
а аналитика и «ниже рынка» считают по price_usd. Конвертируем только
при отсутствии собственного USD (см. normalize_lalafo_listing).
"""

from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from datetime import date, datetime
from decimal import Decimal

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models import ExchangeRate
from app.services.schedule import local_now

logger = logging.getLogger(__name__)

USD = "USD"


def _parse_nbkr_xml(payload: bytes) -> tuple[date, Decimal]:
    """XML НБКР: <CurrencyRates Date="18.07.2026"><Currency ISOCode="USD">
    <Nominal>1</Nominal><Value>87,4500</Value>... Значения — с запятой."""
    root = ET.fromstring(payload)
    rate_date = datetime.strptime(root.attrib["Date"], "%d.%m.%Y").date()
    for currency in root.iter("Currency"):
        if currency.attrib.get("ISOCode") != USD:
            continue
        nominal = Decimal((currency.findtext("Nominal") or "1").replace(",", "."))
        value = Decimal((currency.findtext("Value") or "").replace(",", "."))
        if nominal <= 0 or value <= 0:
            break
        return rate_date, value / nominal
    raise ValueError("nbkr.kg XML has no valid USD rate")


def fetch_nbkr_usd_rate() -> tuple[date, Decimal]:
    settings = get_settings()
    response = httpx.get(
        settings.nbkr_rates_url,
        timeout=settings.request_timeout_seconds,
        headers={"User-Agent": settings.user_agent},
        follow_redirects=True,
    )
    response.raise_for_status()
    return _parse_nbkr_xml(response.content)


def get_latest_usd_rate(session: Session) -> Decimal | None:
    row = session.scalar(
        select(ExchangeRate)
        .where(ExchangeRate.currency == USD)
        .order_by(ExchangeRate.rate_date.desc())
        .limit(1)
    )
    return row.rate_kgs if row else None


def refresh_usd_rate(session: Session) -> ExchangeRate:
    rate_date, rate = fetch_nbkr_usd_rate()
    row = session.scalar(
        select(ExchangeRate).where(ExchangeRate.currency == USD, ExchangeRate.rate_date == rate_date)
    )
    if row is None:
        row = ExchangeRate(currency=USD, rate_date=rate_date, rate_kgs=rate)
        session.add(row)
    else:
        row.rate_kgs = rate
    session.flush()
    logger.info("nbkr USD rate for %s: %s KGS", rate_date, rate)
    return row


def ensure_fresh_usd_rate(session: Session) -> Decimal | None:
    """Курс на сегодня; если его ещё нет — забирает с nbkr.kg.
    При недоступности сайта возвращает последний известный (или None) —
    сбор данных из-за курса не останавливаем."""
    # Контейнеры живут в UTC, а «день» курса — бишкекский (SCHEDULER_TIMEZONE).
    today = local_now(get_settings().scheduler_timezone).date()
    today_row = session.scalar(
        select(ExchangeRate).where(ExchangeRate.currency == USD, ExchangeRate.rate_date == today)
    )
    if today_row is not None:
        return today_row.rate_kgs
    try:
        row = refresh_usd_rate(session)
        session.commit()
        return row.rate_kgs
    except Exception:
        session.rollback()
        logger.exception("nbkr.kg rate refresh failed; falling back to last known rate")
        return get_latest_usd_rate(session)
