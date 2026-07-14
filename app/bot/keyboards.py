from __future__ import annotations

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.bot.db import BrandDTO, FilterDTO, ModelDTO

# Telegram ограничивает inline-клавиатуру ~100 кнопками.
MAX_MODEL_BUTTONS = 96


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


def filters_kb(filters: list[FilterDTO]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for index, item in enumerate(filters, start=1):
        builder.button(
            text=f"🗑 Удалить фильтр {index}",
            callback_data=FilterDeleteCB(filter_id=item.id),
        )
    builder.adjust(1)
    return builder.as_markup()
