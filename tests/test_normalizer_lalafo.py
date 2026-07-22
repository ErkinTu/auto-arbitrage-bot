from datetime import timezone
from decimal import Decimal

from app.services.normalizer import normalize_lalafo_listing


def _detail_payload(**overrides):
    payload = {
        "id": 112331785,
        "title": "Toyota Camry: 2016 г., 2.5 л, Вариатор, Гибрид, Седан",
        "url": "/bishkek/ads/toyota-camry-2016-g-25-l-variator-gibrid-sedan-id-112331785",
        "price": 15500,
        "currency": "USD",
        "national_price": {"price": 1355475, "currency": {"id": 417, "code": "KGS"}},
        "city": "Бишкек",
        "created_time": 1784050897,
        "updated_time": 1784343704,
        "status_id": 2,
        "user": {"pro": False, "username": "seller", "id": 1},
        "mobile": "+996700000000",
        "params": [
            {"id": 49, "name": "Модель", "value": "Camry"},
            {"id": 62, "name": "Год", "value": "2016"},
            {"id": 56, "name": "Пробег (км.)", "value": "182000"},
            {"id": 65, "name": "Топливо", "value": "Бензин, Гибрид"},
            {"id": 63, "name": "Кузов", "value": "Седан"},
            {"id": 64, "name": "Коробка передач", "value": "Вариатор"},
            {"id": 106, "name": "Руль", "value": "Слева"},
            {"id": 244, "name": "Привод", "value": "Передний"},
            {"id": 105, "name": "Цвет", "value": "Белый"},
            {"id": 2, "name": "Объем двигателя", "value": "2.5"},
            {"id": 3, "name": "Растаможка", "value": "Растаможен"},
            {"id": 4, "name": "Страна учета", "value": "Кыргызстан"},
        ],
    }
    payload.update(overrides)
    return payload


def test_normalize_lalafo_detail_usd() -> None:
    item = normalize_lalafo_listing(_detail_payload(), fallback_make="Toyota")

    assert item.external_id == "112331785"
    assert item.make == "Toyota"
    assert item.source_model_text == "Camry"
    assert item.year == 2016
    assert item.mileage_km == 182000
    assert item.gearbox == "Вариатор"
    assert item.engine_volume == Decimal("2.5")
    assert item.customs_status == "cleared"
    assert item.price_currency == "USD"
    assert item.price_usd == Decimal("15500")
    assert item.price_kgs == Decimal("1355475")
    assert item.city == "Бишкек"
    assert item.source_created_at is not None
    assert item.source_created_at.tzinfo == timezone.utc
    # Контакты продавца не должны утечь в safe_payload.
    flat = str(item.safe_payload)
    assert "+996" not in flat
    assert "seller" not in flat


def test_normalize_lalafo_kgs_price_has_no_usd() -> None:
    item = normalize_lalafo_listing(
        _detail_payload(price=395000, currency="KGS", national_price={"price": 395000}),
        fallback_make="Toyota",
    )
    assert item.price_currency == "KGS"
    assert item.price_kgs == Decimal("395000")
    assert item.price_usd is None


def test_normalize_lalafo_kgs_converted_by_nbkr_rate() -> None:
    item = normalize_lalafo_listing(
        _detail_payload(price=395000, currency="KGS", national_price={"price": 395000}),
        fallback_make="Toyota",
        usd_rate_kgs=Decimal("87.45"),
    )
    assert item.price_usd == Decimal("4517")  # 395000 / 87.45, целые доллары
    assert "usd_converted" in item.quality_flags


def test_normalize_lalafo_own_usd_not_overwritten_by_rate() -> None:
    item = normalize_lalafo_listing(_detail_payload(), fallback_make="Toyota", usd_rate_kgs=Decimal("87.45"))
    assert item.price_usd == Decimal("15500")  # собственный USD продавца
    assert "usd_converted" not in item.quality_flags


def test_normalize_lalafo_feed_item_falls_back_to_title() -> None:
    item = normalize_lalafo_listing(_detail_payload(params=[]), fallback_make="Toyota")
    assert item.source_model_text == "Camry"
    assert item.year == 2016
    assert item.mileage_km is None


def test_normalize_lalafo_junk_price_flagged() -> None:
    item = normalize_lalafo_listing(
        _detail_payload(price=180, currency="KGS", national_price={"price": 180}),
        fallback_make="Toyota",
        usd_rate_kgs=Decimal("87.45"),
    )
    assert item.price_usd == Decimal("2")
    assert "price_implausibly_low" in item.quality_flags


def test_normalize_lalafo_placeholder_price_flagged_high() -> None:
    item = normalize_lalafo_listing(
        _detail_payload(price=999_999_999, currency="USD", national_price={}),
        fallback_make="Toyota",
    )
    assert item.price_usd == Decimal("999999999")
    assert "price_implausibly_high" in item.quality_flags


def test_normalize_lalafo_other_model_placeholder() -> None:
    payload = _detail_payload(
        title="Zeekr : | 2021 г. Кроссовер",
        params=[{"id": 49, "name": "Модель", "value": "Другая модель"}],
    )
    item = normalize_lalafo_listing(payload, fallback_make="Zeekr")
    assert item.source_model_text is None
