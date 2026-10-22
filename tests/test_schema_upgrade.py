"""Апгрейд схемы при выкатке: DDL только когда он реально нужен.

`ALTER TABLE … ADD COLUMN IF NOT EXISTS` берёт ACCESS EXCLUSIVE ДО проверки
наличия колонки, и при чужой долгой транзакции на `listings` выкатка вставала
колом вместе со всеми запросами к таблице (21.10.2026). Поэтому наличие
проверяем по каталогу, а DDL идёт с lock_timeout и повтором.
"""

from types import SimpleNamespace

import pytest
from sqlalchemy.exc import OperationalError

import app.db.session as dbsession


class FakeConnection:
    def __init__(self, exists: bool) -> None:
        self.exists = exists
        self.executed: list[str] = []

    def execute(self, statement, params=None):
        sql = str(statement)
        self.executed.append(sql)
        found = self.exists and ("information_schema" in sql or "pg_indexes" in sql)
        return SimpleNamespace(first=lambda: (1,) if found else None)


class FakeEngine:
    def __init__(self, connections: list) -> None:
        self.connections = connections

    def begin(self):
        connection = self.connections.pop(0)
        engine = self

        class _Ctx:
            def __enter__(self):
                return connection

            def __exit__(self, *exc):
                return False

        engine.last = connection
        return _Ctx()


@pytest.mark.parametrize(
    "statement",
    [
        "ALTER TABLE listings ADD COLUMN IF NOT EXISTS source_make_text VARCHAR(100)",
        "ALTER TABLE user_filters ALTER COLUMN brand_id DROP NOT NULL",
        "CREATE UNIQUE INDEX IF NOT EXISTS ix_bot_users_invite_code ON bot_users (invite_code)",
    ],
)
def test_existing_object_skips_ddl_entirely(monkeypatch, statement) -> None:
    connection = FakeConnection(exists=True)
    monkeypatch.setattr(dbsession, "engine", FakeEngine([connection]))
    dbsession._apply(statement)
    assert not any(sql.startswith(("ALTER", "CREATE", "SET")) for sql in connection.executed)


def test_missing_column_is_added_with_lock_timeout(monkeypatch) -> None:
    connection = FakeConnection(exists=False)
    monkeypatch.setattr(dbsession, "engine", FakeEngine([connection]))
    statement = "ALTER TABLE user_filters ADD COLUMN IF NOT EXISTS brand_group VARCHAR(16)"
    dbsession._apply(statement)
    assert connection.executed[-2].startswith("SET LOCAL lock_timeout")
    assert connection.executed[-1] == statement


def test_lock_timeout_is_retried(monkeypatch) -> None:
    class Locked(FakeConnection):
        def execute(self, statement, params=None):
            if str(statement).startswith("ALTER"):
                raise OperationalError("ALTER", {}, SimpleNamespace(sqlstate="55P03"))
            return super().execute(statement, params)

    good = FakeConnection(exists=False)
    monkeypatch.setattr(dbsession, "engine", FakeEngine([Locked(exists=False), good]))
    monkeypatch.setattr(dbsession, "DDL_RETRY_PAUSE_SECONDS", 0)
    statement = "ALTER TABLE listings ADD COLUMN IF NOT EXISTS x INTEGER"
    dbsession._apply(statement)
    assert good.executed[-1] == statement
