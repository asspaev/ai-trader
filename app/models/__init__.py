"""ORM-модели приложения.

В фазе 0 содержит только :class:`Base`. Конкретные модели
(``User``, ``Wallet``, ``Transaction``, ``Decision``, ``News``,
``LLMCall``) добавляются в фазе 1.
"""

from app.models.base import Base

__all__ = ["Base"]
