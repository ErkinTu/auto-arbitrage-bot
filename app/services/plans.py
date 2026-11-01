"""Реестр тарифных планов и правило «с какой скоростью слать уведомления».

Четыре плана = две длины (неделя/месяц) x две скорости (полная / с отставанием
~1 час). Отставание — единственное отличие между «дешёвым» и «дорогим»: фильтры,
аналитика и чёрный список одинаковы у всех (`docs/payments/PRICING_PLAN.md`).

Почему прайс живёт в коде, а не в env: у плана есть не только цена, но и тексты
(подпись кнопки, название в сообщениях), и образ всё равно пересобирается на
каждом деплое. Две правды о цене (переменная на сервере + число в тексте) дают
расхождение вида «в кнопке 990, а списали 1990» — а это возврат, которого у нас
нет вовсе.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from app.models import BotUser
from app.services.trial import is_trial_active

logger = logging.getLogger(__name__)

# Скорость доставки пушей. Хранится в `access_blocks.tier`, поэтому значения
# менять нельзя без миграции данных.
TIER_FULL = "full"
TIER_DELAYED = "delayed"

# Откуда взялся блок доступа (`access_blocks.origin`) — для разбора «за что дни».
ORIGIN_PAYMENT = "payment"
ORIGIN_REFERRAL = "referral"
ORIGIN_MANUAL = "manual"
ORIGIN_BACKFILL = "backfill"


@dataclass(frozen=True, slots=True)
class Plan:
    code: str
    days: int
    price_kgs: Decimal
    tier: str
    title: str  # как план называется в сообщениях
    recommended: bool = False

    @property
    def delayed(self) -> bool:
        return self.tier == TIER_DELAYED

    @property
    def price(self) -> int:
        """Цена целым числом: сомы у нас всегда круглые, а в текстах нужна строка."""
        return int(self.price_kgs)

    @property
    def button_label(self) -> str:
        """Подпись кнопки в меню /subscribe: срок, скорость и цена в одной строке."""
        speed = "с отставанием ~1 ч" if self.delayed else "полная скорость"
        star = "⭐ " if self.recommended else ""
        return f"{star}{self.period_label} · {speed} - {self.price} сом"

    @property
    def period_label(self) -> str:
        return "Месяц" if self.days >= 30 else "Неделя"


# Порядок = порядок кнопок в меню (§7 плана): сначала рекомендуемый месячный
# полный, потом месячный с отставанием, потом недельные. Дорогой первым —
# он же и самый выгодный по цене за день.
PLANS: tuple[Plan, ...] = (
    Plan("month_full", 30, Decimal("1990"), TIER_FULL, "месяц, полная скорость", recommended=True),
    Plan("month_delayed", 30, Decimal("990"), TIER_DELAYED, "месяц, с отставанием ~1 ч"),
    Plan("week_full", 7, Decimal("600"), TIER_FULL, "неделя, полная скорость"),
    Plan("week_delayed", 7, Decimal("300"), TIER_DELAYED, "неделя, с отставанием ~1 ч"),
)

# План по умолчанию для строк `payments` без `plan_code`: до тарифов подписка
# была одна — 30 дней полной скорости (2000 сом). Цена у него теперь другая, но
# для доступа важны только дни и скорость.
DEFAULT_PLAN_CODE = "month_full"

_BY_CODE: dict[str, Plan] = {plan.code: plan for plan in PLANS}


def find_plan(code: str | None) -> Plan | None:
    """Строгий поиск — для того, что пришло от пользователя (callback кнопки).

    Неизвестный код здесь обязан быть отказом, а не «подставим месячный»: иначе
    устаревшая кнопка из истории чата продала бы не тот план.
    """
    if not code:
        return None
    return _BY_CODE.get(code)


def plan_for_code(code: str | None) -> Plan:
    """Мягкий поиск — для уже сохранённых платежей (`payments.plan_code`).

    NULL = исторический месячный полный (см. DEFAULT_PLAN_CODE). Неизвестный код
    тоже отдаёт дефолт, но с warning: платёж уже принят, и отказать в доступе
    из-за нашей же рассинхронизации хуже, чем выдать месяц полной скорости.
    """
    plan = find_plan(code)
    if plan is not None:
        return plan
    if code:
        logger.warning("unknown plan_code %r, falling back to %s", code, DEFAULT_PLAN_CODE)
    return _BY_CODE[DEFAULT_PLAN_CODE]


def effective_tier(
    user: BotUser, active_block_tier: str | None, now: datetime | None = None
) -> str:
    """С какой скоростью человеку слать пуши прямо сейчас.

    Приоритет тот же, что у самого доступа (`bot/db.py::decide_access_status`):
    группа → триал → оплаченный блок → дефолт.

    - **участник закрытой группы** — как админ: бесплатно и без отставания,
      тариф ему не выдаётся и не проверяется;
    - **триал** показывает продукт в лучшем виде: продавать отставание тому, кто
      ещё не понял ценности, — значит продавать вслепую;
    - **дефолт `delayed`** намеренный: любая неизвестность (нет блока, сбой,
      новая ветка кода) даёт медленный, но рабочий доступ. Обратный fail-safe
      раздавал бы полный тариф бесплатно.
    """
    if user.access_status == "member":
        return TIER_FULL
    if is_trial_active(user, now):
        return TIER_FULL
    return active_block_tier or TIER_DELAYED
