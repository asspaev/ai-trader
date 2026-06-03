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
import uuid
from functools import lru_cache
from pathlib import Path
from string import Template
from typing import Any, Awaitable, Callable, Mapping, Protocol, TypeVar

from loguru import logger


PROMPTS_DIR: Path = Path(__file__).resolve().parent / "prompts"

T = TypeVar("T")
MessagesFactory = Callable[["AgentJSONParseError | None"], list[dict]]
ContentParser = Callable[[str], T]


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


class ChatLLM(Protocol):
    """Минимальный публичный контракт LLM-клиента для агентов.

    Реализуется :class:`OpenRouterClient` и тестовым
    :class:`FakeOpenRouterClient`. Поднят в :mod:`base`, чтобы все
    агенты импортировали один и тот же тип, а не дублировали
    Protocol в каждом модуле.
    """

    async def chat_completion(
        self,
        *,
        agent_name: str,
        model: str,
        messages: list[dict],
        pipeline_run_id: uuid.UUID | None = ...,
        **extra: Any,
    ) -> dict:
        ...  # pragma: no cover — structural Protocol


class BaseAgent:
    """Общий предок LLM-агентов: владение моделью + локальный парсинг-ретрай.

    Каждый агент-наследник вызывает :meth:`_chat_with_parse_retry`,
    передавая фабрику сообщений и парсер ответа. Базовый класс делает
    до :attr:`MAX_PARSE_ATTEMPTS` попыток: при первом провале парсинга
    второй вызов идёт с тем же ``messages_factory``, но с переданным в
    него ``prior_error`` — агент сам решает, добавить ли в промпт
    reminder про предыдущую ошибку.

    Если все попытки провалились — поднимаем последний
    :class:`AgentJSONParseError`. Pipeline пометит шаг как
    ``executed=False`` + ``not_executed_reason="LLM_PARSE_FAILED"``.

    Attributes:
        MAX_PARSE_ATTEMPTS: Сколько раз пробуем распарсить ответ.
            Меняется через override в наследниках (но обычно 2 хватает).
        LOG_COMPONENT: Имя компонента для ``logger.bind(component=...)``.
            Наследник переопределяет, чтобы логи были различимы по агенту.
    """

    MAX_PARSE_ATTEMPTS: int = 2
    LOG_COMPONENT: str = "agent"

    def __init__(self, llm_client: ChatLLM, *, model: str) -> None:
        self._llm = llm_client
        self._model = model

    async def _chat_with_parse_retry(
        self,
        *,
        agent_name: str,
        messages_factory: MessagesFactory,
        parser: ContentParser[T],
        pipeline_run_id: uuid.UUID | None = None,
        log_extra: Mapping[str, Any] | None = None,
    ) -> T:
        """Выполнить LLM-вызов и распарсить ответ с ретраем при парс-ошибке.

        Args:
            agent_name: Имя для записи в ``llm_calls.agent_name``.
                У NewsAgent три разных значения (`news_summary`,
                `news_agenda`, `news_final_score`) — поэтому параметр
                на каждый вызов, а не атрибут класса.
            messages_factory: Фабрика, которая по ``prior_error``
                (``None`` на первой попытке) возвращает финальный список
                ``messages``. Reminder при ретрае собирает сам агент —
                базовый класс не знает специфики формата.
            parser: Функция, превращающая ``content`` ответа в результат
                агента. Должна бросать :class:`AgentJSONParseError` при
                невалидном/неполном JSON.
            pipeline_run_id: Прокидывается в LLM-клиент для трекинга.
            log_extra: Дополнительные поля для ``logger.bind`` (обычно
                ``{"asset": "BTC"}``).

        Returns:
            Результат ``parser(content)`` первой удачной попытки.
        """
        bound = logger.bind(
            component=self.LOG_COMPONENT,
            agent=agent_name,
            pipeline_run_id=str(pipeline_run_id) if pipeline_run_id else None,
            **(dict(log_extra) if log_extra else {}),
        )

        last_error: AgentJSONParseError | None = None
        for attempt in range(1, self.MAX_PARSE_ATTEMPTS + 1):
            response = await self._llm.chat_completion(
                agent_name=agent_name,
                model=self._model,
                messages=messages_factory(last_error),
                pipeline_run_id=pipeline_run_id,
            )
            content = extract_assistant_content(response)
            try:
                return parser(content)
            except AgentJSONParseError as exc:
                last_error = exc
                bound.warning(
                    "{agent} response failed parsing on attempt {attempt}/{total}: {msg}",
                    agent=agent_name,
                    attempt=attempt,
                    total=self.MAX_PARSE_ATTEMPTS,
                    msg=str(exc),
                )

        assert last_error is not None
        raise last_error


__all__ = [
    "AgentError",
    "AgentJSONParseError",
    "BaseAgent",
    "ChatLLM",
    "ContentParser",
    "MessagesFactory",
    "PROMPTS_DIR",
    "Sentiment",
    "extract_assistant_content",
    "load_prompt",
    "parse_strict_json",
    "render_prompt",
]
