"""ORM-модели приложения.

Импорт всех моделей в этом модуле гарантирует, что они зарегистрированы
в ``Base.metadata`` к моменту, когда Alembic строит граф миграций.
"""

from app.models.base import Base
from app.models.decision import Decision
from app.models.enums import DecisionAction, LLMCallStatus, TransactionAction
from app.models.llm_call import LLMCall
from app.models.news import News
from app.models.transaction import Transaction
from app.models.user import User
from app.models.wallet import Wallet

__all__ = [
    "Base",
    "Decision",
    "DecisionAction",
    "LLMCall",
    "LLMCallStatus",
    "News",
    "Transaction",
    "TransactionAction",
    "User",
    "Wallet",
]
