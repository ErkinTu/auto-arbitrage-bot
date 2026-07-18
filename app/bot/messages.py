"""Русские HTML-шаблоны сообщений бота."""

from __future__ import annotations

import html

from app.bot.matching import PendingNotification, is_foreign

START_TEXT = (
    "👋 Привет! Я слежу за новыми объявлениями на mashina.kg и присылаю те, "
    "что подходят под ваши фильтры.\n\n"
    "Команды:\n"
    "/add_filter — создать фильтр (марка, модель, год, пробег, цена, зарубежные авто)\n"
    "/filters — мои фильтры и их удаление\n"
    "/help — справка"
)

HELP_TEXT = (
    "ℹ️ Как это работает:\n\n"
    "1. Создайте фильтр через /add_filter: марка → модель → годы → пробег → цена → "
    "«только ниже рынка» → «зарубежные авто».\n"
    "2. Каждые ~10 минут я проверяю свежие объявления.\n"
    "3. Как только появляется машина под ваш фильтр — присылаю её сюда со ссылкой.\n\n"
    "Любой шаг фильтра можно пропустить — тогда ограничения по нему не будет.\n"
    "«Только ниже рынка» — присылать лишь авто с ценой заметно ниже средней "
    "по таким же марке/модели/году/пробегу.\n"
    "«Зарубежные авто» — объявления из Китая, Южной Кореи, США и т.п.; их цены "
    "обычно без растаможки и доставки, поэтому их можно пропускать.\n\n"
    "/filters — посмотреть и удалить фильтры."
)

ACCESS_DENIED_TEXT = (
    "⛔ Доступ только для участников закрытой группы.\n"
    "Если вы считаете, что это ошибка — свяжитесь с администратором."
)


def _fmt_int(value: int) -> str:
    return f"{value:,}".replace(",", " ")


def format_notification(p: PendingNotification) -> str:
    title = html.escape(p.title) if p.title else "Объявление"
    year = str(p.year) if p.year is not None else "—"
    mileage = f"{_fmt_int(p.mileage_km)} км" if p.mileage_km is not None else "—"
    price = f"${_fmt_int(round(p.price_usd))}" if p.price_usd is not None else "—"
    city = html.escape(p.city) if p.city else "—"

    below = ""
    if p.below_market_percent is not None and p.below_market_percent > 0:
        below = f" · на {p.below_market_percent:g}% ниже средней"

    avg_line = ""
    if p.market_avg_usd is not None:
        avg_line = f"Средняя цена по рынку: ${_fmt_int(round(p.market_avg_usd))}\n"

    foreign_note = ""
    if is_foreign(p.city):
        foreign_note = "🌍 <b>Внимание: авто находится за рубежом</b>\n"

    return (
        "🚗 Новое объявление по вашему фильтру\n\n"
        f"<b>{title}</b>\n"
        f"Год: {year} · Пробег: {mileage}\n"
        f"Цена: <b>{price}</b>{below}\n"
        f"{avg_line}"
        f"Город: {city}\n"
        f"{foreign_note}\n"
        f'<a href="{p.url}">Открыть на {p.source_name}</a>'
    )


def format_filter_line(
    *,
    brand_name: str | None,
    model_name: str | None,
    year_from: int | None,
    year_to: int | None,
    mileage_max_km: int | None,
    price_max_usd: float | None,
    below_market_only: bool,
    below_market_percent: int | None = None,
    skip_foreign: bool = False,
) -> str:
    if brand_name is None:
        parts = ["Все марки"]
    else:
        parts = [f"{brand_name} {model_name}" if model_name else f"{brand_name} (все модели)"]
    if year_from is not None or year_to is not None:
        parts.append(f"{year_from or '…'}–{year_to or '…'} г.")
    if mileage_max_km is not None:
        parts.append(f"до {_fmt_int(mileage_max_km)} км")
    if price_max_usd is not None:
        parts.append(f"до ${_fmt_int(round(price_max_usd))}")
    if below_market_only:
        if below_market_percent is not None:
            parts.append(f"ниже рынка на {below_market_percent}%+")
        else:
            parts.append("только ниже рынка")
    if skip_foreign:
        parts.append("без зарубежных")
    return " · ".join(parts)
