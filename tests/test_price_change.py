"""Смена цены — только настоящая (разбор 16.11.2026, TODO § «Ложная смена цены»).

mashina отдаёт сомы, пересчитанные по своему курсу, и полный обход каждую
субботу «менял» цену почти всем объявлениям: $33 800 → $33 800, а сомы
2 955 810 → 2 955 769. Курс двигает одну из двух цен, продавец — обе.
"""

from __future__ import annotations

from decimal import Decimal as D
from types import SimpleNamespace

import pytest

from app.services.sync import is_real_price_change, price_change_details


def _listing(usd, kgs, amount=None):
    return SimpleNamespace(current_price_usd=usd, current_price_kgs=kgs, current_price_amount=amount)


def _item(usd, kgs, amount=None):
    return SimpleNamespace(price_usd=usd, price_kgs=kgs, price_amount=amount, quality_flags=[])


@pytest.mark.parametrize(
    ("old", "new", "changed"),
    [
        # mashina, цена в долларах: сомы гуляют по курсу — не смена (данные прода).
        ((D("33800"), D("2955810")), (D("33800"), D("2955769")), False),
        # Цена в сомах: доллары гуляют по курсу ($3 316 → $3 317) — не смена.
        ((D("3316"), D("290000")), (D("3317"), D("290000")), False),
        # Продавец поменял цену — меняются обе.
        ((D("51000"), D("4459950")), (D("53000"), D("4634786")), True),
        # Ничего не поменялось.
        ((D("10000"), D("874500")), (D("10000"), D("874500")), False),
        # Сомов нет ни до, ни после — по долларам.
        ((D("10000"), None), (D("9500"), None), True),
        # Сомы появились, доллары те же — не смена (у lalafo пересчёт бывает не всегда).
        ((D("10000"), None), (D("10000"), D("874500")), False),
        # Цены не было — появилась: смена.
        ((None, None), (D("10000"), D("874500")), True),
    ],
)
def test_real_price_change(old, new, changed) -> None:
    assert is_real_price_change(_listing(*old), _item(*new)) is changed


def test_amount_used_when_no_kgs() -> None:
    """lalafo в сомах без отдельного price_kgs: сумма продавца + наш пересчёт в доллары."""
    assert is_real_price_change(_listing(D("3316"), None, D("290000")), _item(D("3317"), None, D("290000"))) is False
    assert is_real_price_change(_listing(D("3316"), None, D("290000")), _item(D("3200"), None, D("280000"))) is True


def test_change_direction() -> None:
    assert price_change_details(D("21000"), D("20000"))["direction"] == "down"
    assert price_change_details(D("20000"), D("21000"))["direction"] == "up"
    assert price_change_details(D("20000"), D("20000")) is None


def test_cleanup_drops_only_rate_noise() -> None:
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import sessionmaker

    from app.db.base import Base
    from app.models import Brand, Listing, ListingPriceHistory, Source
    from app.services.price_history_cleanup import delete_rows, find_false_changes

    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    src, brand = Source(name="mashina.kg"), Brand(name="Toyota")
    s.add_all([src, brand])
    s.flush()
    car = Listing(source_id=src.id, external_id="1", brand_id=brand.id, title="t", payload_hash="x")
    s.add(car)
    s.flush()
    t0 = datetime(2026, 7, 1, tzinfo=timezone.utc)
    rows = [  # данные прода: объявление 4406357
        ("initial", "51000", "4459950"),
        ("price_changed", "53000", "4634786"),  # продавец поднял — оставить
        ("price_changed", "53000", "4634850"),  # курс — удалить
        ("price_changed", "53000", "4634802"),  # курс — удалить
        ("price_changed", "52000", "4547000"),  # продавец снизил — оставить
    ]
    for i, (event, usd, kgs) in enumerate(rows):
        s.add(ListingPriceHistory(listing_id=car.id, event_type=event, price_usd=D(usd), price_kgs=D(kgs),
                                  observed_at=t0 + timedelta(days=i)))
    s.commit()
    ids, report = find_false_changes(s)
    assert report.false_changes == 2
    delete_rows(s, ids)
    left = s.scalars(select(ListingPriceHistory.price_usd).order_by(ListingPriceHistory.observed_at)).all()
    assert left == [D("51000"), D("53000"), D("52000")]
