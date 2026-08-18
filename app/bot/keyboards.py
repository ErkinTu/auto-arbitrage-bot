from __future__ import annotations

from urllib.parse import quote

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.bot.db import BrandDTO, FilterDTO, ModelDTO
from app.services.plans import PLANS
from app.services.regions import ALL_CARS_CODE, ALL_CARS_LABEL, REGION_CHOICES

# Telegram ограничивает inline-клавиатуру ~100 кнопками.
MAX_MODEL_BUTTONS = 96


def pay_kb(url: str, label: str) -> InlineKeyboardMarkup:
    """Одна кнопка-ссылка на страницу оплаты Finik (QR / банковское приложение)."""
    builder = InlineKeyboardBuilder()
    builder.button(text=label, url=url)
    return builder.as_markup()


class PlanCB(CallbackData, prefix="plan"):
    code: str  # код тарифа из services/plans.py


def plans_kb() -> InlineKeyboardMarkup:
    """Меню тарифов: по кнопке на план, в порядке реестра (рекомендуемый первым).

    По одной кнопке в строку: в подписи и срок, и скорость, и цена — в два
    столбца это не читается на телефоне.
    """
    builder = InlineKeyboardBuilder()
    for plan in PLANS:
        builder.button(text=plan.button_label, callback_data=PlanCB(code=plan.code))
    builder.adjust(1)
    return builder.as_markup()


class TrialCB(CallbackData, prefix="trial"):
    action: str  # "start"


def trial_kb(trial_days: int) -> InlineKeyboardMarkup:
    """Кнопка активации бесплатного периода в онбординге."""
    builder = InlineKeyboardBuilder()
    builder.button(
        text=f"🎁 Начать бесплатный период ({trial_days} дня)",
        callback_data=TrialCB(action="start"),
    )
    return builder.as_markup()


def share_kb(link: str) -> InlineKeyboardMarkup:
    """Кнопка «поделиться» реферальной ссылкой через нативный диалог Telegram."""
    builder = InlineKeyboardBuilder()
    builder.button(
        text="📨 Отправить другу",
        url=f"https://t.me/share/url?url={quote(link, safe='')}",
    )
    return builder.as_markup()


class BrandCB(CallbackData, prefix="brand"):
    brand_id: int


class ModelCB(CallbackData, prefix="model"):
    model_id: int  # 0 = любая модель


class SkipCB(CallbackData, prefix="skip"):
    step: str


class BoolCB(CallbackData, prefix="bool"):
    step: str
    value: bool


class FilterDeleteCB(CallbackData, prefix="fdel"):
    filter_id: int


class PercentCB(CallbackData, prefix="pct"):
    step: str
    value: int


class RegionCB(CallbackData, prefix="region"):
    code: str  # код региона из regions.REGION_CHOICES или ALL_CARS_CODE («Все авто»)


def brands_kb(brands: list[BrandDTO]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="🌐 Все марки", callback_data=BrandCB(brand_id=0))
    for brand in brands:
        builder.button(text=brand.name, callback_data=BrandCB(brand_id=brand.id))
    builder.adjust(2)
    return builder.as_markup()


def models_kb(models: list[ModelDTO]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="🔓 Любая модель", callback_data=ModelCB(model_id=0))
    for model in models[:MAX_MODEL_BUTTONS]:
        builder.button(text=model.name, callback_data=ModelCB(model_id=model.id))
    builder.adjust(2)
    return builder.as_markup()


def skip_kb(step: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="⏭ Пропустить", callback_data=SkipCB(step=step))
    return builder.as_markup()


def yes_no_kb(step: str, yes: str = "✅ Да", no: str = "❌ Нет") -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text=yes, callback_data=BoolCB(step=step, value=True))
    builder.button(text=no, callback_data=BoolCB(step=step, value=False))
    builder.adjust(2)
    return builder.as_markup()


def percent_kb(step: str, values: tuple[int, ...] = (5, 10, 15, 20)) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for value in values:
        builder.button(text=f"{value}%", callback_data=PercentCB(step=step, value=value))
    builder.adjust(4)
    return builder.as_markup()


def region_kb() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text=f"🌐 {ALL_CARS_LABEL}", callback_data=RegionCB(code=ALL_CARS_CODE))
    for code, label in REGION_CHOICES:
        builder.button(text=label, callback_data=RegionCB(code=code))
    # «Все авто» и «Местный рынок» — отдельными строками, остальные регионы по два.
    builder.adjust(1, 1, 2)
    return builder.as_markup()


class BlockMenuCB(CallbackData, prefix="blkm"):
    """Кнопка под карточкой: открыть меню «что именно не показывать»."""

    listing_id: int


class BlockCB(CallbackData, prefix="blk"):
    kind: str  # brand | model | city
    listing_id: int


class BlockDeleteCB(CallbackData, prefix="blkd"):
    block_id: int


def block_menu_kb(listing_id: int) -> InlineKeyboardMarkup:
    """Одна кнопка под карточкой: не мешает читать объявление."""
    builder = InlineKeyboardBuilder()
    builder.button(
        text="🚫 Не показывать такие", callback_data=BlockMenuCB(listing_id=listing_id)
    )
    return builder.as_markup()


def block_choice_kb(
    listing_id: int, *, brand_name: str | None, model_name: str | None, city: str | None
) -> InlineKeyboardMarkup:
    """Второй шаг: чем именно надоело — маркой, моделью или городом.

    Показываем только то, что у объявления заполнено: кнопка «Скрыть город»
    у объявления без города обманывала бы.
    """
    builder = InlineKeyboardBuilder()
    if model_name:
        builder.button(
            text=f"🚫 {model_name}", callback_data=BlockCB(kind="model", listing_id=listing_id)
        )
    if brand_name:
        builder.button(
            text=f"🚫 Вся марка {brand_name}",
            callback_data=BlockCB(kind="brand", listing_id=listing_id),
        )
    if city:
        builder.button(
            text=f"🚫 Город {city}", callback_data=BlockCB(kind="city", listing_id=listing_id)
        )
    builder.button(text="← Отмена", callback_data=BlockMenuCB(listing_id=0))
    builder.adjust(1)
    return builder.as_markup()


def blocks_kb(blocks: list) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for item in blocks:
        builder.button(
            text=f"↩️ Вернуть: {item.label}", callback_data=BlockDeleteCB(block_id=item.id)
        )
    builder.adjust(1)
    return builder.as_markup()


def filters_kb(filters: list[FilterDTO]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for index, item in enumerate(filters, start=1):
        builder.button(
            text=f"🗑 Удалить фильтр {index}",
            callback_data=FilterDeleteCB(filter_id=item.id),
        )
    builder.adjust(1)
    return builder.as_markup()
