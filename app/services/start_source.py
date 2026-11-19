"""Метки источника в ссылке на бота: `t.me/<бот>?start=<метка>` (план 18.11.2026, № 5).

Реклама и сайт ведут в бота ссылками с меткой: `hrd`, `ig`, `reel`, `market`,
`site`, `site_<id объявления>`. Метка нужна воронке «откуда пришёл → оплатил»
(задача 25), поэтому пишем её двумя способами:

* `bot_users.start_source` — ПЕРВОЕ касание: ставится один раз, на первом /start
  человека. Повторный переход по другой ссылке его не перезаписывает — иначе
  оплату «ig»-человека засчитали бы последнему клику.
* событие `start_link` в `user_events` — КАЖДЫЙ переход с меткой (payload:
  метка и первый ли это старт), чтобы видеть и повторные касания.

Набор меток не закрытый: новая рекламная площадка = новая ссылка, без выката.
Формат — тот же, что Telegram допускает в `start` (A–Z, a–z, 0–9, `_`, `-`, до 64).
Реферальная ссылка `ref_<код>` — тоже источник: «ref».
"""

from __future__ import annotations

import re

from app.services.referral import DEEP_LINK_PREFIX

# Telegram: параметр start — до 64 символов из [A-Za-z0-9_-].
_PAYLOAD_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
SOURCE_REFERRAL = "ref"
SOURCE_SITE_LOGIN = "site_login"


def parse_source(payload: str | None) -> str | None:
    """Метка источника из аргумента /start; None — пусто или мусор."""
    if not payload:
        return None
    payload = payload.strip()
    if not _PAYLOAD_RE.match(payload):
        return None
    if payload.lower().startswith(DEEP_LINK_PREFIX):
        return SOURCE_REFERRAL
    if payload.startswith("login_"):
        return SOURCE_SITE_LOGIN  # вход на сайт кнопкой: nonce в метку не пишем
    return payload.lower()


def source_channel(source: str | None) -> str | None:
    """Канал без хвоста: `site_345153` → `site` (для сводок по каналам)."""
    if not source:
        return None
    return source.split("_", 1)[0]
