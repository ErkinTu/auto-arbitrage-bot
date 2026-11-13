"""Swagger для внешнего разработчика: логин, выключатель, ничего админского в схеме."""

from __future__ import annotations

import base64
import json

import pytest
from fastapi.testclient import TestClient

from app.api import partner_docs
from app.core.config import get_settings
from app.main import app


def _auth(login: str, password: str) -> dict[str, str]:
    token = base64.b64encode(f"{login}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(get_settings(), "partner_docs_users", "mobile:s3cret-pass, other:x2")
    monkeypatch.setattr(partner_docs, "_cached", None)
    return TestClient(app)


def test_disabled_by_default_is_404(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "partner_docs_users", "")
    c = TestClient(app)
    assert c.get("/api/v1/docs").status_code == 404
    assert (
        c.get("/api/v1/docs/openapi.json", headers=_auth("mobile", "s3cret-pass")).status_code
        == 404
    )


@pytest.mark.parametrize("headers", [{}, _auth("mobile", "wrong"), _auth("nobody", "s3cret-pass")])
def test_requires_valid_login(client: TestClient, headers: dict) -> None:
    for path in ("/api/v1/docs", "/api/v1/docs/openapi.json", "/api/v1/docs/init.js"):
        r = client.get(path, headers=headers)
        assert r.status_code == 401, path
        assert r.headers["www-authenticate"].startswith("Basic")


def test_page_and_headers(client: TestClient) -> None:
    r = client.get("/api/v1/docs", headers=_auth("other", "x2"))
    assert r.status_code == 200
    assert "swagger-ui-bundle.js" in r.text and "/api/v1/docs/init.js" in r.text
    assert r.headers["cache-control"] == "no-store"
    assert "script-src 'self' https://cdn.jsdelivr.net" in r.headers["content-security-policy"]
    assert "noindex" in r.headers["x-robots-tag"]


def test_schema_has_only_public_routes(client: TestClient) -> None:
    r = client.get("/api/v1/docs/openapi.json", headers=_auth("mobile", "s3cret-pass"))
    assert r.status_code == 200
    spec = r.json()
    paths = spec["paths"]
    assert "/api/v1/public/feed" in paths
    assert "/api/v1/auth/exchange" in paths
    assert "/api/v1/me/filters" in paths
    for path, ops in paths.items():
        assert not path.startswith(("/api/v1/admin", "/api/v1/payments", "/api/v1/docs")), path
        if not path.startswith(("/api/v1/auth/", "/api/v1/me/")):
            assert set(ops) <= {"get", "head"}, path
    # Единственное допустимое «admin» — значение роли в /auth/me: API его
    # возвращает, и клиент, сгенерированный без него, упал бы на входе админа.
    text = json.dumps(spec).lower().replace('"subscriber", "admin"]', '"subscriber"]')
    assert "admin" not in text and "finik" not in text and "superuser" not in text
    # Каждая ссылка на модель разрешается — лишнее вычищено, нужное на месте.
    for ref in {
        s.split('"')[0] for s in json.dumps(spec).split('"$ref": "#/components/schemas/')[1:]
    }:
        assert ref in spec["components"]["schemas"], ref
