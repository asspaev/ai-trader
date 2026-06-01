"""Тестовые утилиты для модулей ``app.services.llm``.

Содержит:

* :func:`load_fixture` — чтение JSON-снимков из ``tests/fixtures``.
* :func:`build_openrouter_settings` — конструктор настроек без задержек.
* :class:`FakeOpenRouterClient` — простой стенд для тестов агентов
  (Фаза 6+): мимикрирует публичный API :class:`OpenRouterClient`, но
  отдаёт заранее уложенные в очередь ответы без HTTP. Сейчас используется
  как smoke-проверка интерфейса; в фазе агентов будет подмешиваться
  через DI.
"""

from __future__ import annotations

import json
import uuid
from collections import deque
from pathlib import Path
from typing import Any

from app.config import OpenRouterSettings


_FIXTURES_DIR = Path(__file__).resolve().parents[3] / "fixtures"


def load_fixture(name: str) -> dict[str, Any]:
    """Прочитать JSON-фикстуру из ``tests/fixtures/<name>``."""
    path = _FIXTURES_DIR / name
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def build_openrouter_settings(
    *,
    base_url: str = "https://openrouter.test",
    api_key: str = "test-key",
    timeout_seconds: float = 1.0,
    max_retries: int = 3,
    retry_backoff_base: float = 0.0,
) -> OpenRouterSettings:
    """Сконструировать настройки без реальных задержек ретрая.

    ``retry_backoff_base=0`` гарантирует мгновенный ретрай в тестах
    (``0 ** k`` всё равно ноль или единица в нулевой степени, но мы
    нигде не сравниваем «1s/3s/9s» — здесь важна только семантика).
    """
    return OpenRouterSettings(
        api_key=api_key,
        base_url=base_url,
        timeout_seconds=timeout_seconds,
        max_retries=max_retries,
        retry_backoff_base=retry_backoff_base,
    )


class FakeOpenRouterClient:
    """In-memory заглушка вместо :class:`OpenRouterClient`.

    Отдаёт ответы из очереди ``responses`` в порядке поступления вызовов.
    Используется тестами агентов в Фазе 6 как drop-in замена реального
    клиента. Совместима по сигнатурам ``chat_completion`` / ``embeddings``
    с production-классом.
    """

    def __init__(
        self,
        *,
        chat_responses: list[dict[str, Any]] | None = None,
        embedding_responses: list[dict[str, Any]] | None = None,
    ) -> None:
        self._chat_queue: deque[dict[str, Any]] = deque(chat_responses or [])
        self._embedding_queue: deque[dict[str, Any]] = deque(embedding_responses or [])
        self.chat_calls: list[dict[str, Any]] = []
        self.embedding_calls: list[dict[str, Any]] = []

    async def __aenter__(self) -> "FakeOpenRouterClient":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None

    async def aclose(self) -> None:
        return None

    async def chat_completion(
        self,
        *,
        agent_name: str,
        model: str,
        messages: list[dict[str, Any]],
        pipeline_run_id: uuid.UUID | None = None,
        **extra: Any,
    ) -> dict[str, Any]:
        self.chat_calls.append(
            {
                "agent_name": agent_name,
                "model": model,
                "messages": messages,
                "pipeline_run_id": pipeline_run_id,
                "extra": extra,
            }
        )
        if not self._chat_queue:
            raise AssertionError("FakeOpenRouterClient: chat response queue is empty")
        return self._chat_queue.popleft()

    async def embeddings(
        self,
        *,
        agent_name: str,
        model: str,
        inputs: str | list[str],
        pipeline_run_id: uuid.UUID | None = None,
    ) -> dict[str, Any]:
        self.embedding_calls.append(
            {
                "agent_name": agent_name,
                "model": model,
                "inputs": inputs,
                "pipeline_run_id": pipeline_run_id,
            }
        )
        if not self._embedding_queue:
            raise AssertionError(
                "FakeOpenRouterClient: embedding response queue is empty"
            )
        return self._embedding_queue.popleft()


__all__ = [
    "FakeOpenRouterClient",
    "build_openrouter_settings",
    "load_fixture",
]
