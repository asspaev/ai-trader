"""Async-клиент OpenRouter с трекингом каждого вызова в ``llm_calls``.

Каждый запрос к OpenRouter обёрнут трекером, который:

1. До отправки HTTP открывает запись :class:`LLMCall` в статусе
   ``IN_PROGRESS`` (отдельная транзакция БД, commit сразу) — это
   нужно, чтобы в случае краша процесса в БД остался след «запрос был
   отправлен».
2. Выполняет ретраи на ``429`` / ``5xx`` / сетевые ошибки
   с экспоненциальным backoff ``base ** (attempt - 1)`` секунд
   (по дефолту ``base=3``, ``max_retries=4`` → паузы ``1s/3s/9s``
   между попытками).
3. После успеха закрывает запись статусом ``COMPLETE`` и сохраняет
   полный payload + usage-токены.
4. На ошибку — закрывает запись статусом ``ERROR`` с текстом исключения
   и поднимает исключение наверх.

ВНИМАНИЕ. Трекер использует **собственные** сессии БД (через
``session_factory``) — он не пишет в сессию вызывающего, потому что
любой outer-транзакции может быть откачен, и тогда трек LLM-вызова
пропадёт. Для unit-тестов в ``session_factory`` подсовывают
``async_sessionmaker``, привязанный к тестовому engine.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any, Protocol

import httpx
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import OpenRouterSettings, settings
from app.core.db import SessionLocal
from app.crud import llm_call as llm_call_crud


_RETRYABLE_STATUS: frozenset[int] = frozenset({408, 425, 429, 500, 502, 503, 504})


class SessionFactory(Protocol):
    """Совместимый с ``async_sessionmaker`` колбэк выдачи сессии.

    Используется отдельным типом, чтобы в коде/тестах было ясно, что
    клиент хочет «фабрику сессий», а не уже открытую сессию.
    """

    def __call__(self) -> AsyncSession:  # pragma: no cover — структурный protocol
        ...


class OpenRouterError(Exception):
    """Ошибка ответа OpenRouter, которую не имеет смысла ретраить.

    Поднимается на HTTP-статусах, которые НЕ входят в
    :data:`_RETRYABLE_STATUS`, либо после исчерпания ретраев.
    """

    def __init__(self, status_code: int, payload: Any) -> None:
        super().__init__(f"OpenRouter error {status_code}: {payload!r}")
        self.status_code = status_code
        self.payload = payload


class OpenRouterClient:
    """Async-клиент с встроенным :class:`LLMCallTracker`.

    Используется как контекстный менеджер::

        async with OpenRouterClient() as client:
            data = await client.chat_completion(
                agent_name="price",
                model=settings.agent.price_model,
                messages=[...],
            )

    Параметры:
        config: Группа настроек OpenRouter; по умолчанию берётся
            ``settings.openrouter``.
        session_factory: Фабрика async-сессий БД. По умолчанию
            :data:`app.core.db.SessionLocal`. В тестах подменяется на
            sessionmaker, привязанный к тестовому engine.
        http_client: Опциональный готовый ``httpx.AsyncClient`` — если
            не передан, клиент создаётся внутри (и закрывается в
            :meth:`aclose`).
    """

    def __init__(
        self,
        config: OpenRouterSettings | None = None,
        *,
        session_factory: SessionFactory | async_sessionmaker[AsyncSession] | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._config = config or settings.openrouter
        self._session_factory: SessionFactory | async_sessionmaker[AsyncSession] = (
            session_factory or SessionLocal
        )
        self._owns_http = http_client is None
        self._http = http_client or httpx.AsyncClient(
            base_url=self._config.base_url,
            timeout=httpx.Timeout(self._config.timeout_seconds),
        )
        # Заголовки навешиваем per-request, чтобы они применялись и в том
        # случае, когда тесты передают свой ``http_client`` с MockTransport.
        self._default_headers = self._build_default_headers()

    async def __aenter__(self) -> "OpenRouterClient":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Закрыть внутренний httpx-клиент (если он создан внутри)."""
        if self._owns_http:
            await self._http.aclose()

    async def chat_completion(
        self,
        *,
        agent_name: str,
        model: str,
        messages: list[dict[str, Any]],
        pipeline_run_id: uuid.UUID | None = None,
        **extra: Any,
    ) -> dict[str, Any]:
        """Вызов ``POST /chat/completions`` с трекингом и ретраями.

        ``extra`` пробрасывается в тело запроса (например, ``response_format``,
        ``temperature``). Возвращаем сырой распаршенный JSON-ответ
        OpenRouter; парсинг бизнес-полей (контента) — задача агентов.
        """
        payload = {"model": model, "messages": messages, **extra}
        return await self._tracked_post(
            path="/chat/completions",
            agent_name=agent_name,
            model=model,
            payload=payload,
            pipeline_run_id=pipeline_run_id,
        )

    async def embeddings(
        self,
        *,
        agent_name: str,
        model: str,
        inputs: str | list[str],
        pipeline_run_id: uuid.UUID | None = None,
    ) -> dict[str, Any]:
        """Вызов ``POST /embeddings`` с тем же трекингом/ретраями."""
        payload: dict[str, Any] = {"model": model, "input": inputs}
        return await self._tracked_post(
            path="/embeddings",
            agent_name=agent_name,
            model=model,
            payload=payload,
            pipeline_run_id=pipeline_run_id,
        )

    # ---------- internals ----------

    async def _tracked_post(
        self,
        *,
        path: str,
        agent_name: str,
        model: str,
        payload: dict[str, Any],
        pipeline_run_id: uuid.UUID | None,
    ) -> dict[str, Any]:
        """Обёртка одного HTTP-вызова в жизненный цикл ``LLMCall``."""
        call_id = await self._open_call(
            agent_name=agent_name,
            model=model,
            payload=payload,
            pipeline_run_id=pipeline_run_id,
        )
        bound = logger.bind(
            llm_call_id=call_id,
            agent=agent_name,
            model=model,
            pipeline_run_id=str(pipeline_run_id) if pipeline_run_id else None,
        )
        try:
            response = await self._post_with_retries(path, payload, bound=bound)
        except Exception as exc:
            await self._fail_call(call_id, exc, bound=bound)
            raise
        await self._finalize_call(call_id, response, bound=bound)
        return response

    async def _open_call(
        self,
        *,
        agent_name: str,
        model: str,
        payload: dict[str, Any],
        pipeline_run_id: uuid.UUID | None,
    ) -> int:
        """Открыть запись ``LLMCall(IN_PROGRESS)`` отдельной транзакцией."""
        async with self._session_factory() as session:
            record = await llm_call_crud.create_in_progress(
                session,
                agent_name=agent_name,
                model=model,
                request_payload=payload,
                pipeline_run_id=pipeline_run_id,
            )
            await session.commit()
            return record.id

    async def _finalize_call(
        self, call_id: int, response: dict[str, Any], *, bound
    ) -> None:
        """Закрыть запись статусом ``COMPLETE`` и сохранить usage-токены."""
        usage = response.get("usage") if isinstance(response, dict) else None
        usage = usage if isinstance(usage, dict) else {}
        prompt_tokens = _safe_int(usage.get("prompt_tokens"))
        completion_tokens = _safe_int(usage.get("completion_tokens"))
        try:
            async with self._session_factory() as session:
                await llm_call_crud.complete(
                    session,
                    call_id=call_id,
                    response_payload=response,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                )
                await session.commit()
        except Exception:  # pragma: no cover — крайний случай: уронили БД
            bound.exception("Failed to mark LLMCall as COMPLETE")
            raise

    async def _fail_call(self, call_id: int, exc: Exception, *, bound) -> None:
        """Закрыть запись статусом ``ERROR``. Не подавляем исходное исключение."""
        text = f"{type(exc).__name__}: {exc}"[:2000]
        try:
            async with self._session_factory() as session:
                await llm_call_crud.mark_error(
                    session, call_id=call_id, error_text=text
                )
                await session.commit()
        except Exception:  # pragma: no cover — изоляция: лог + дальше
            bound.exception(
                "Failed to mark LLMCall as ERROR; original exception will be re-raised"
            )

    async def _post_with_retries(
        self,
        path: str,
        payload: dict[str, Any],
        *,
        bound,
    ) -> dict[str, Any]:
        """POST с экспоненциальным backoff на retryable-ошибки."""
        attempts = max(1, self._config.max_retries)
        backoff_base = float(self._config.retry_backoff_base)
        last_exc: Exception | None = None

        for attempt in range(1, attempts + 1):
            try:
                response = await self._http.post(
                    path, json=payload, headers=self._default_headers
                )
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_exc = exc
                bound.warning(
                    "OpenRouter transport error on attempt {attempt}/{attempts}: {exc}",
                    attempt=attempt,
                    attempts=attempts,
                    exc=type(exc).__name__,
                )
            else:
                if response.status_code == 200:
                    return _parse_json_or_raise(response)

                if response.status_code in _RETRYABLE_STATUS:
                    last_exc = OpenRouterError(
                        response.status_code, _safe_json(response)
                    )
                    bound.warning(
                        "OpenRouter retryable status {status} on attempt {attempt}/{attempts}",
                        status=response.status_code,
                        attempt=attempt,
                        attempts=attempts,
                    )
                else:
                    raise OpenRouterError(
                        response.status_code, _safe_json(response)
                    )

            if attempt < attempts:
                delay = backoff_base ** (attempt - 1)
                await asyncio.sleep(delay)

        assert last_exc is not None  # для type-checker
        raise last_exc

    def _build_default_headers(self) -> dict[str, str]:
        """Заголовки по умолчанию для всех запросов к OpenRouter."""
        headers: dict[str, str] = {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        if self._config.api_key:
            headers["Authorization"] = f"Bearer {self._config.api_key}"
        if self._config.http_referer:
            headers["HTTP-Referer"] = self._config.http_referer
        if self._config.app_title:
            headers["X-Title"] = self._config.app_title
        return headers


# ---------- module-level helpers ----------


def _safe_json(response: httpx.Response) -> Any:
    """Попробовать распарсить JSON ответа, иначе вернуть текст."""
    try:
        return response.json()
    except ValueError:
        return response.text


def _parse_json_or_raise(response: httpx.Response) -> dict[str, Any]:
    """Распарсить 200-ответ; если это не JSON — поднять :class:`OpenRouterError`."""
    try:
        payload = response.json()
    except ValueError as exc:
        raise OpenRouterError(response.status_code, response.text) from exc
    if not isinstance(payload, dict):
        raise OpenRouterError(response.status_code, payload)
    return payload


def _safe_int(value: Any) -> int | None:
    """Аккуратно превратить любое в int, либо вернуть None."""
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


__all__ = [
    "OpenRouterClient",
    "OpenRouterError",
    "SessionFactory",
]
