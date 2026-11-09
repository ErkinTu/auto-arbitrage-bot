import pytest

from app.services.specs import raw_values_for, spec_group


@pytest.mark.parametrize(
    ("field", "raw", "code"),
    [
        ("body_type", "внедорожник 5 дв.", "suv"),
        ("body_type", "внедорожник_5_дв", "suv"),
        ("body_type", "Кроссовер", "suv"),
        ("body_type", "Хэтчбэк", "hatchback"),
        ("body_type", "хэтчбек 3 дв.", "hatchback"),
        ("body_type", "пикап двойная кабина", "pickup"),
        ("body_type", "компактвэн", "minivan"),
        ("body_type", "Бус", "van"),
        ("body_type", "Roadster", "convertible"),
        ("body_type", "-1", None),
        ("body_type", "Лимузин", None),
        ("gearbox", "{name: Автомат, category_id: 1}", "automatic"),
        ("gearbox", "Типтроник", "automatic"),
        ("gearbox", "механика", "manual"),
        ("gearbox", "-1", None),
        ("drive_type", "4WD, полный", "awd"),
        ("drive_type", "AWD, полный", "awd"),
        ("drive_type", "Передний", "front"),
        ("steering_wheel", "слева", "left"),
        ("steering_wheel", "Справа", "right"),
    ],
)
def test_spec_group(field, raw, code) -> None:
    assert spec_group(field, raw) == code


def test_raw_values_for() -> None:
    values = {"седан": 3, "Седан": 2, "Кроссовер": 1, "внедорожник 5 дв.": 4, "-1": 1}
    assert raw_values_for("body_type", values, ["suv"]) == ["Кроссовер", "внедорожник 5 дв."]
    assert raw_values_for("body_type", values, ["sedan", "suv"]) == [
        "Кроссовер", "Седан", "внедорожник 5 дв.", "седан",
    ]
