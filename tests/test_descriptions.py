"""Описание продавца на сайте: разбор страницы mashina, маска телефонов, кэш в базе."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import app.services.descriptions as d
from tests.test_public_feed import _listing, db  # noqa: F401 — фикстура


def test_mashina_description_from_escaped_next_data() -> None:
    html = r'xx\"title\":\"Land Rover\",\"description\":\"На заказ!\\n\\nкарфакс \\\"чистый\\\".\",\"category_id\":1,yy'
    assert d.mashina_description_from_page(html) == 'На заказ!\n\nкарфакс "чистый".'
    assert d.mashina_description_from_page("<html></html>") is None


def test_clean_masks_phones_but_keeps_prices_and_years() -> None:
    text = "Звоните +996 (555) 12-34-56 или 0700123456\n\n\n\nЦена 46 600$, 2019 год, пробег 150 000 км"
    cleaned = d.clean_description(text)
    assert "555" not in cleaned and "0700123456" not in cleaned
    assert cleaned.count(d.PHONE_MASK) == 2
    assert "46 600$" in cleaned and "2019" in cleaned and "150 000 км" in cleaned
    assert "\n\n\n" not in cleaned
    assert d.clean_description("   ") is None


def test_description_fetched_once_and_kept(db, monkeypatch) -> None:  # noqa: F811
    """Запасной запрос к странице — один раз; дальше описание из базы бессрочно."""
    Factory, ids = db
    calls = []

    def fake_fetch(source_name, listing, url):
        calls.append(listing.id)
        return "Отличное состояние, тел. 0555 123 456"

    monkeypatch.setattr(d, "_fetch", fake_fetch)
    with Factory() as s:
        listing = _listing(s, ids)
        first = d.listing_description(s, listing.id)
        assert first["text"] == f"Отличное состояние, тел. {d.PHONE_MASK}"
        d.listing_description(s, listing.id, now=datetime.now(timezone.utc) + timedelta(days=365))
        assert calls == [listing.id]


def test_empty_description_not_rechecked(db, monkeypatch) -> None:  # noqa: F811
    Factory, ids = db
    calls = []
    monkeypatch.setattr(d, "_fetch", lambda source, listing, url: calls.append(1) or None)
    with Factory() as s:
        listing = _listing(s, ids)
        d.listing_description(s, listing.id)
        d.listing_description(s, listing.id, now=datetime.now(timezone.utc) + timedelta(days=30))
        assert calls == [1]  # «описания нет» запомнили; появится — допишет обход


def test_source_error_keeps_old_text(db, monkeypatch) -> None:  # noqa: F811
    Factory, ids = db

    def boom(*args):
        raise RuntimeError("timeout")

    monkeypatch.setattr(d, "_fetch", boom)
    with Factory() as s:
        listing = _listing(s, ids, description="было", description_fetched_at=None)
        assert d.listing_description(s, listing.id)["text"] == "было"


def test_lalafo_description_saved_by_sync_and_never_fetched_on_demand(db, monkeypatch) -> None:  # noqa: F811
    """lalafo — только из карточки обхода: запрос через прокси по открытию страницы не делаем."""
    from app.services.normalizer import normalize_lalafo_listing

    Factory, ids = db
    calls = []
    monkeypatch.setattr(d, "_fetch", lambda *a: calls.append(a) or "x")
    with Factory() as s:
        listing = _listing(s, ids, source_id=ids["lalafo"])
        assert d.listing_description(s, listing.id)["text"] is None
        assert calls == []  # к lalafo не ходили
    raw = {"id": 1, "title": "Toyota Camry", "price": 1, "currency": "USD", "description": "Тел 0555123456, торг"}
    item = normalize_lalafo_listing(raw, fallback_make="Toyota")
    assert item.description == "Тел 0555123456, торг"
    assert "description" not in item.analytics_payload()  # вне payload_hash


def test_session_ttl_week_and_admin_hour() -> None:
    from datetime import timedelta as td

    from app.models import BotUser
    from app.services.site_auth import session_ttl

    assert session_ttl(BotUser(is_superuser=False)) == td(days=7)
    assert session_ttl(BotUser(is_superuser=True)) == td(hours=1)
