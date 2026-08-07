"""Публичные payment-эндпоинты Finik (единственное, что торчит наружу).

За Caddy наружу пробрасываются ТОЛЬКО пути /api/v1/payments/*. Безопасность
webhook держится на RSA-подписи Finik (проверяем их публичным ключом по среде),
поэтому доп. аутентификация не нужна.

Коды ответов заданы осмысленно, потому что Finik ретраит доставку (5 попыток по
30 с, дальше удвоение до суток):
  200 — подписка продлена (или уже была продлена по этому платежу);
  400/401 — тело/подпись не в порядке, продлевать нечего;
  500 — наша ошибка, ретрай уместен и нужен.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from decimal import Decimal, InvalidOperation

import httpx
from fastapi import APIRouter, BackgroundTasks, Depends, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.session import get_session
from app.models import Payment
from app.services.finik import verify_webhook
from app.services.subscription import apply_paid_payment

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/payments", tags=["payments"])


def _ok(description: str) -> Response:
    return JSONResponse({"status": "ok", "description": description})


@router.post("/finik")
async def finik_webhook(
    request: Request,
    background: BackgroundTasks,
    session: Session = Depends(get_session),
) -> Response:
    """Webhook Finik: подтверждает оплату и продлевает подписку.

    Идемпотентен по `order_id`: подпись Finik валидна бессрочно (ни nonce, ни
    срока в теле нет), поэтому «уже оплачено → просто ОК» — часть безопасности,
    а не оптимизация.
    """
    raw = await request.body()
    try:
        payload = json.loads(raw)
    except ValueError:
        logger.warning("finik webhook: body is not JSON (%s bytes)", len(raw))
        return JSONResponse({"error": "bad json"}, status_code=400)
    if not isinstance(payload, dict):
        return JSONResponse({"error": "bad json"}, status_code=400)

    signature_ok = verify_webhook(
        method=request.method,
        path=request.url.path,
        headers=dict(request.headers),
        body=payload,
        signature=request.headers.get("signature", ""),
        query=dict(request.query_params),
    )
    if not signature_ok:
        logger.warning("finik webhook: bad signature (payload=%s)", payload)
        return JSONResponse({"error": "invalid signature"}, status_code=401)

    fields = payload.get("fields") or {}
    order_id = str(fields.get("paymentId") or "")
    if not order_id:
        logger.error("finik webhook: no fields.paymentId in payload %s", payload)
        return JSONResponse({"error": "no paymentId"}, status_code=400)

    payment = session.scalar(select(Payment).where(Payment.order_id == order_id))
    if payment is None:
        logger.warning("finik webhook: unknown order %s", order_id)
        return JSONResponse({"error": "unknown order"}, status_code=404)

    if payment.status == "paid":  # идемпотентность: Finik ретраит доставку
        return _ok("already processed")

    if str(payload.get("status", "")).lower() != "success":
        # По докам webhook приходит только на успех; всё прочее — не наш случай.
        logger.warning(
            "finik webhook: non-success status %r for order %s", payload.get("status"), order_id
        )
        return _ok("ignored non-success status")

    # Защита: сумма из подписанного webhook должна совпасть с суммой платежа.
    try:
        callback_amount = Decimal(str(payload.get("amount")))
    except (InvalidOperation, ValueError):
        callback_amount = None
    if callback_amount is None or callback_amount != payment.amount:
        logger.error(
            "finik webhook: amount mismatch order=%s expected=%s got=%s",
            order_id,
            payment.amount,
            payload.get("amount"),
        )
        return JSONResponse({"error": "amount mismatch"}, status_code=400)

    transaction_id = payload.get("transactionId")
    user = apply_paid_payment(
        session,
        payment,
        provider_txn_id=str(transaction_id) if transaction_id is not None else None,
        raw_callback=payload,
    )
    telegram_id = user.telegram_id
    expires = user.subscription_expires_at
    session.commit()

    # Ответ Finik не должен ждать Telegram: залипший Bot API растянул бы webhook.
    background.add_task(_notify_paid, telegram_id, expires)
    return _ok("subscription activated")


async def _notify_paid(telegram_id: int, expires_at: datetime | None) -> None:
    """Сообщает пользователю об активации подписки прямым вызовом Bot API.

    Отдельный процесс от бота; sendMessage по HTTP не конфликтует с long-polling
    (конфликт даёт лишь второй getUpdates). Best-effort — сбой не валит webhook.
    """
    settings = get_settings()
    token = settings.telegram_bot_token
    if not token:
        return
    when = expires_at.strftime("%d.%m.%Y") if expires_at else "—"
    text = f"✅ Оплата получена. Подписка активна до {when}.\nДоступ открыт — /add_filter"
    payload: dict[str, object] = {"chat_id": telegram_id, "text": text}
    # Этот путь минует aiogram (и его session-middleware) — защиту ставим сами.
    if settings.protect_content_for(telegram_id):
        payload["protect_content"] = True
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            await client.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json=payload,
            )
    except httpx.HTTPError:
        logger.exception("failed to notify user %s about paid subscription", telegram_id)


_RETURN_PAGE = """<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title></head><body style="font-family:system-ui;text-align:center;padding:3rem 1rem">
<h2>{heading}</h2><p>{body}</p><p>Вернитесь в Telegram-бот.</p></body></html>"""


@router.get("/success", response_class=HTMLResponse)
def payment_success() -> HTMLResponse:
    """Фолбэк-страница возврата (нужна, только если BOT_USERNAME не задан).

    Её появление НЕ означает, что оплата подтверждена: Finik возвращает юзера
    сразу, а подтверждает платёж отдельным webhook.
    """
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
