from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.db.base import Base
from app.models import ParserRun, Source
from app.worker import fail_orphaned_full_runs


def _session() -> Session:
    import app.models  # noqa: F401

    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = Session(engine)
    session.add(Source(name="mashina.kg"))
    session.commit()
    return session


def test_orphaned_full_runs_marked_failed_fresh_untouched() -> None:
    session = _session()
    source_id = session.scalar(select(Source.id))
    session.add_all(
        [
            ParserRun(source_id=source_id, mode="full", status="running"),
            ParserRun(source_id=source_id, mode="full", status="completed"),
            # fresh принадлежит боту и может идти прямо сейчас — не трогаем.
            ParserRun(source_id=source_id, mode="fresh", status="running"),
        ]
    )
    session.commit()

    assert fail_orphaned_full_runs(session) == 1
    session.commit()

    runs = {(r.mode, r.status) for r in session.scalars(select(ParserRun))}
    assert runs == {("full", "failed"), ("full", "completed"), ("fresh", "running")}
    failed = session.scalar(select(ParserRun).where(ParserRun.status == "failed"))
    assert failed.finished_at is not None
    assert "orphaned" in failed.error_message
