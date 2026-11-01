"""Телефон продавца для WhatsApp под альбомом (план 01.11.2026, п. Ж).

Номер — персональные данные: только цифры, вне raw_payloads и payload_hash,
стирается при деактивации и когда продавец скрыл его на источнике.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import select

from app.models import Listing, RawPayload
from app.services.normalizer import normalize_lalafo_listing
from app.services.phones import display_phone, normalize_phone, whatsapp_url
from app.services.sync import deactivate_by_card, run_sync
from tests.test_rich_cards import _FeedParser, _lalafo_item, _lalafo_setup, _sync_session


@pytest.mark.parametrize(
    ("raw", "digits"),
    [
        ("+996 500 328 668", "996500328668"),
        ("996500328668", "996500328668"),
        ("0500 328 668", "996500328668"),
        ("500328668", "996500328668"),
        ("+7 (916) 123-45-67", "79161234567"),  # иностранный — как есть
        ("12345", None),
        ("", None),
        (None, None),
    ],
)
def test_normalize_phone(raw, digits) -> None:
    assert normalize_phone(raw) == digits


def test_display_and_whatsapp() -> None:
    assert display_phone("996500328668") == "+996 500 328 668"
    assert display_phone("79161234567") == "+79161234567"
    assert whatsapp_url("996500328668") == "https://wa.me/996500328668"
    assert whatsapp_url("996500328668", "Привет, 1 & 2?") == (
        "https://wa.me/996500328668?text=%D0%9F%D1%80%D0%B8%D0%B2%D0%B5%D1%82%2C%201%20%26%202%3F"
    )


def _item(**extra) -> dict:
    item = _lalafo_item(None)
    item.update(extra)
    return item


def test_lalafo_normalizer_takes_phone_outside_hash() -> None:
    with_phone = normalize_lalafo_listing(_item(mobile="+996500328668"), fallback_make="Toyota")
    without = normalize_lalafo_listing(_item(), fallback_make="Toyota")
    assert with_phone.seller_phone == "996500328668"
    assert with_phone.payload_hash == without.payload_hash  # выкат не пометит базу «updated»
    hidden = normalize_lalafo_listing(_item(mobile="+996500328668", hide_phone=True), fallback_make="Toyota")
    assert (hidden.seller_phone, hidden.seller_phone_hidden) == (None, True)


def _sync(session, source, target, feed: dict, detail: dict | None = None) -> None:
    run_sync(session, _FeedParser([feed], {101: detail or feed}), source, target, mode="fresh")


def test_sync_stores_phone_but_never_in_raw_payloads() -> None:
    session = _sync_session()
    source, target = _lalafo_setup(session)
    _sync(session, source, target, _item(mobile="+996 500 328 668"))
    listing = session.scalar(select(Listing))
    assert listing.seller_phone == "996500328668"
    payloads = [json.dumps(p, ensure_ascii=False) for p in session.scalars(select(RawPayload.payload))]
    assert payloads and not any("328" in p or "mobile" in p for p in payloads)


def test_known_listing_gets_phone_from_feed_and_loses_it_when_hidden() -> None:
    session = _sync_session()
    source, target = _lalafo_setup(session)
    _sync(session, source, target, _item())  # увидели до колонки — номера нет
    listing = session.scalar(select(Listing))
    assert listing.seller_phone is None

    _sync(session, source, target, _item(mobile="+996500328668"))  # та же цена — путь ленты
    session.refresh(listing)
    assert listing.seller_phone == "996500328668"

    _sync(session, source, target, _item(hide_phone=True))
    session.refresh(listing)
    assert listing.seller_phone is None


def test_deactivation_erases_phone() -> None:
    session = _sync_session()
    source, target = _lalafo_setup(session)
    _sync(session, source, target, _item(mobile="+996500328668"))
    listing = session.scalar(select(Listing))
    deactivate_by_card(session, listing, reason="card_gone")
    session.commit()
    session.refresh(listing)
    assert listing.is_active is False and listing.seller_phone is None
