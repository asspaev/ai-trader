"""CRUD-тесты для :mod:`app.crud.llm_call` (трекинг вызовов LLM)."""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest

from app.crud import llm_call as llm_crud
from app.models.enums import LLMCallStatus


pytestmark = pytest.mark.asyncio(loop_scope="session")


async def test_create_in_progress_then_complete(session):
    run_id = uuid.uuid4()

    record = await llm_crud.create_in_progress(
        session,
        agent_name="price",
        model="deepseek/deepseek-chat",
        request_payload={"messages": [{"role": "user", "content": "ping"}]},
        pipeline_run_id=run_id,
    )

    assert record.status is LLMCallStatus.IN_PROGRESS
    assert record.pipeline_run_id == run_id
    assert record.finished_at is None

    completed = await llm_crud.complete(
        session,
        call_id=record.id,
        response_payload={"choices": [{"message": {"content": "pong"}}]},
        prompt_tokens=10,
        completion_tokens=5,
        cost_usd=Decimal("0.001234"),
    )

    assert completed.status is LLMCallStatus.COMPLETE
    assert completed.prompt_tokens == 10
    assert completed.completion_tokens == 5
    assert completed.cost_usd == Decimal("0.001234")
    assert completed.response_payload == {
        "choices": [{"message": {"content": "pong"}}]
    }
    assert completed.finished_at is not None


async def test_mark_error_sets_status_and_text(session):
    record = await llm_crud.create_in_progress(
        session,
        agent_name="news_summary",
        model="deepseek/deepseek-chat",
        request_payload={"x": 1},
    )

    failed = await llm_crud.mark_error(
        session, call_id=record.id, error_text="timeout"
    )

    assert failed.status is LLMCallStatus.ERROR
    assert failed.error_text == "timeout"
    assert failed.finished_at is not None


async def test_list_for_pipeline_run(session):
    run_id = uuid.uuid4()

    for agent in ("price", "news_summary", "trader"):
        await llm_crud.create_in_progress(
            session,
            agent_name=agent,
            model="deepseek/deepseek-chat",
            request_payload={"agent": agent},
            pipeline_run_id=run_id,
        )
    # Запись из другого тика — не должна попасть.
    await llm_crud.create_in_progress(
        session,
        agent_name="price",
        model="deepseek/deepseek-chat",
        request_payload={"other": True},
        pipeline_run_id=uuid.uuid4(),
    )

    items = await llm_crud.list_for_pipeline_run(
        session, pipeline_run_id=run_id
    )

    assert [c.agent_name for c in items] == ["price", "news_summary", "trader"]


async def test_count_by_status(session):
    in_progress = await llm_crud.create_in_progress(
        session,
        agent_name="x",
        model="m",
        request_payload={},
    )
    completed = await llm_crud.create_in_progress(
        session,
        agent_name="x",
        model="m",
        request_payload={},
    )
    await llm_crud.complete(
        session, call_id=completed.id, response_payload={"ok": True}
    )
    errored = await llm_crud.create_in_progress(
        session,
        agent_name="x",
        model="m",
        request_payload={},
    )
    await llm_crud.mark_error(session, call_id=errored.id, error_text="oops")

    counts = await llm_crud.count_by_status(session)

    assert counts.get(LLMCallStatus.IN_PROGRESS) == 1
    assert counts.get(LLMCallStatus.COMPLETE) == 1
    assert counts.get(LLMCallStatus.ERROR) == 1
    # Sanity: запись действительно создалась
    assert in_progress.status is LLMCallStatus.IN_PROGRESS
