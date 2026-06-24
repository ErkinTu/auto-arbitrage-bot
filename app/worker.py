from __future__ import annotations

import logging
import time

from app.db.session import SessionLocal, init_db
from app.parsers.mashina import MashinaParser
from app.services.lookup import get_or_create_source
from app.services.sync import iter_due_targets, run_sync
from app.core.config import get_settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def run_once() -> int:
    settings = get_settings()
    init_db()
    with SessionLocal() as session:
        source = get_or_create_source(session, "mashina.kg", settings.mashina_api_url)
        session.commit()
        parser = MashinaParser()
        completed = 0
        try:
            for target in iter_due_targets(session):
                summary = run_sync(session, parser, source, target, mode="fresh")
                logger.info("Synced %s: %s", target.brand_filter, summary)
                completed += 1
        finally:
            parser.close()
        return completed


def main() -> None:
    while True:
        try:
            completed = run_once()
            logger.info("Worker cycle complete. Targets run: %s", completed)
        except Exception:
            logger.exception("Worker cycle failed")
        time.sleep(60)


if __name__ == "__main__":
    main()
