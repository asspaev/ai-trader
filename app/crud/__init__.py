"""CRUD-слой: единственное место в проекте, где разрешены SQL/ORM-запросы.

Сервисы и агенты обращаются к БД исключительно через функции этих
модулей. Это гарантирует, что атомарность сделок и инварианты
поддерживаются единообразно.
"""

from app.crud import (
    decision,
    llm_call,
    news,
    scheduler_state,
    transaction,
    user,
    wallet,
)

__all__ = [
    "decision",
    "llm_call",
    "news",
    "scheduler_state",
    "transaction",
    "user",
    "wallet",
]
