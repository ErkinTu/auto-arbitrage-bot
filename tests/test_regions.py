"""Тесты сопоставления города объявления с регионом КР (app/services/regions.py)."""

from __future__ import annotations

from app.services.regions import (
    BATKEN,
    BISHKEK,
    CHUY,
    FOREIGN,
    ISSYKKUL,
    JALALABAD,
    LOCAL,
    NARYN,
    OSH_CITY,
    OSH_REGION,
    REGION_LABELS,
    TALAS,
    is_valid_region,
    region_matches,
    region_of,
)


def test_region_of_cities() -> None:
    assert region_of("Бишкек") == BISHKEK
    assert region_of("Фрунзе") == BISHKEK  # старое имя Бишкека
    assert region_of("Ош") == OSH_CITY
    assert region_of("Кара-Суу") == OSH_REGION  # город в Ошской обл.
    assert region_of("Токмок") == CHUY
    assert region_of("Каракол") == ISSYKKUL
    assert region_of("Талас") == TALAS
    assert region_of("Нарын") == NARYN
    assert region_of("Базар-Коргон") == JALALABAD
    assert region_of("Баткен") == BATKEN


def test_region_of_verified_tricky() -> None:
    # Частые ошибки, перепроверенные по источникам:
    assert region_of("Казарман") == NARYN  # не Джалал-Абад
    assert region_of("Ичке-Булун") == ISSYKKUL  # не Нарын
    assert region_of("Шамшы") == CHUY  # Чуйский р-н
    # Город Джалал-Абад = Манас, но область осталась Джалал-Абадской.
    assert region_of("Манас (Джалал-Абад)") == JALALABAD
    assert region_of("Джалал-Абад") == JALALABAD


def test_region_of_foreign_and_luxembourg() -> None:
    assert region_of("Китай") == FOREIGN
    assert region_of("Южная Корея") == FOREIGN
    # «Люксембург» как страна — зарубежное; «с. Люксембург» — село в Чуе.
    assert region_of("Люксембург") == FOREIGN
    assert region_of("с. Люксембург") == CHUY


def test_region_of_normalization() -> None:
    # Префиксы «с./село/пос.», скобки-уточнения, ё, регистр.
    assert region_of("с. Ивановка") == CHUY
    assert region_of("село Ленинское") == CHUY
    assert region_of("Аламедин (ГЭС-2)") == CHUY
    assert region_of("  ТОКМОК  ") == CHUY
    assert region_of("Семёновка") == ISSYKKUL  # ё→е


def test_region_of_unknown_and_disputed() -> None:
    assert region_of(None) is None
    assert region_of("") is None
    assert region_of("-1") is None
    # Спорные тёзки не классифицируются (только «Все авто»).
    assert region_of("Кызыл-Туу") is None
    assert region_of("Покровка") is None
    assert region_of("Манас") is None  # без уточнения «(Джалал-Абад)»


def test_is_valid_region() -> None:
    assert is_valid_region(None)  # «Все авто»
    assert all(is_valid_region(code) for code in REGION_LABELS)
    assert is_valid_region(LOCAL)
    assert not is_valid_region("atlantis")


def test_region_matches_local_is_any_non_foreign() -> None:
    # «Местный рынок» — любой не-зарубежный город, включая нераспознанные сёла.
    assert region_matches("Бишкек", LOCAL)
    assert region_matches("Ноокат", LOCAL)
    assert region_matches("Кызыл-Туу", LOCAL)  # спорный, но КР -> местный
    assert region_matches("Неведомоесело", LOCAL)  # неизвестный -> местный
    assert not region_matches("Китай", LOCAL)  # зарубежное исключается
    assert not region_matches("Люксембург", LOCAL)


def test_region_matches_specific() -> None:
    assert region_matches("Бишкек", BISHKEK)
    assert not region_matches("Бишкек", CHUY)
    assert region_matches("Каракол", ISSYKKUL)
    assert not region_matches("Кызыл-Туу", ISSYKKUL)  # спорный -> не проходит


def test_jalalabad_label_shows_manas() -> None:
    assert REGION_LABELS[JALALABAD] == "Джалал-Абадская обл. (Манас)"
