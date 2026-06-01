"""Настройка loguru.

Один источник конфигурации логирования: stdout + ротируемый файл
``LOG_DIR/app.log``. Сообщения логов пишутся на английском, контекст
прикрепляется через ``logger.bind(...)`` (``pipeline_run_id``,
``asset``, ``llm_call_id`` и т.п.).
"""

from __future__ import annotations

import sys
from pathlib import Path

from loguru import logger

from app.config import settings

_LOG_FORMAT = (
    "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> "
    "<level>{level: <8}</level> "
    "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> "
    "{extra} - <level>{message}</level>"
)

_configured = False


def configure_logging() -> None:
    """Сбрасывает дефолтных хендлеров loguru и навешивает свои.

    Идемпотентна — повторные вызовы безопасны (всё равно поднимут флаг
    и пропустят повторную настройку).
    """
    global _configured
    if _configured:
        return

    logger.remove()
    logger.add(
        sys.stdout,
        level=settings.logging.level,
        format=_LOG_FORMAT,
        backtrace=False,
        diagnose=False,
        enqueue=False,
    )

    log_dir = Path(settings.logging.dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    logger.add(
        log_dir / "app.log",
        level=settings.logging.level,
        format=_LOG_FORMAT,
        rotation="10 MB",
        retention=7,
        compression="zip",
        enqueue=True,
        backtrace=False,
        diagnose=False,
    )

    _configured = True
    logger.info(
        "Logger configured: level={level}, dir={dir}",
        level=settings.logging.level,
        dir=str(log_dir),
    )


__all__ = ["configure_logging", "logger"]
