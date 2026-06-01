"""Утилиты для тестов агентов: фабрики LLM-ответов и фикстур данных."""

from __future__ import annotations

import json
from typing import Any


def chat_response(content: str | dict[str, Any]) -> dict[str, Any]:
    """Сконструировать минимальный chat.completion-ответ OpenRouter.

    ``content`` может быть строкой (как есть) или dict — он сериализуется
    в JSON-строку. Так удобнее писать тесты «вот такой объект пришёл от
    LLM» без ручного ``json.dumps``.
    """
    if isinstance(content, dict):
        body = json.dumps(content, ensure_ascii=False)
    else:
        body = content
    return {
        "id": "test-id",
        "object": "chat.completion",
        "model": "deepseek/deepseek-chat",
        "choices": [
            {
                "index": 0,
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": body},
            }
        ],
        "usage": {
            "prompt_tokens": 1,
            "completion_tokens": 1,
            "total_tokens": 2,
        },
    }


__all__ = ["chat_response"]
