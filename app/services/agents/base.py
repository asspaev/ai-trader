"""Базовая инфраструктура LLM-агентов.

Содержит:

* :data:`PROMPTS_DIR` — корень с ``.md``-промптами рядом с этим
  модулем. Они версионируются вместе с кодом, см. `architecture.md` §8.5.
* :func:`load_prompt` / :func:`render_prompt` — кэш и шаблонизация
  через :class:`string.Template` (выбор зафиксирован в `architecture.md` §17.17).
* :func:`extract_assistant_content` — достаёт первое сообщение
  ``choices[0].message.content`` из ответа OpenRouter в текст.
* :func:`parse_strict_json` — строгий разбор JSON с попыткой
  «выкусить» ```json …``` блок, если LLM всё-таки обернул ответ в
  markdown.
* :class:`AgentError`, :class:`AgentJSONParseError` — корневые
  ошибки слоя; агенты бросают их в pipeline.

Каждый агент строится как тонкий слой: рендер → вызов LLM → парсинг.
Никаких прямых обращений к БД (это задача :mod:`app.crud`).
"""

from __future__ import annotations

import enum
import json
import re
from functools import lru_cache
from pathlib import Path
from string import Template
from typing import Any


PROMPTS_DIR: Path = Path(__file__).resolve().parent / "prompts"


class AgentError(RuntimeError):
    """Базовое исключение слоя агентов.

    Используется как корневой класс, чтобы pipeline мог ловить любые
    проблемы агентов одним ``except AgentError``.
    """


class AgentJSONParseError(AgentError):
    """LLM вернул не-JSON или JSON неожиданной формы.

    Содержит сырой ``content`` для записи в логи и (опционально) в
    повторный prompt при ретрае.
    """

    # ``raw_content`` пробрасываем в ``args`` (а не только в атрибут),
    # чтобы исключение корректно pickle/unpickle-илось — иначе loguru
    # с ``enqueue=True`` падает при передаче записи в фоновую очередь
    # (default __reduce__ для BaseException зовёт ``cls(*self.args)``).
    def __init__(self, message: str, raw_content: str) -> None:
        super().__init__(message, raw_content)
        self.raw_content = raw_content


class Sentiment(str, enum.Enum):
    """Единый алфавит «тональности», общий для PRICE и NEWS.

    Значения нижним регистром — соответствуют тому, что мы просим в
    промптах. Используем :class:`str.Enum`, чтобы значения сериализовались
    в JSON «как есть» без дополнительной обработки.
    """

    BULLISH = "bullish"
    BEARISH = "bearish"
    NEUTRAL = "neutral"

    @classmethod
    def parse(cls, value: Any) -> "Sentiment":
        """Толерантный парсинг: case-insensitive, обрезка пробелов.

        Бросает :class:`ValueError`, если значение не из словаря —
        вызывающая сторона завернёт это в :class:`AgentJSONParseError`.
        """
        if isinstance(value, cls):
            return value
        if not isinstance(value, str):
            raise ValueError(f"Sentiment must be a string, got {type(value).__name__}")
        normalized = value.strip().lower()
        for member in cls:
            if member.value == normalized:
                return member
        raise ValueError(f"Unknown sentiment value: {value!r}")


@lru_cache(maxsize=None)
def load_prompt(name: str) -> str:
    """Прочитать шаблон промпта по имени файла (без расширения).

    Файлы лежат в :data:`PROMPTS_DIR` (``app/services/agents/prompts``).
    Результат кэшируется, потому что промпты статичны и не меняются
    в рантайме (изменение требует деплоя).
    """
    path = PROMPTS_DIR / f"{name}.md"
    if not path.exists():
        raise FileNotFoundError(f"Prompt template not found: {path}")
    return path.read_text(encoding="utf-8")


def render_prompt(name: str, **values: Any) -> str:
    """Подставить ``values`` в шаблон промпта ``name``.

    Использует :class:`string.Template` (``$var`` / ``${var}``).
    ``substitute`` бросает ``KeyError`` при пропущенной переменной —
    это спецом строже, чем ``safe_substitute``: лучше упасть сразу,
    чем отправить в LLM полу-заполненный prompt.
    """
    template = Template(load_prompt(name))
    return template.substitute(**values)


def extract_assistant_content(response: dict[str, Any]) -> str:
    """Достать текстовый контент первого ``choices[0].message`` из ответа.

    Сигнатура ответа OpenAI-совместима с OpenRouter::

        {
          "choices": [
            {"index": 0, "message": {"role": "assistant", "content": "..."}}
          ],
          "usage": {...}
        }

    Бросает :class:`AgentError`, если структура неожиданная.
    """
    if not isinstance(response, dict):
        raise AgentError(f"LLM response is not an object: {type(response).__name__}")

    choices = response.get("choices")
    if not isinstance(choices, list) or not choices:
        raise AgentError("LLM response has no choices")

    first = choices[0]
    if not isinstance(first, dict):
        raise AgentError("LLM response choices[0] is not an object")

    message = first.get("message")
    if not isinstance(message, dict):
        raise AgentError("LLM response choices[0].message is missing")

    content = message.get("content")
    if not isinstance(content, str):
        raise AgentError("LLM response choices[0].message.content is not a string")

    return content


_JSON_FENCE_RE = re.compile(
    r"```(?:json)?\s*(.+?)\s*```",
    re.DOTALL | re.IGNORECASE,
)


def parse_strict_json(content: str) -> dict[str, Any]:
    """Распарсить ``content`` как JSON-объект.

    Поведение:

    * Если LLM завернул ответ в ```json …``` блок, выкусываем тело
      блока перед парсингом.
    * Иначе ищем первую ``{`` и пытаемся прочитать **один** JSON-объект
      через :meth:`json.JSONDecoder.raw_decode` — это терпимо к
      «хвосту» из текста после JSON (часто DeepSeek дописывает прозу
      после ответа, что давало ``JSONDecodeError: Extra data``).

    Массив на верхнем уровне или мусор без ``{`` — ошибка агента,
    бросаем :class:`AgentJSONParseError` с исходным ``content``.
    """
    raw = content.strip()
    fence_match = _JSON_FENCE_RE.search(raw)
    candidate = fence_match.group(1).strip() if fence_match else raw

    start = candidate.find("{")
    if start == -1:
        raise AgentJSONParseError(
            "LLM response contains no JSON object",
            raw_content=content,
        )

    try:
        parsed, _end = json.JSONDecoder().raw_decode(candidate, start)
    except json.JSONDecodeError as exc:
        raise AgentJSONParseError(
            f"LLM response is not valid JSON: {exc.msg}",
            raw_content=content,
        ) from exc

    if not isinstance(parsed, dict):
        raise AgentJSONParseError(
            f"LLM response JSON must be an object, got {type(parsed).__name__}",
            raw_content=content,
        )
    return parsed


__all__ = [
    "AgentError",
    "AgentJSONParseError",
    "PROMPTS_DIR",
    "Sentiment",
    "extract_assistant_content",
    "load_prompt",
    "parse_strict_json",
    "render_prompt",
]
