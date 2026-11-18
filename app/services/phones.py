"""Телефон продавца для «написать в WhatsApp» (решение владельца 01.11.2026).

Номер — персональные данные продавца. Правила (см. `docs/planning/PLAN_2026-11-01.md § Ж`):
- храним только цифры номера в `listings.seller_phone` — без имени и без
  названия организации; в `raw_payloads`, логи, админку и API он не попадает;
- продавец скрыл номер на источнике (lalafo `hide_phone`) — не храним, а
  известный ранее стираем;
- снятое объявление номер теряет (`sync._append_status_history`, событие
  `deactivated`) — кроме живой проверки сайта (`keep_phone`, 12.11.2026);
- **признак номера** `listings.seller_phone_hash` (решение владельца 18.11.2026:
  «больше данных лучше, мы не продаём чужие данные») — HMAC-SHA256 цифр с
  ключом `SELLER_PHONE_HASH_KEY` из `.env`. Восстановить по нему номер нельзя
  без ключа (а с ключом — только перебором ~10⁹ номеров КР, поэтому ключ —
  секрет, как токен бота). Нужен, чтобы находить объявления одного продавца
  (дубли, перезаливы) и после снятия: **не стирается никогда**, даже когда
  стирается сам номер. Ставится сам при любой записи номера (событие ORM в
  `models/entities.py`), обходной путь — `bot/db.save_seller_phone` (bulk
  update) — считает его явно; старым — `cli.py phone-hash-backfill`. Как и
  номер, в API, админку и логи не попадает. Пустой ключ — хэш не считается;
- в боте номер отдаётся только под альбомом «Все фото»: для звонка — текстом
  под спойлером, для переписки — кнопкой «Написать в WhatsApp» (wa.me с
  готовым текстом про объявление). Номер всё равно лежит в данных сообщения —
  это барьер от сбора ботами, а не защита от человека. Есть ли у номера
  WhatsApp, проверить нельзя (открытого API нет) — тогда wa.me скажет об этом
  сам, а номер для звонка остаётся.
"""

from __future__ import annotations

import hashlib
import hmac
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


def phone_hash(digits: str | None) -> str | None:
    """HMAC-SHA256 цифр номера (hex, 64 знака); None — нет номера или ключа."""
    from app.core.config import get_settings

    key = get_settings().seller_phone_hash_key
    if not digits or not key:
        return None
    return hmac.new(key.encode(), digits.encode(), hashlib.sha256).hexdigest()


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
