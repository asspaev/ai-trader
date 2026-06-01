"""Точка входа приложения.

В фазе 0 — только настраивает логирование и блокируется на
``asyncio.Event().wait()``, чтобы контейнер оставался живым. В
последующих фазах сюда добавятся запуск планировщика
(:mod:`app.services.pipeline.scheduler`) и Telegram-бота
(:mod:`app.services.telegram.bot`) через ``asyncio.gather``.
"""

from __future__ import annotations

import asyncio

from app.core.db import dispose_engine
from app.core.logger import configure_logging, logger


async def _run() -> None:
    configure_logging()
    logger.info("ai-trader skeleton started; scheduler and bot will be wired in later phases")
    try:
        await asyncio.Event().wait()
    finally:
        await dispose_engine()
        logger.info("ai-trader skeleton stopped")


def main() -> None:
    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        logger.info("Interrupted by user")


if __name__ == "__main__":
    main()
