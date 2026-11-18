"""Публичный id объявления в ссылках сайта: `/car/Q041bg` вместо `/car/345153`
(решение владельца 18.11.2026, план № 11; разбор — `docks/analysis/22_*.md`, ID-2A).

Число из базы в адресе выдаёт объём базы и позволяет перебирать объявления
подряд. Sqids — обратимая кодировка числа с перемешанным алфавитом: хранить
ничего не нужно, ссылка однозначно ведёт на объявление.

Правила:
* алфавит — `PUBLIC_ID_ALPHABET` в `.env` (перемешанные 62 символа). **Не менять
  после запуска**: все выданные ссылки перестанут открываться. Пусто — публичный
  id выключен (`public_id` = null, сайт ходит по числу, как раньше);
* принимаем только каноническую запись: `decode` Sqids понимает и «чужие»
  строки (любую строку из алфавита превращает в какое-то число), поэтому
  проверяем, что `encode(decode(s)) == s` — иначе одно объявление открывалось бы
  по бесконечному числу адресов;
* числовой id в ручках API пока тоже принимается: старые ссылки и внешний
  мобильный разработчик (сайт делает 301 на новую ссылку; отключить через
  60 дней — план ID-4).
* позже — токен+слаг на группу машины (план ID-2C), Sqids тогда уйдут в 301.
"""

from __future__ import annotations

from functools import lru_cache

from sqids import Sqids

from app.core.config import get_settings

MIN_LENGTH = 6
# Больше миллиарда объявлений не будет; защита от переполнения INTEGER в запросе.
MAX_ID = 2**31 - 1


@lru_cache(maxsize=4)
def _coder(alphabet: str) -> Sqids:
    return Sqids(alphabet=alphabet, min_length=MIN_LENGTH)


def _alphabet() -> str:
    return get_settings().public_id_alphabet


def encode(listing_id: int) -> str | None:
    """Публичный id объявления; None — выключено (пустой алфавит)."""
    alphabet = _alphabet()
    return _coder(alphabet).encode([listing_id]) if alphabet else None


def decode(ref: str | int) -> int | None:
    """id объявления по ссылке: публичный id или (пока) число. None — не наше."""
    text = str(ref).strip()
    if text.isdigit():
        value = int(text)
        return value if 0 < value <= MAX_ID else None
    alphabet = _alphabet()
    if not alphabet or not text:
        return None
    coder = _coder(alphabet)
    numbers = coder.decode(text)
    if len(numbers) != 1 or not 0 < numbers[0] <= MAX_ID:
        return None
    return numbers[0] if coder.encode(numbers) == text else None
