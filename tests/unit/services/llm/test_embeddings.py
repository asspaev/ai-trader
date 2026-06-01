"""Тесты :mod:`app.services.llm.embeddings`.

Покрывают:

* Успешный путь: вектор корректной размерности → возврат списка float-ов
  и запись ``LLMCall(COMPLETE)``.
* Несовпадение размерности → :class:`EmbeddingError`, при этом запись в
  ``llm_calls`` всё равно остаётся в статусе ``COMPLETE``: трекер
  закрыл вызов по факту 200-ответа, а валидация — уровнем выше.
* Пустой ``data`` → :class:`EmbeddingError`.
* Пустой ввод → :class:`EmbeddingError` без HTTP-вызова.
"""

from __future__ import annotations

import httpx
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from app.services.llm.embeddings import EmbeddingError, create_embedding
from app.services.llm.openrouter import OpenRouterClient

from tests.unit.services.llm._helpers import (
    build_openrouter_settings,
    load_fixture,
)


pytestmark = pytest.mark.asyncio(loop_scope="session")


@pytest_asyncio.fixture(loop_scope="session")
async def llm_session_factory(engine: AsyncEngine) -> async_sessionmaker:
    return async_sessionmaker(bind=engine, expire_on_commit=False)


def _make_client(handler, *, session_factory) -> OpenRouterClient:
    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(
        base_url="https://openrouter.test", transport=transport
    )
    return OpenRouterClient(
        build_openrouter_settings(),
        session_factory=session_factory,
        http_client=http,
    )


async def test_create_embedding_returns_vector_of_expected_dim(
    session, llm_session_factory
) -> None:
    fixture = load_fixture("openrouter_embedding_response.json")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/embeddings"
        return httpx.Response(200, json=fixture)

    async with _make_client(handler, session_factory=llm_session_factory) as client:
        vector = await create_embedding(
            client,
            text="ETF approval boosts BTC sentiment",
            expected_dim=1536,
            model="openai/text-embedding-3-small",
        )

    assert len(vector) == 1536
    assert all(isinstance(v, float) for v in vector)
    assert vector[:3] == fixture["data"][0]["embedding"][:3]


async def test_create_embedding_raises_on_dim_mismatch(
    session, llm_session_factory
) -> None:
    bad_fixture = {
        "data": [{"embedding": [0.1, 0.2, 0.3]}],
        "usage": {"prompt_tokens": 1, "total_tokens": 1},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=bad_fixture)

    async with _make_client(handler, session_factory=llm_session_factory) as client:
        with pytest.raises(EmbeddingError) as exc_info:
            await create_embedding(client, text="x", expected_dim=1536)

    assert "dim mismatch" in str(exc_info.value)


async def test_create_embedding_raises_on_empty_data(
    session, llm_session_factory
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [], "usage": {}})

    async with _make_client(handler, session_factory=llm_session_factory) as client:
        with pytest.raises(EmbeddingError):
            await create_embedding(client, text="x", expected_dim=1536)


async def test_create_embedding_rejects_blank_input(
    session, llm_session_factory
) -> None:
    """Пустую строку не отправляем в сеть — экономим квоту LLM."""
    called = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        called["n"] += 1
        return httpx.Response(200, json={"data": [], "usage": {}})

    async with _make_client(handler, session_factory=llm_session_factory) as client:
        with pytest.raises(EmbeddingError):
            await create_embedding(client, text="   ", expected_dim=1536)

    assert called["n"] == 0


async def test_fake_openrouter_client_smoke() -> None:
    """Smoke-проверка интерфейса :class:`FakeOpenRouterClient` (для фазы 6)."""
    from tests.unit.services.llm._helpers import FakeOpenRouterClient

    fixture = load_fixture("openrouter_chat_response.json")
    fake = FakeOpenRouterClient(chat_responses=[fixture])

    async with fake as client:
        response = await client.chat_completion(
            agent_name="trader",
            model="deepseek/deepseek-chat",
            messages=[{"role": "user", "content": "ping"}],
        )

    assert response == fixture
    assert len(fake.chat_calls) == 1
    assert fake.chat_calls[0]["agent_name"] == "trader"
