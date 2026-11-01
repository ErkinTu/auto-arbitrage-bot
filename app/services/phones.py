"""Телефон продавца для «написать в WhatsApp» (решение владельца 01.11.2026).

Номер — персональные данные продавца. Правила (см. `docs/planning/PLAN_2026-11-01.md § Ж`):
- храним только цифры номера в `listings.seller_phone` — без имени и без
  названия организации; в `raw_payloads`, логи, админку и API он не попадает;
- продавец скрыл номер на источнике (lalafo `hide_phone`) — не храним, а
  известный ранее стираем;
- снятое объявление номер теряет (`sync._append_status_history`, событие
  `deactivated`);
- в боте номер отдаётся только под альбомом «Все фото»: для звонка — текстом
  под спойлером, для переписки — кнопкой «Написать в WhatsApp» (wa.me с
  готовым текстом про объявление). Номер всё равно лежит в данных сообщения —
  это барьер от сбора ботами, а не защита от человека. Есть ли у номера
  WhatsApp, проверить нельзя (открытого API нет) — тогда wa.me скажет об этом
  сам, а номер для звонка остаётся.
"""

from __future__ import annotations

import re
from urllib.parse import quote

KG_CODE = "996"
_NON_DIGITS = re.compile(r"\D+")


def normalize_phone(value: object) -> str | None:
    """«+996 500 328 668», «0500 328668», «500328668» → «996500328668».

    Короткие (меньше 10 цифр после приведения) и слишком длинные (больше 15 —
    предел E.164) — не номер. Иностранный номер с кодом страны пропускаем как
    есть: wa.me работает с любым международным.
    """
    if value is None:
        return None
    digits = _NON_DIGITS.sub("", str(value))
    if len(digits) == 10 and digits.startswith("0"):  # 0500 123 456 — местный
        digits = KG_CODE + digits[1:]
    elif len(digits) == 9:  # 500 123 456 — без кода и нуля
        digits = KG_CODE + digits
    if not 10 <= len(digits) <= 15:
        return None
    return digits


def display_phone(digits: str) -> str:
    """«996500328668» → «+996 500 328 668»; иностранный — «+<цифры>»."""
    if digits.startswith(KG_CODE) and len(digits) == 12:
        rest = digits[3:]
        return f"+{KG_CODE} {rest[:3]} {rest[3:6]} {rest[6:]}"
    return f"+{digits}"


def whatsapp_url(digits: str, text: str | None = None) -> str:
    """wa.me-ссылка; `text` — готовое первое сообщение (человек может поправить)."""
    url = f"https://wa.me/{digits}"
    return f"{url}?text={quote(text)}" if text else url
