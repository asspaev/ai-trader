"""Тесты :class:`PipelineScheduler` — фаза 8.

Покрытие:

* Сборка APScheduler-trigger'ов по конфигу (cron / interval).
* ``_safe_tick``: проверка флага паузы, подавление исключений runner'а.
* ``pause`` / ``resume`` / ``is_paused`` — round-trip через БД.
* ``trigger_now`` — игнорирует флаг паузы.
* ``start()`` — регистрирует job с ``max_instances=1`` и ``coalesce=True``,
  ``run_on_startup`` стартует immediate-тик только для interval-режима.

Валидация :class:`SchedulerSettings` (mode, cron_hours, interval_minutes)
вынесена в :mod:`tests.unit.test_config_scheduler` — она чисто-pydantic
и не требует ни event-loop, ни БД.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from apscheduler.triggers.combining import OrTrigger
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import SchedulerSettings
from app.services.pipeline.scheduler import (
    PIPELINE_JOB_ID,
    PipelineScheduler,
    ReloadResult,
)


pytestmark = pytest.mark.asyncio(loop_scope="session")


# ---------- общие фикстуры/хелперы ----------


@pytest_asyncio.fixture(loop_scope="session")
async def session_factory(engine, session) -> async_sessionmaker:
    """async_sessionmaker поверх тестового движка. ``session`` гарантирует TRUNCATE."""
    return async_sessionmaker(bind=engine, expire_on_commit=False)


class _CountingRunner:
    """Минимальный runner-заглушка для проверки числа вызовов."""

    def __init__(self) -> None:
        self.calls = 0
        self.error: Exception | None = None

    async def __call__(self) -> None:
        self.calls += 1
        if self.error is not None:
            raise self.error


def _make_scheduler(
    *,
    session_factory: async_sessionmaker,
    mode: str = "cron",
    cron_times: str = "00:00,06:00,12:00,18:00",
    interval_minutes: int = 30,
    run_on_startup: bool = True,
) -> tuple[PipelineScheduler, _CountingRunner]:
    """Собрать :class:`PipelineScheduler` со свежим runner-счётчиком."""
    runner = _CountingRunner()
    config = SchedulerSettings(
        mode=mode,
        cron_times=cron_times,
        interval_minutes=interval_minutes,
        run_on_startup=run_on_startup,
    )
    scheduler = PipelineScheduler(
        runner=runner,
        session_factory=session_factory,
        config=config,
    )
    return scheduler, runner


# ---------- _build_trigger ----------


async def test_build_trigger_cron_returns_or_trigger_in_utc(session_factory) -> None:
    """cron-режим всегда отдаёт OrTrigger из CronTrigger'ов с UTC-таймзоной."""
    sch, _ = _make_scheduler(
        session_factory=session_factory, mode="cron", cron_times="00:00,12:30"
    )
    trigger = sch._build_trigger()
    assert isinstance(trigger, OrTrigger)
    assert len(trigger.triggers) == 2
    for sub in trigger.triggers:
        assert isinstance(sub, CronTrigger)
        # TZ должен быть UTC — иначе расписание поедет в локальной зоне.
        assert "UTC" in str(sub.timezone)


async def test_build_trigger_cron_single_time_still_or_trigger(session_factory) -> None:
    """Даже одна пара ``HH:MM`` оборачивается в OrTrigger для единообразия."""
    sch, _ = _make_scheduler(
        session_factory=session_factory, mode="cron", cron_times="09:30"
    )
    trigger = sch._build_trigger()
    assert isinstance(trigger, OrTrigger)
    assert len(trigger.triggers) == 1
    assert isinstance(trigger.triggers[0], CronTrigger)


async def test_build_trigger_interval_uses_configured_minutes(session_factory) -> None:
    sch, _ = _make_scheduler(
        session_factory=session_factory, mode="interval", interval_minutes=15
    )
    trigger = sch._build_trigger()
    assert isinstance(trigger, IntervalTrigger)
    assert trigger.interval.total_seconds() == 15 * 60


async def test_build_trigger_rejects_unsupported_mode(session_factory) -> None:
    """Защита от ручной мутации config — bypass валидатора."""
    sch, _ = _make_scheduler(session_factory=session_factory, mode="cron")
    # ``object.__setattr__`` обходит pydantic-валидатор — имитируем
    # «программную» порчу config'а, чтобы убедиться: scheduler сам
    # тоже проверяет режим перед использованием.
    object.__setattr__(sch.config, "mode", "rumba")
    with pytest.raises(ValueError):
        sch._build_trigger()


# ---------- _safe_tick: pause-флаг и подавление исключений ----------


async def test_safe_tick_calls_runner_when_not_paused(session_factory) -> None:
    sch, runner = _make_scheduler(session_factory=session_factory, mode="interval")
    await sch._safe_tick()
    assert runner.calls == 1


async def test_safe_tick_skips_runner_when_paused(session_factory) -> None:
    sch, runner = _make_scheduler(session_factory=session_factory, mode="interval")
    await sch.pause()

    await sch._safe_tick()

    assert runner.calls == 0


async def test_safe_tick_swallows_runner_exception(session_factory) -> None:
    """Сбой runner'а не должен валить весь scheduler-цикл."""
    sch, runner = _make_scheduler(session_factory=session_factory, mode="interval")
    runner.error = RuntimeError("boom from runner")

    # Не должен бросать наружу.
    await sch._safe_tick()

    assert runner.calls == 1  # runner всё же вызывался


# ---------- pause / resume / is_paused: round-trip через БД ----------


async def test_pause_and_resume_round_trip(session_factory) -> None:
    sch, _ = _make_scheduler(session_factory=session_factory, mode="cron")

    assert await sch.is_paused() is False

    await sch.pause()
    assert await sch.is_paused() is True

    await sch.resume()
    assert await sch.is_paused() is False


async def test_trigger_now_ignores_pause_flag(session_factory) -> None:
    """Принудительный запуск работает и на паузе (для ``/start_pipeline``)."""
    sch, runner = _make_scheduler(session_factory=session_factory, mode="cron")
    await sch.pause()

    await sch.trigger_now()

    assert runner.calls == 1


async def test_trigger_now_propagates_runner_exception(session_factory) -> None:
    """В отличие от ``_safe_tick``, force-вызов отдаёт ошибку наверх."""
    sch, runner = _make_scheduler(session_factory=session_factory, mode="cron")
    runner.error = RuntimeError("manual fail")

    with pytest.raises(RuntimeError, match="manual fail"):
        await sch.trigger_now()
    assert runner.calls == 1


# ---------- start() / shutdown() ----------


async def test_start_registers_job_with_overlap_protection(session_factory) -> None:
    """``add_job`` должен выставить ``max_instances=1`` и ``coalesce=True``."""
    sch, _ = _make_scheduler(
        session_factory=session_factory, mode="cron", run_on_startup=False
    )
    try:
        sch.start()
        job = sch.scheduler.get_job(PIPELINE_JOB_ID)
        assert job is not None
        assert job.max_instances == 1
        assert job.coalesce is True
        assert isinstance(job.trigger, OrTrigger)
    finally:
        await sch.shutdown(wait=False)


async def test_start_with_interval_run_on_startup_fires_immediately(
    session_factory,
) -> None:
    """interval + run_on_startup=true → один тик сразу, до первого интервала."""
    sch, runner = _make_scheduler(
        session_factory=session_factory,
        mode="interval",
        interval_minutes=60,  # достаточно большой gap, чтобы регулярный тик не сработал в тесте
        run_on_startup=True,
    )
    try:
        sch.start()
        assert sch._startup_task is not None
        await sch._startup_task
        assert runner.calls == 1
    finally:
        await sch.shutdown(wait=False)


async def test_start_with_cron_does_not_run_on_startup(session_factory) -> None:
    """В cron-режиме ``run_on_startup`` игнорируется (даже если =true)."""
    sch, runner = _make_scheduler(
        session_factory=session_factory,
        mode="cron",
        run_on_startup=True,
    )
    try:
        sch.start()
        # Дать циклу пнуться, чтобы любой случайный create_task успел исполниться.
        await asyncio.sleep(0)
        assert sch._startup_task is None
        assert runner.calls == 0
    finally:
        await sch.shutdown(wait=False)


async def test_start_with_interval_run_on_startup_false_does_not_fire(
    session_factory,
) -> None:
    """interval + run_on_startup=false → стартового тика нет."""
    sch, runner = _make_scheduler(
        session_factory=session_factory,
        mode="interval",
        interval_minutes=60,
        run_on_startup=False,
    )
    try:
        sch.start()
        await asyncio.sleep(0)
        assert sch._startup_task is None
        assert runner.calls == 0
    finally:
        await sch.shutdown(wait=False)


async def test_startup_tick_respects_pause_flag(session_factory) -> None:
    """Если флаг паузы выставлен до старта — startup-тик его уважает."""
    sch, runner = _make_scheduler(
        session_factory=session_factory,
        mode="interval",
        interval_minutes=60,
        run_on_startup=True,
    )
    await sch.pause()
    try:
        sch.start()
        assert sch._startup_task is not None
        await sch._startup_task
        # Runner не должен был быть вызван, потому что _safe_tick
        # отрабатывает ранний выход по pause-флагу.
        assert runner.calls == 0
    finally:
        await sch.shutdown(wait=False)


# ---------- метаданные ----------


async def test_scheduler_exposes_underlying_apscheduler(session_factory) -> None:
    """Хук для отладки/диагностики: ``.scheduler`` отдаёт ``AsyncIOScheduler``."""
    sch, _ = _make_scheduler(session_factory=session_factory, mode="cron")
    # AsyncIOScheduler не запущен — это нормально (тесты не вызывают start).
    assert sch.scheduler is not None
    assert sch.config.mode == "cron"


async def test_pipeline_runner_protocol_accepts_plain_async_callable(
    session_factory,
) -> None:
    """:class:`PipelineRunner` — это ``Callable[[], Awaitable[None]]``."""

    async def runner() -> None:  # noqa: D401 — простой no-op
        return None

    # Не должно падать на инициализации.
    config = SchedulerSettings(mode="cron")
    sch: Any = PipelineScheduler(
        runner=runner,
        session_factory=session_factory,
        config=config,
    )
    assert sch is not None


# ---------- reload() ----------


@pytest_asyncio.fixture(loop_scope="session")
async def _isolated_env_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Чистая CWD с пустым ``.env`` и без ``SCHEDULER_*`` в окружении.

    Делает reload-тесты воспроизводимыми независимо от .env репозитория и
    переменных окружения dev-машины.
    """
    monkeypatch.chdir(tmp_path)
    for key in [k for k in os.environ if k.startswith("SCHEDULER_")]:
        monkeypatch.delenv(key, raising=False)
    (tmp_path / ".env").write_text("", encoding="utf-8")
    return tmp_path


async def test_reload_picks_up_new_times_from_env_file(
    session_factory, _isolated_env_dir: Path
) -> None:
    """Меняем ``.env`` → ``reload`` подхватывает новое расписание."""
    (_isolated_env_dir / ".env").write_text(
        "SCHEDULER_MODE=cron\nSCHEDULER_CRON_TIMES=00:00,12:00\n",
        encoding="utf-8",
    )
    sch, _ = _make_scheduler(
        session_factory=session_factory, mode="cron", cron_times="06:00"
    )
    try:
        sch.start()

        (_isolated_env_dir / ".env").write_text(
            "SCHEDULER_MODE=cron\nSCHEDULER_CRON_TIMES=03:00,09:30,21:00\n",
            encoding="utf-8",
        )
        result = await sch.reload()

        assert isinstance(result, ReloadResult)
        assert sch.config.cron_times == "03:00,09:30,21:00"
        job = sch.scheduler.get_job(PIPELINE_JOB_ID)
        assert job is not None
        assert isinstance(job.trigger, OrTrigger)
        assert len(job.trigger.triggers) == 3
        assert "03:00" in result.new_description
        assert "09:30" in result.new_description
        assert "21:00" in result.new_description
    finally:
        await sch.shutdown(wait=False)


async def test_reload_can_switch_mode_cron_to_interval(
    session_factory, _isolated_env_dir: Path
) -> None:
    """Через ``.env`` можно переключить режим cron → interval."""
    (_isolated_env_dir / ".env").write_text(
        "SCHEDULER_MODE=interval\nSCHEDULER_INTERVAL_MINUTES=15\n",
        encoding="utf-8",
    )
    sch, _ = _make_scheduler(
        session_factory=session_factory, mode="cron", cron_times="00:00"
    )
    try:
        sch.start()
        result = await sch.reload()

        assert sch.config.mode == "interval"
        assert sch.config.interval_minutes == 15
        job = sch.scheduler.get_job(PIPELINE_JOB_ID)
        assert job is not None
        assert isinstance(job.trigger, IntervalTrigger)
        assert "interval" in result.new_description
    finally:
        await sch.shutdown(wait=False)


async def test_reload_propagates_validation_error_and_keeps_old_trigger(
    session_factory, _isolated_env_dir: Path
) -> None:
    """Битый ``.env`` → reload бросает исключение, старый trigger жив."""
    (_isolated_env_dir / ".env").write_text(
        "SCHEDULER_MODE=cron\nSCHEDULER_CRON_TIMES=99:99\n",
        encoding="utf-8",
    )
    sch, _ = _make_scheduler(
        session_factory=session_factory, mode="cron", cron_times="06:00"
    )
    try:
        sch.start()
        with pytest.raises(Exception):
            await sch.reload()

        # Старый trigger по-прежнему зарегистрирован.
        job = sch.scheduler.get_job(PIPELINE_JOB_ID)
        assert job is not None
        assert isinstance(job.trigger, OrTrigger)
    finally:
        await sch.shutdown(wait=False)


async def test_reload_ignores_stale_os_environ_value(
    session_factory, _isolated_env_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """В Docker .env запекается в os.environ при старте; reload должен
    предпочесть текущий файл, а не запечённое значение."""
    monkeypatch.setenv("SCHEDULER_MODE", "cron")
    monkeypatch.setenv("SCHEDULER_CRON_TIMES", "06:00")  # «запечённое» из контейнера
    (_isolated_env_dir / ".env").write_text(
        "SCHEDULER_MODE=cron\nSCHEDULER_CRON_TIMES=00:00,12:00,18:00\n",
        encoding="utf-8",
    )
    sch, _ = _make_scheduler(
        session_factory=session_factory, mode="cron", cron_times="06:00"
    )
    try:
        sch.start()
        await sch.reload()
        assert sch.config.cron_times == "00:00,12:00,18:00"

        # os.environ остался нетронутым после reload — backup восстановился.
        assert os.environ.get("SCHEDULER_CRON_TIMES") == "06:00"
    finally:
        await sch.shutdown(wait=False)
