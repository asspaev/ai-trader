"""Core-инфраструктура: движок БД, фабрика сессий, логирование.

Здесь живут «фундаментальные» сервисы, не имеющие домена и нужные
почти всем модулям приложения (``app.crud``, ``app.services.*``,
``app.main``).
"""

from app.core.db import SessionLocal, dispose_engine, engine, get_session
from app.core.logger import configure_logging, logger

__all__ = [
    "SessionLocal",
    "configure_logging",
    "dispose_engine",
    "engine",
    "get_session",
    "logger",
]
