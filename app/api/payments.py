"""Публичные payment-эндпоинты FreedomPay (единственное, что торчит наружу).

За Caddy наружу пробрасываются ТОЛЬКО пути /api/v1/payments/*. Безопасность
webhook держится на проверке pg_sig (secret_key знают лишь мы и FreedomPay),
поэтому доп. аутентификация не нужна.
"""

from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation

import httpx
from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.session import get_session
from app.models import Payment
from app.services.freedompay import (
    RESULT_SCRIPT,
    build_callback_response,
    verify_signature,
)
from app.services.subscription import apply_paid_payment

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/payments", tags=["payments"])

_XML = "application/xml"


def _xml(status: str, description: str) -> Response:
    return Response(content=build_callback_response(status, description), media_type=_XML)


@router.post("/result")
async def payment_result(request: Request, session: Session = Depends(get_session)) -> Response:
    """Result URL webhook FreedomPay: подтверждает оплату, продлевает подписку.

    Идемпотентен по pg_order_id (повторный вызов на уже оплаченный — ok).
    Ответ — подписанный XML; при ошибке отвечаем 'error', и FreedomPay повторит.
    """
    settings = get_settings()
    form = await request.form()
    params = {key: str(value) for key, value in form.items()}

    if not verify_signature(RESULT_SCRIPT, params, settings.freedompay_secret_key):
        logger.warning("payment webhook: bad signature (order=%s)", params.get("pg_order_id"))
        return _xml("error", "invalid signature")

    order_id = params.get("pg_order_id")
    payment = session.scalar(select(Payment).where(Payment.order_id == order_id))
    if payment is None:
        logger.warning("payment webhook: unknown order %s", order_id)
        return _xml("error", "unknown order")

    if payment.status == "paid":  # идемпотентность: FreedomPay ретраит вызовы
        return _xml("ok", "already processed")

    if params.get("pg_result") != "1":  # оплата не прошла — фиксируем и подтверждаем
        payment.status = "failed"
        payment.raw_callback = params
        session.commit()
        return _xml("ok", "payment failed noted")

    # Защита: сумма из подписанного webhook должна совпасть с суммой платежа.
    try:
        callback_amount = Decimal(params.get("pg_amount", ""))
    except (InvalidOperation, ValueError):
        callback_amount = None
    if callback_amount is None or callback_amount != payment.amount:
        logger.error(
            "payment webhook: amount mismatch order=%s expected=%s got=%s",
            order_id,
            payment.amount,
            params.get("pg_amount"),
        )
        return _xml("error", "amount mismatch")

    user = apply_paid_payment(
        session,
        payment,
        freedompay_payment_id=params.get("pg_payment_id"),
        raw_callback=params,
    )
    telegram_id = user.telegram_id
    expires = user.subscription_expires_at
    session.commit()

    await _notify_paid(telegram_id, expires)
    return _xml("ok", "subscription activated")


async def _notify_paid(telegram_id: int, expires_at) -> None:
    """Сообщает пользователю об активации подписки прямым вызовом Bot API.

    Отдельный процесс от бота; sendMessage по HTTP не конфликтует с long-polling
    (конфликт даёт лишь второй getUpdates). Best-effort — сбой не валит webhook.
    """
    token = get_settings().telegram_bot_token
    if not token:
        return
    when = expires_at.strftime("%d.%m.%Y") if expires_at else "—"
    text = f"✅ Оплата получена. Подписка активна до {when}.\nДоступ открыт — /add_filter"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            await client.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": telegram_id, "text": text},
            )
    except httpx.HTTPError:
        logger.exception("failed to notify user %s about paid subscription", telegram_id)


_RETURN_PAGE = """<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title></head><body style="font-family:system-ui;text-align:center;padding:3rem 1rem">
<h2>{heading}</h2><p>{body}</p><p>Вернитесь в Telegram-бот.</p></body></html>"""


@router.get("/success", response_class=HTMLResponse)
def payment_success() -> HTMLResponse:
    return HTMLResponse(
        _RETURN_PAGE.format(
            title="Оплата принята",
            heading="✅ Оплата принята",
            body="Подписка будет активирована в течение минуты.",
        )
    )


@router.get("/failure", response_class=HTMLResponse)
def payment_failure() -> HTMLResponse:
    return HTMLResponse(
        _RETURN_PAGE.format(
            title="Оплата не прошла",
            heading="⚠️ Оплата не прошла",
            body="Платёж не завершён. Можно попробовать снова командой /subscribe.",
        )
    )
