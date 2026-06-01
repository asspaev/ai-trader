"""Pipeline-слой: оркестрация PRICE/NEWS/TRADER в рамках одного тика.

Модули:

* :mod:`app.services.pipeline.crypto_step` — обработка одной монеты:
  параллельные ветки PRICE и NEWS внутри ``asyncio.gather``, дальше
  TRADER + исполнение mock-сделки в одной БД-транзакции, таймауты и
  стандартизированные коды причин неисполнения.
* :mod:`app.services.pipeline.runner` — один полный тик: создаёт
  ``pipeline_run_id`` и последовательно обходит монеты, собирая
  агрегированный :class:`PipelineRunResult`.

Логика scheduler (cron/interval) находится в отдельном модуле,
который будет реализован в фазе 8; pipeline-слой остаётся
тестируемым в изоляции — runner просто вызывается как корутина.
"""

from app.services.pipeline.crypto_step import (
    CryptoStepResult,
    PipelineContext,
    PipelineStepFailureReason,
)
from app.services.pipeline.runner import PipelineRunResult, run_pipeline_once


# ВАЖНО: функцию ``crypto_step`` на уровне пакета НЕ ре-экспортируем.
# Её имя совпадает с именем подмодуля (``crypto_step.py``), и при
# re-export атрибут пакета ``app.services.pipeline.crypto_step``
# перезаписывается функцией — после этого ``import
# app.services.pipeline.crypto_step as cs`` начинает возвращать
# функцию вместо модуля, что ломает ``monkeypatch.setattr`` по строке.
# Импортируйте функцию явно: ``from app.services.pipeline.crypto_step
# import crypto_step``.


__all__ = [
    "CryptoStepResult",
    "PipelineContext",
    "PipelineRunResult",
    "PipelineStepFailureReason",
    "run_pipeline_once",
]
