from app.services.normalizer import normalize_mashina_listing


def test_normalize_audi_q5() -> None:
    raw = {
        "id": 10063047,
        "title": "Продажа Audi Q5 II (FY) 2019",
        "slug": "audi-q5-example",
        "prices": [
            {"currency": "USD", "amount": 21000, "is_original": False},
            {"currency": "KGS", "amount": 1836450, "is_original": True},
        ],
        "status": "active",
        "created_at": "2026-06-16T18:18:29.933087+06:00",
        "attributes": [
            {"slug": "year", "value_number": 2019},
            {"slug": "mileage", "value_number": 119000},
            {"slug": "engine_volume", "value_number": 2.0},
            {"slug": "gearbox", "value_text": "робот"},
        ],
        "user": {"name": "Private", "phones": ["secret"], "is_verified_seller": False},
    }

    result = normalize_mashina_listing(raw)

    assert result.external_id == "10063047"
    assert result.make == "Audi"
    assert result.model == "Q5"
    assert result.generation == "II (FY)"
    assert result.year == 2019
    assert result.mileage_km == 119000
    assert result.price_usd == 21000
    assert "user" not in result.safe_payload
    assert "phones" not in str(result.safe_payload)


def test_normalizer_uses_filtered_brand_when_title_is_nonstandard() -> None:
    from app.services.normalizer import normalize_mashina_listing

    item = normalize_mashina_listing(
        {
            "id": 42,
            "title": "Продаю X5 G05 2020",
            "prices": [{"is_original": True, "currency": "USD", "amount": 50000}],
            "status": "active",
            "attributes": [{"slug": "year", "value_number": 2020}],
        },
        fallback_make="BMW",
    )

    assert item.make == "BMW"
    assert item.year == 2020
    assert item.model == "X5"
