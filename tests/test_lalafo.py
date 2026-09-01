"""Разрешение марки в раздел lalafo: слаги страниц марок."""

from __future__ import annotations

from app.parsers.lalafo import LalafoParser


def test_slug_candidates_prefers_explicit_override():
    """«ВАЗ (Lada)» латиницей даёт `lada`, а страница марки живёт под `vaz-lada`.

    Регрессия: без карты исключений обход lalafo по Lada падал с
    LalafoBrandNotFoundError, то есть марка молча оставалась одноисточниковой.
    """
    candidates = LalafoParser._slug_candidates("ВАЗ (Lada)")
    assert candidates[0] == "vaz-lada"
    # Догадки по имени остаются запасным вариантом, а не заменяются.
    assert "lada" in candidates


def test_slug_candidates_without_override_are_guessed_from_name():
    assert LalafoParser._slug_candidates("Land Rover") == [
        "land-rover",
        "land-rover-avtomobili-s-probegom",
    ]
