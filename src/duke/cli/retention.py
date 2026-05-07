"""Retention CLI: `python -m duke.cli.retention purge`.

Intended to be invoked from a host cron / Kubernetes CronJob. Picks up
configuration from the same env vars as the main service.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

import structlog
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from duke.config import get_settings
from duke.integration.store.retention import purge_old_turn_text
from duke.observability.logging import configure_logging


async def _purge() -> int:
    settings = get_settings()
    engine = create_async_engine(settings.duke_db_dsn)
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        return await purge_old_turn_text(
            sessionmaker, retention_days=settings.retention_days_turn_text
        )
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="duke.cli.retention")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("purge", help="Anonymize conversation_turn.text past the retention window.")
    args = parser.parse_args(argv)

    settings = get_settings()
    configure_logging(settings.log_level, settings.log_verbose_payloads)
    log = structlog.get_logger("duke.cli.retention")

    if args.cmd == "purge":
        purged = asyncio.run(_purge())
        log.info("retention.cli_done", purged=purged)
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
