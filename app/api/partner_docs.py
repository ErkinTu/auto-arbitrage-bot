"""Swagger для внешнего разработчика (мобильное приложение) — под логином.

`GET /api/v1/docs` — Swagger UI, `GET /api/v1/docs/openapi.json` — схема.
Вход — HTTP Basic, пары логин:пароль в `PARTNER_DOCS_USERS`. Пусто — ручек
как будто нет (404): по умолчанию выключено.

В схему попадает ровно то, что Caddy открывает наружу на домене сайта
(лента, карточка, справочник, оценка, аналитика подписчика, вход, фильтры
уведомлений). Админка, платежи, служебное — не попадают, и их схемы данных
(модели пользователей и т. п.) вычищаются тоже: по документу нельзя даже
узнать, что они есть. Это не замена защите — `admin/*` и так закрыт гейтом
и не проксируется на домен сайта; схема лишь не показывает лишнего.

«Try it out» ходит в тот же домен — к боевым данным, с теми же лимитами и
ролями, что у сайта: закрытое без входа/подписки ответит 401/403.
"""

from __future__ import annotations

import copy
import hmac
import json
import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from app.core.config import get_settings

logger = logging.getLogger("app.partner_docs")

router = APIRouter(prefix="/api/v1/docs", include_in_schema=False)
_basic = HTTPBasic(auto_error=False, realm="API docs")

SWAGGER_CDN = "https://cdn.jsdelivr.net/npm/swagger-ui-dist@5.17.14"

# Тот же белый список, что `@public_api` / `@auth_api` в Caddyfile.
# Поменяли там — поменяйте здесь (тест сверяет, что admin/payments не протекли).
_READ_ONLY = (
    "/api/v1/public/",
    "/api/v1/catalog/",
    "/api/v1/dashboard/",
    "/api/v1/market/",
    "/api/v1/estimate",
    "/api/v1/price-trend",
)
_ANY_METHOD = ("/api/v1/auth/", "/api/v1/me/")
_EXACT = {"/health": {"get"}}

_HEADERS = {
    "Cache-Control": "no-store",
    "X-Robots-Tag": "noindex, nofollow",
    # Свой CSP вместо «default-src 'none'» общего middleware: Swagger UI — с CDN.
    "Content-Security-Policy": (
        f"default-src 'none'; script-src 'self' {SWAGGER_CDN}/; "
        f"style-src 'unsafe-inline' {SWAGGER_CDN}/; img-src 'self' data: https:; "
        "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
    ),
}


def _users() -> dict[str, str]:
    raw = get_settings().partner_docs_users
    pairs = (item.strip().split(":", 1) for item in raw.split(",") if ":" in item)
    return {
        login.strip(): password.strip()
        for login, password in pairs
        if login.strip() and password.strip()
    }


def require_partner(
    request: Request, credentials: Annotated[HTTPBasicCredentials | None, Depends(_basic)]
) -> str:
    users = _users()
    if not users:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not Found")
    if credentials is not None:
        # compare_digest по всем парам — время ответа не выдаёт, какой логин есть.
        ok = None
        for login, password in users.items():
            match = hmac.compare_digest(
                credentials.username.encode(), login.encode()
            ) & hmac.compare_digest(credentials.password.encode(), password.encode())
            if match:
                ok = login
        if ok:
            return ok
        logger.warning("partner docs: неверный вход login=%r", credentials.username[:64])
    raise HTTPException(
        status.HTTP_401_UNAUTHORIZED,
        "Нужен логин и пароль",
        headers={"WWW-Authenticate": 'Basic realm="API docs", charset="UTF-8"'},
    )


def _allowed(path: str, method: str) -> bool:
    if path in _EXACT:
        return method in _EXACT[path]
    if path.startswith(_ANY_METHOD):
        return True
    return method in {"get", "head"} and path.startswith(_READ_ONLY)


def _collect_refs(node, found: set[str], schemas: dict) -> None:
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/components/schemas/"):
            name = ref.rsplit("/", 1)[-1]
            if name not in found:
                found.add(name)
                _collect_refs(schemas.get(name), found, schemas)
        for value in node.values():
            _collect_refs(value, found, schemas)
    elif isinstance(node, list):
        for value in node:
            _collect_refs(value, found, schemas)


def partner_schema(full: dict) -> dict:
    """Схема только с открытыми наружу ручками и их моделями."""
    paths: dict = {}
    for path, item in full.get("paths", {}).items():
        ops = {m: copy.deepcopy(op) for m, op in item.items() if _allowed(path, m)}
        if ops:
            paths[path] = ops
    schemas = full.get("components", {}).get("schemas", {})
    used: set[str] = set()
    _collect_refs(paths, used, schemas)
    spec = {
        "openapi": full.get("openapi", "3.1.0"),
        "info": {
            "title": "Car Market API",
            "version": full.get("info", {}).get("version", "1"),
            "description": (
                "Боевые данные: лента, карточка, справочник, оценка рынка; вход — через "
                "Telegram-бота (`/login`), сессия `sk_session`. Без входа открыто всё, "
                "кроме истории цены, аналитики (`401`/`403`) и фильтров уведомлений. "
                "Лимит частоты — на IP, при `429` ждать `Retry-After`. "
                "Подробно — комплект для разработчика (README, docs/, mock/)."
            ),
        },
        "paths": paths,
        "components": {
            "schemas": {name: schemas[name] for name in sorted(used) if name in schemas},
            "securitySchemes": {
                "session": {
                    "type": "apiKey",
                    "in": "cookie",
                    "name": "sk_session",
                    "description": "Сессия после POST /api/v1/auth/exchange. Нативный клиент шлёт `Cookie: sk_session=<значение>`.",
                }
            },
        },
    }
    return _rename_models(spec)


def _rename_models(spec: dict) -> dict:
    """Модели, общие с админкой (`AdminLoginRequest` у входа сайта), — без «Admin»
    в имени: внешнему разработчику незачем знать, что она есть."""
    schemas = spec["components"]["schemas"]
    renames = {
        name: name.replace("Admin", "", 1)
        for name in schemas
        if "Admin" in name and name.replace("Admin", "", 1) not in schemas
    }
    if not renames:
        return spec
    text = json.dumps(spec, ensure_ascii=False)
    for old, new in renames.items():
        text = text.replace(f'"#/components/schemas/{old}"', f'"#/components/schemas/{new}"')
    spec = json.loads(text)
    schemas = spec["components"]["schemas"]
    for old, new in renames.items():
        model = schemas.pop(old)
        if isinstance(model, dict) and model.get("title") == old:
            model["title"] = new
        schemas[new] = model
    spec["components"]["schemas"] = dict(sorted(schemas.items()))
    return spec


_cached: dict | None = None


@router.get("/openapi.json")
def docs_schema(request: Request, login: Annotated[str, Depends(require_partner)]) -> Response:
    global _cached
    if _cached is None:
        _cached = partner_schema(request.app.openapi())
    return JSONResponse(_cached, headers=_HEADERS)


@router.get("/init.js")
def docs_init(login: Annotated[str, Depends(require_partner)]) -> Response:
    # Отдельным файлом, а не inline: CSP без 'unsafe-inline' для скриптов.
    options = {
        "url": "/api/v1/docs/openapi.json",
        "dom_id": "#swagger-ui",
        "deepLinking": True,
        "filter": True,
        "docExpansion": "list",
        "defaultModelsExpandDepth": 0,
        "displayRequestDuration": True,
        "withCredentials": True,
    }
    body = f"window.ui = SwaggerUIBundle({json.dumps(options)});\n"
    return Response(body, media_type="application/javascript", headers=_HEADERS)


@router.get("", response_class=HTMLResponse)
def docs_page(request: Request, login: Annotated[str, Depends(require_partner)]) -> Response:
    logger.info("partner docs: открыт login=%r", login)
    html = f"""<!doctype html>
<html lang="ru"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>Car Market API</title>
<link rel="stylesheet" href="{SWAGGER_CDN}/swagger-ui.css">
<style>body{{margin:0;background:#fff}} .swagger-ui .topbar{{display:none}}</style>
</head><body>
<div id="swagger-ui"></div>
<script src="{SWAGGER_CDN}/swagger-ui-bundle.js"></script>
<script src="/api/v1/docs/init.js"></script>
</body></html>
"""
    return HTMLResponse(html, headers=_HEADERS)
