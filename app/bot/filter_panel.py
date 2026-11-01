"""Панель фильтра: одно сообщение, 8 шагов со статусом (план 31.10.2026, п. 2.7).

Замена пошагового визарда, где на каждый шаг приходило новое сообщение, а
готовый фильтр можно было только удалить. Здесь — чистые функции без aiogram:
черновик фильтра, отрисовка текста и кнопок, разбор ручного ввода, применение
нажатия. Хендлеры (`handlers.py`) только хранят черновик в FSM и перерисовывают
сообщение.

Черновик — обычный dict (лежит в данных FSM): ключи = поля `user_filters`, плюс
имена марки/модели для показа и `done` — пройденные шаги. Отсутствующее поле =
«любой». ✅ ставится за ПРОЙДЕННЫЙ шаг, а не за непустое значение: «Все марки»
и «вся КР» — тоже ответ (проверка владельцем 01.11.2026: выбранное «Все марки»
оставалось ⬜ и читалось как «не сработало»).

Решения владельца (31.10.2026): «ниже рынка» с процентом — один шаг (5/10/20/не
важно); фильтр без марки разрешён, но с подсказкой про низкий приоритет;
редактирование — кнопкой ✏️ в «Мои фильтры» и командой /edit_filter.

Шаг 8 «Топливо» (план 01.11.2026, п. Е) — единственный с выбором нескольких
вариантов: нажатие переключает тип и оставляет на шаге, «Все типы» сбрасывает.
"""

from __future__ import annotations

import html
import re
from datetime import datetime, timezone

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.bot.messages import format_filter_line
from app.services.filter_presets import BRAND_GROUPS, brand_group_label
from app.services.fuel import (
    FUEL_GROUPS,
    FUEL_LABELS,
    fuel_groups_label,
    pack_fuel_groups,
    unpack_fuel_groups,
)
from app.services.regions import ALL_CARS_CODE, LOCAL, REGION_CHOICES, REGION_LABELS

# Границы ручного ввода — те же, что были у визарда.
MIN_YEAR = 1950
MAX_MILEAGE_KM = 1_500_000
MAX_PRICE_USD = 5_000_000

(
    STEP_BRAND,
    STEP_MODEL,
    STEP_YEARS,
    STEP_MILEAGE,
    STEP_PRICE,
    STEP_MARKET,
    STEP_REGION,
    STEP_FUEL,
) = range(1, 9)
# 8 — и предел Telegram на кнопки в одном ряду (ряд «1…8» под панелью).
STEPS = (
    STEP_BRAND,
    STEP_MODEL,
    STEP_YEARS,
    STEP_MILEAGE,
    STEP_PRICE,
    STEP_MARKET,
    STEP_REGION,
    STEP_FUEL,
)
STEP_TITLES = {
    STEP_BRAND: "Марка",
    STEP_MODEL: "Модель",
    STEP_YEARS: "Годы",
    STEP_MILEAGE: "Пробег",
    STEP_PRICE: "Цена",
    STEP_MARKET: "Рынок",
    STEP_REGION: "Регион",
    STEP_FUEL: "Топливо",
}
# Шаги, где принимается ввод текстом.
TEXT_STEPS = (STEP_YEARS, STEP_MILEAGE, STEP_PRICE)

MODELS_PER_PAGE = 30  # у Toyota 111 моделей, а в сообщении не больше ~100 кнопок
MARKET_PERCENTS = (5, 10, 20)
YEAR_QUICK = (2010, 2015, 2018)
AGE_QUICK = 5
MILEAGE_QUICK = (100_000, 150_000, 200_000)
PRICE_QUICK = (5_000, 10_000, 15_000, 20_000)

LINE = "━━━━━━━━━━━━━━━━"
ALL_CARS_HINT = (
    "💡 Фильтр на все марки без «ниже рынка» - самый широкий: при лимите пушей за "
    "цикл его объявления идут последними."
)


class PanelCB(CallbackData, prefix="fp"):
    """Нажатие в панели. action — что сделать, value — с каким значением."""

    action: str
    value: str = ""


# --- черновик ---------------------------------------------------------------------


def new_draft() -> dict:
    """Новый фильтр: всё «любое», регион — вся КР (зарубежные без растаможки шумят)."""
    return {"region": LOCAL, "below_market_only": False, "done": []}


def mark_done(draft: dict, *steps: int) -> dict:
    done = set(draft.get("done") or ())
    done.update(steps)
    return {**draft, "done": sorted(done)}


def is_done(draft: dict, step: int) -> bool:
    return step in (draft.get("done") or ()) or step_value(draft, step) is not None


def draft_from_filter(item) -> dict:
    """Черновик из готового фильтра (`db.FilterDTO`) — для редактирования."""
    return {
        "brand_id": item.brand_id,
        "brand_name": item.brand_name,
        "brand_group": item.brand_group,
        "car_model_id": item.car_model_id,
        "model_name": item.model_name,
        "year_from": item.year_from,
        "year_to": item.year_to,
        "max_age_years": item.max_age_years,
        "mileage_max_km": item.mileage_max_km,
        "price_max_usd": item.price_max_usd,
        "below_market_only": item.below_market_only,
        "below_market_percent": item.below_market_percent,
        "region": item.region,
        "skip_foreign": item.skip_foreign,
        "fuel_groups": item.fuel_groups,
        # Сохранённый фильтр — каждый шаг уже решён, «любой» тоже.
        "done": list(STEPS),
    }


def filter_fields(draft: dict) -> dict:
    """Поля для `db.create_filter` / `db.update_filter`."""
    below = bool(draft.get("below_market_only"))
    return {
        "brand_id": draft.get("brand_id"),
        "car_model_id": draft.get("car_model_id") if draft.get("brand_id") else None,
        "year_from": draft.get("year_from"),
        "year_to": draft.get("year_to"),
        "mileage_max_km": draft.get("mileage_max_km"),
        "price_max_usd": draft.get("price_max_usd"),
        "below_market_only": below,
        "below_market_percent": draft.get("below_market_percent") if below else None,
        "skip_foreign": bool(draft.get("skip_foreign")),
        "region": draft.get("region"),
        "brand_group": None if draft.get("brand_id") else draft.get("brand_group"),
        "max_age_years": draft.get("max_age_years"),
        "fuel_groups": draft.get("fuel_groups"),
    }


def summary(draft: dict) -> str:
    fields = filter_fields(draft)
    return format_filter_line(
        brand_name=draft.get("brand_name") if fields["brand_id"] else None,
        model_name=draft.get("model_name") if fields["car_model_id"] else None,
        year_from=fields["year_from"],
        year_to=fields["year_to"],
        mileage_max_km=fields["mileage_max_km"],
        price_max_usd=fields["price_max_usd"],
        below_market_only=fields["below_market_only"],
        below_market_percent=fields["below_market_percent"],
        skip_foreign=fields["skip_foreign"],
        region=fields["region"],
        brand_group=fields["brand_group"],
        max_age_years=fields["max_age_years"],
        fuel_groups=fields["fuel_groups"],
    )


# --- значения шагов для показа ----------------------------------------------------


def _fmt(value: float) -> str:
    return f"{int(round(value)):,}".replace(",", " ")


def years_label(draft: dict) -> str | None:
    if draft.get("max_age_years"):
        return f"не старше {draft['max_age_years']} лет"
    year_from, year_to = draft.get("year_from"), draft.get("year_to")
    if year_from and year_to:
        return str(year_from) if year_from == year_to else f"{year_from}-{year_to}"
    if year_from:
        return f"от {year_from}"
    if year_to:
        return f"до {year_to}"
    return None


def step_value(draft: dict, step: int) -> str | None:
    """Выбранное значение шага; None — «любой» (шаг не задан)."""
    if step == STEP_BRAND:
        if draft.get("brand_id"):
            return draft.get("brand_name")
        return brand_group_label(draft.get("brand_group"))
    if step == STEP_MODEL:
        return draft.get("model_name") if draft.get("brand_id") and draft.get("car_model_id") else None
    if step == STEP_YEARS:
        return years_label(draft)
    if step == STEP_MILEAGE:
        value = draft.get("mileage_max_km")
        return f"до {_fmt(value)} км" if value else None
    if step == STEP_PRICE:
        value = draft.get("price_max_usd")
        return f"до ${_fmt(value)}" if value else None
    if step == STEP_MARKET:
        if not draft.get("below_market_only"):
            return None
        percent = draft.get("below_market_percent")
        return f"ниже на {percent}%+" if percent else "только ниже рынка"
    if step == STEP_REGION:
        region = draft.get("region")
        if region == LOCAL:
            return None  # по умолчанию
        return REGION_LABELS.get(region, "все авто, вкл. зарубежные") if region else "все авто, вкл. зарубежные"
    if step == STEP_FUEL:
        return fuel_groups_label(draft.get("fuel_groups"))
    raise ValueError(step)


_EMPTY_LABELS = {
    STEP_BRAND: "все марки",
    STEP_MODEL: "любая",
    STEP_YEARS: "любые",
    STEP_MILEAGE: "любой",
    STEP_PRICE: "любая",
    STEP_MARKET: "не важно",
    STEP_REGION: "вся КР",
    STEP_FUEL: "все типы",
}

_PROMPTS = {
    STEP_BRAND: "Марка. «Все марки» - без ограничения.",
    STEP_MODEL: "Модель выбранной марки.",
    STEP_YEARS: "Годы выпуска. Пример: <code>2015-2022</code>, <code>от 2018</code> или <code>до 2012</code>.",
    STEP_MILEAGE: "Максимальный пробег, км. Пример: <code>150000</code> или <code>150к</code>.",
    STEP_PRICE: "Максимальная цена, $. Пример: <code>15000</code> или <code>15к</code>.",
    STEP_MARKET: "Присылать только авто дешевле средней цены по рынку?",
    STEP_REGION: "Откуда показывать авто. ⚠️ У зарубежных цена обычно без растаможки.",
    STEP_FUEL: (
        "Тип топлива - можно отметить несколько. Плагин-гибриды - в «Гибрид». "
        "Если топливо в объявлении не указано, по выбранным типам оно не придёт."
    ),
}


# --- отрисовка ----------------------------------------------------------------------


def render_text(draft: dict, step: int, *, editing: bool = False, notice: str | None = None) -> str:
    title = "✏️ Изменение фильтра" if editing else "🔧 Новый фильтр"
    lines = [f"<b>{title}</b>", LINE]
    for number in STEPS:
        value = step_value(draft, number)
        shown = html.escape(value) if value else _EMPTY_LABELS[number]
        if number == step:
            mark, tail = "✏️", "  ← сейчас"
        else:
            mark, tail = ("✅" if is_done(draft, number) else "⬜"), ""
        lines.append(f"{mark} {number} {STEP_TITLES[number]}: {shown}{tail}")
    lines.append(LINE)
    lines.append(f"Итог: {html.escape(summary(draft))}")
    if not draft.get("brand_id") and not draft.get("brand_group") and not draft.get("below_market_only"):
        lines.append(ALL_CARS_HINT)
    lines.append("")
    if notice:
        lines.append(f"⚠️ {html.escape(notice)}")
    prompt = _PROMPTS[step]
    if step == STEP_MODEL and not draft.get("brand_id"):
        prompt = "Модель выбирается после марки - сначала шаг 1."
    if step in TEXT_STEPS:
        prompt += "\nМожно написать своё значение сообщением."
    lines.append(prompt)
    return "\n".join(lines)


def _button(builder: InlineKeyboardBuilder, text: str, action: str, value: object = "") -> None:
    builder.button(text=text, callback_data=PanelCB(action=action, value=str(value)))


def _chosen(text: str, chosen: bool) -> str:
    return f"• {text} •" if chosen else text


def render_keyboard(
    draft: dict,
    step: int,
    *,
    editing: bool = False,
    brands=(),
    models=(),
    model_page: int = 0,
) -> InlineKeyboardMarkup:
    """Кнопки текущего шага + ряд 1–7 + «Создать/Сохранить» и «Отмена»."""
    builder = InlineKeyboardBuilder()
    sizes: list[int] = []

    if step == STEP_BRAND:
        _button(builder, _chosen("🌐 Все марки", not draft.get("brand_id") and not draft.get("brand_group")), "brand", 0)
        sizes.append(1)
        for code, (label, _) in BRAND_GROUPS.items():
            _button(builder, _chosen(f"🧩 {label}", draft.get("brand_group") == code and not draft.get("brand_id")), "group", code)
            sizes.append(1)
        for brand in brands:
            _button(builder, _chosen(brand.name, draft.get("brand_id") == brand.id), "brand", brand.id)
        sizes.extend(_pairs(len(brands)))
    elif step == STEP_MODEL:
        if draft.get("brand_id"):
            _button(builder, _chosen("🔓 Любая модель", not draft.get("car_model_id")), "model", 0)
            sizes.append(1)
            pages = max(1, -(-len(models) // MODELS_PER_PAGE))
            page = min(max(model_page, 0), pages - 1)
            chunk = list(models)[page * MODELS_PER_PAGE : (page + 1) * MODELS_PER_PAGE]
            for model in chunk:
                _button(builder, _chosen(model.name, draft.get("car_model_id") == model.id), "model", model.id)
            sizes.extend(_pairs(len(chunk)))
            if pages > 1:
                _button(builder, "◀", "mpage", (page - 1) % pages)
                _button(builder, f"{page + 1}/{pages}", "noop")
                _button(builder, "▶", "mpage", (page + 1) % pages)
                sizes.append(3)
        else:
            _button(builder, "→ 1 Марка", "step", STEP_BRAND)
            sizes.append(1)
    elif step == STEP_YEARS:
        for year in YEAR_QUICK:
            current = draft.get("year_from") == year and not draft.get("year_to") and not draft.get("max_age_years")
            _button(builder, _chosen(f"{year}+", current), "years", year)
        _button(builder, _chosen(f"Новее {AGE_QUICK} лет", draft.get("max_age_years") == AGE_QUICK), "years", f"age{AGE_QUICK}")
        _button(builder, _chosen("Любые", years_label(draft) is None), "years", "any")
        _button(builder, "✍️ Ввести свои", "input", step)
        sizes.extend([4, 2])
    elif step == STEP_MILEAGE:
        for value in MILEAGE_QUICK:
            _button(builder, _chosen(f"до {_fmt(value)}", draft.get("mileage_max_km") == value), "mileage", value)
        _button(builder, _chosen("Любой", not draft.get("mileage_max_km")), "mileage", "any")
        _button(builder, "✍️ Ввести свой", "input", step)
        sizes.extend([3, 2])
    elif step == STEP_PRICE:
        for value in PRICE_QUICK:
            _button(builder, _chosen(f"${value // 1000}k", draft.get("price_max_usd") == value), "price", value)
        _button(builder, _chosen("Любая", not draft.get("price_max_usd")), "price", "any")
        _button(builder, "✍️ Ввести свою", "input", step)
        sizes.extend([4, 2])
    elif step == STEP_MARKET:
        below = bool(draft.get("below_market_only"))
        for percent in MARKET_PERCENTS:
            _button(builder, _chosen(f"ниже на {percent}%", below and draft.get("below_market_percent") == percent), "market", percent)
        _button(builder, _chosen("Не важно", not below), "market", "off")
        sizes.extend([3, 1])
    elif step == STEP_REGION:
        region = draft.get("region")
        _button(builder, _chosen("🌐 Все авто (вкл. зарубежные)", region is None), "region", ALL_CARS_CODE)
        sizes.append(1)
        for code, label in REGION_CHOICES:
            _button(builder, _chosen(label, region == code), "region", code)
        sizes.extend([1, *_pairs(len(REGION_CHOICES) - 1)])
    elif step == STEP_FUEL:
        chosen = unpack_fuel_groups(draft.get("fuel_groups"))
        _button(builder, _chosen("🌐 Все типы", not chosen), "fuel", FUEL_ALL)
        sizes.append(1)
        for code in FUEL_GROUPS:
            mark = "✅" if code in chosen else "▫️"
            _button(builder, f"{mark} {FUEL_LABELS[code]}", "fuel", code)
        sizes.extend(_pairs(len(FUEL_GROUPS)))

    for number in STEPS:
        _button(builder, f"[{number}]" if number == step else str(number), "step", number)
    sizes.append(len(STEPS))
    _button(builder, "💾 Сохранить" if editing else "💾 Создать фильтр", "save")
    _button(builder, "✖ Отмена", "cancel")
    sizes.append(2)
    builder.adjust(*sizes)
    return builder.as_markup()


def _pairs(count: int) -> list[int]:
    return [2] * (count // 2) + ([1] if count % 2 else [])


# --- применение нажатий -------------------------------------------------------------


def next_step(step: int) -> int:
    return min(step + 1, STEPS[-1])


_ACTION_STEPS = {
    "brand": STEP_BRAND,
    "group": STEP_BRAND,
    "model": STEP_MODEL,
    "years": STEP_YEARS,
    "mileage": STEP_MILEAGE,
    "price": STEP_PRICE,
    "market": STEP_MARKET,
    "region": STEP_REGION,
    "fuel": STEP_FUEL,
}
# Выбор нескольких вариантов: нажатие не уводит со шага.
_STAY_ACTIONS = frozenset({"fuel"})
FUEL_ALL = "all"


def apply_choice(draft: dict, action: str, value: str) -> tuple[dict, bool]:
    """Применяет кнопку выбора; возвращает (черновик, перейти ли к следующему шагу).

    Топливо — переключатель: черновик меняется, но шаг остаётся (False).

    Принятый выбор отмечает шаг пройденным; «Все марки» и группа — ещё и шаг
    модели (выбирать её не из чего). Неизвестное значение (старая кнопка,
    подделанный callback) не меняет черновик.
    """
    new, accepted = _apply_choice(draft, action, value)
    if not accepted:
        return draft, False
    steps = [_ACTION_STEPS[action]]
    if action in ("brand", "group") and not new.get("brand_id"):
        steps.append(STEP_MODEL)
    return mark_done(new, *steps), action not in _STAY_ACTIONS


def _apply_choice(draft: dict, action: str, value: str) -> tuple[dict, bool]:
    draft = dict(draft)
    if action == "brand":
        brand_id = int(value) if value.isdigit() else -1
        if brand_id < 0:
            return draft, False
        if brand_id != draft.get("brand_id"):
            draft.update(car_model_id=None, model_name=None)
        draft.update(brand_id=brand_id or None, brand_group=None)
        if not brand_id:
            draft["brand_name"] = None
        return draft, True
    if action == "group":
        if value not in BRAND_GROUPS:
            return draft, False
        draft.update(brand_group=value, brand_id=None, brand_name=None, car_model_id=None, model_name=None)
        return draft, True
    if action == "model":
        if not value.isdigit():
            return draft, False
        draft["car_model_id"] = int(value) or None
        if not draft["car_model_id"]:
            draft["model_name"] = None
        return draft, True
    if action == "years":
        if value == "any":
            draft.update(year_from=None, year_to=None, max_age_years=None)
        elif value.startswith("age") and value[3:].isdigit():
            draft.update(year_from=None, year_to=None, max_age_years=int(value[3:]))
        elif value.isdigit():
            draft.update(year_from=int(value), year_to=None, max_age_years=None)
        else:
            return draft, False
        return draft, True
    if action in ("mileage", "price"):
        key = "mileage_max_km" if action == "mileage" else "price_max_usd"
        if value == "any":
            draft[key] = None
        elif value.isdigit():
            draft[key] = int(value) if action == "mileage" else float(value)
        else:
            return draft, False
        return draft, True
    if action == "market":
        if value == "off":
            draft.update(below_market_only=False, below_market_percent=None)
        elif value.isdigit():
            draft.update(below_market_only=True, below_market_percent=int(value))
        else:
            return draft, False
        return draft, True
    if action == "region":
        if value == ALL_CARS_CODE:
            draft["region"] = None
        elif value in REGION_LABELS:
            draft["region"] = value
        else:
            return draft, False
        # Явно выбранный регион отменяет legacy-флаг «без зарубежных».
        draft["skip_foreign"] = False
        return draft, True
    if action == "fuel":
        if value == FUEL_ALL:
            draft["fuel_groups"] = None
        elif value in FUEL_LABELS:
            chosen = set(unpack_fuel_groups(draft.get("fuel_groups")))
            chosen ^= {value}
            # Пусто или все пять — это «все типы» (None), см. pack_fuel_groups.
            draft["fuel_groups"] = pack_fuel_groups(chosen)
        else:
            return draft, False
        return draft, True
    return draft, False


# --- ручной ввод ----------------------------------------------------------------------

_YEAR_RANGE_RE = re.compile(r"^\s*(\d{4})\s*[-–—]\s*(\d{4})\s*$")
_YEAR_FROM_RE = re.compile(r"^\s*(?:от|с|после|>=?)\s*(\d{4})\s*(?:г\.?|года?)?\s*$|^\s*(\d{4})\s*\+\s*$", re.I)
_YEAR_TO_RE = re.compile(r"^\s*(?:до|по|<=?)\s*(\d{4})\s*(?:г\.?|года?)?\s*$", re.I)
_YEAR_ONE_RE = re.compile(r"^\s*(\d{4})\s*(?:г\.?|год)?\s*$", re.I)
_AMOUNT_RE = re.compile(r"^\s*(?:до\s*)?\$?\s*([\d\s.,]+?)\s*(к|k|тыс\.?|т)?\s*(?:км|km|\$|usd|долл\.?)?\s*$", re.I)


def _max_year() -> int:
    return datetime.now(timezone.utc).year + 1


def parse_years(text: str) -> dict | None:
    """Годы из текста: «2015-2022», «от 2018», «2018+», «до 2012», «2018».

    Возвращает поля черновика или None, если не разобрали / год вне границ.
    """
    year_from = year_to = None
    if match := _YEAR_RANGE_RE.match(text):
        year_from, year_to = sorted((int(match.group(1)), int(match.group(2))))
    elif match := _YEAR_FROM_RE.match(text):
        year_from = int(match.group(1) or match.group(2))
    elif match := _YEAR_TO_RE.match(text):
        year_to = int(match.group(1))
    elif match := _YEAR_ONE_RE.match(text):
        year_from = year_to = int(match.group(1))
    else:
        return None
    for year in (year_from, year_to):
        if year is not None and not MIN_YEAR <= year <= _max_year():
            return None
    return {"year_from": year_from, "year_to": year_to, "max_age_years": None}


def parse_amount(text: str, maximum: int) -> int | None:
    """Число из «150000», «150 000», «150к», «$15k», «до 15 000 км»; None — не разобрали."""
    match = _AMOUNT_RE.match(text or "")
    if match is None:
        return None
    digits = re.sub(r"[\s.,]", "", match.group(1))
    if not digits.isdigit():
        return None
    value = int(digits) * (1000 if match.group(2) else 1)
    return value if 0 < value <= maximum else None


def apply_text(draft: dict, step: int, text: str) -> tuple[dict, str | None]:
    """Ручной ввод для шага. Возвращает (черновик, ошибка для показа или None)."""
    draft = dict(draft)
    if step == STEP_YEARS:
        years = parse_years(text)
        if years is None:
            return draft, f"Не понял годы. Пример: 2015-2022, от 2018, до 2012 (с {MIN_YEAR} по {_max_year()})."
        draft.update(years)
        return mark_done(draft, step), None
    if step == STEP_MILEAGE:
        value = parse_amount(text, MAX_MILEAGE_KM)
        if value is None:
            return draft, "Не понял пробег. Пример: 150000 или 150к."
        draft["mileage_max_km"] = value
        return mark_done(draft, step), None
    if step == STEP_PRICE:
        value = parse_amount(text, MAX_PRICE_USD)
        if value is None:
            return draft, "Не понял цену. Пример: 15000 или 15к."
        draft["price_max_usd"] = float(value)
        return mark_done(draft, step), None
    return draft, "На этом шаге выберите вариант кнопкой."
