"""APScheduler-обёртка вокруг pipeline runner.

Назначение модуля — отделить «когда запускать тик» от «что делать на
тике». Сам тик умеет :func:`app.services.pipeline.runner.run_pipeline_once`,
а здесь живёт логика расписания: cron-часы UTC либо периодический
interval, защита от перекрытий, immediate-run-on-startup и проверка
флага паузы в БД.

Режимы — из ``SCHEDULER_MODE``:

* ``cron``  — :class:`CronTrigger` по часам UTC из ``SCHEDULER_CRON_HOURS``
  (CSV, например ``"0,6,12,18"``).
* ``interval`` — :class:`IntervalTrigger` каждые
  ``SCHEDULER_INTERVAL_MINUTES`` минут. Если
  ``SCHEDULER_RUN_ON_STARTUP=true`` — дополнительно делаем один тик
  сразу после ``start()``.

Защита от перекрытий: при ``add_job`` выставлены ``max_instances=1`` и
``coalesce=True``. Это значит, что:

* пока предыдущий тик идёт, следующий не запустится (а просто
  пропустится),
* несколько пропущенных срабатываний склеиваются в одно — догон не
  устраиваем (это важно после длительной паузы по ``/stop``).

Флаг паузы (для ``/stop``/``/resume``) живёт в таблице
``scheduler_state`` (singleton). Каждый раз перед запуском tick'а
читаем флаг — если ``paused=true``, тик пропускается с INFO-логом.
В отличие от APScheduler ``pause_job``, такой подход переживает
рестарт процесса: после ``docker compose restart`` сервис стартует
уже «выключенным», и /resume его включает.

Команда ``/start_pipeline`` (фаза 9) использует :meth:`trigger_now`,
которая запускает runner вне расписания и **игнорирует флаг паузы**:
форс-запуск — это явное действие авторизованного пользователя.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Final

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.base import BaseTrigger
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import SchedulerSettings, settings
from app.crud import scheduler_state as scheduler_state_crud


PIPELINE_JOB_ID: Final[str] = "pipeline_tick"
"""Стабильный ID job'а в APScheduler — нужен для ``replace_existing``."""


PipelineRunner = Callable[[], Awaitable[None]]
"""No-arg async-колбэк, который запускает один тик pipeline.

Реальная реализация — лямбда, замыкающая :class:`PipelineContext`
вокруг :func:`run_pipeline_once`. В тестах подменяется на mock.
"""


class PipelineScheduler:
    """Планировщик одного pipeline-тика."""

    def __init__(
        self,
        *,
        runner: PipelineRunner,
        session_factory: async_sessionmaker[AsyncSession],
        config: SchedulerSettings | None = None,
        scheduler: AsyncIOScheduler | None = None,
    ) -> None:
        """Собрать планировщик.

        Args:
            runner: Корутина, которая выполняет один тик. Никаких
                аргументов не принимает; контекст pipeline она замыкает
                в себе (обычно через лямбду в ``app/main.py``).
            session_factory: Фабрика async-сессий БД. Используется
                **только** для чтения/записи флага паузы — не для
                pipeline-логики, у которой свой набор сессий.
            config: Кастомные настройки расписания. По умолчанию
                берётся ``settings.scheduler``.
            scheduler: Готовый :class:`AsyncIOScheduler` (полезно в
                тестах). По умолчанию создаётся новый с TZ=UTC.
        """
        self._runner = runner
        self._session_factory = session_factory
        self._config = config or settings.scheduler
        self._scheduler = scheduler or AsyncIOScheduler(timezone="UTC")
        self._startup_task: asyncio.Task[None] | None = None
        self._log = logger.bind(component="pipeline.scheduler")

    @property
    def scheduler(self) -> AsyncIOScheduler:
        """Доступ к нижнему ``AsyncIOScheduler`` (для тестов/диагностики)."""
        return self._scheduler

    @property
    def config(self) -> SchedulerSettings:
        """Snapshot настроек, с которыми планировщик был собран."""
        return self._config

    # ---------- lifecycle ----------

    def start(self) -> None:
        """Зарегистрировать job, запустить scheduler и (опц.) первый тик.

        Безопасно вызывать многократно: благодаря ``replace_existing=True``
        повторный ``add_job`` просто перепишет старую регистрацию. Если
        scheduler уже запущен — повторный ``start()`` падает ``SchedulerAlreadyRunningError``
        от APScheduler; считаем это программной ошибкой и наверх не глотаем.
        """
        trigger = self._build_trigger()
        self._scheduler.add_job(
            self._safe_tick,
            trigger=trigger,
            id=PIPELINE_JOB_ID,
            name="pipeline_tick",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
        self._scheduler.start()
        self._log.info(
            "Pipeline scheduler started: mode={mode}, trigger={trigger}",
            mode=self._config.mode,
            trigger=_describe_trigger(trigger),
        )

        if self._should_run_on_startup():
            self._log.info("Scheduling immediate startup tick (run_on_startup=true)")
            self._startup_task = asyncio.create_task(
                self._safe_tick(),
                name="pipeline-startup-tick",
            )

    async def shutdown(self, *, wait: bool = False) -> None:
        """Корректно остановить scheduler.

        Args:
            wait: Если ``True`` — ждём завершения уже запущенных job'ов.
                По умолчанию — fire-and-forget shutdown, корректный для
                Ctrl+C / sigterm-сценариев в ``app/main.py``.
        """
        if self._scheduler.running:
            self._scheduler.shutdown(wait=wait)
        if self._startup_task is not None and not self._startup_task.done():
            self._startup_task.cancel()
            try:
                await self._startup_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                # Startup-тик мог упасть до отмены — лог уже есть в _safe_tick.
                pass
        self._log.info("Pipeline scheduler shutdown")

    # ---------- pause / resume / force ----------

    async def pause(self) -> None:
        """Поставить pipeline на паузу (флаг ``paused=true`` в БД).

        Сам APScheduler-job остаётся зарегистрированным — он просто
        будет каждый раз попадать в ранний return из :meth:`_safe_tick`.
        """
        await self._write_paused(True)
        self._log.info("Pipeline paused via DB flag")

    async def resume(self) -> None:
        """Снять паузу (флаг ``paused=false`` в БД)."""
        await self._write_paused(False)
        self._log.info("Pipeline resumed via DB flag")

    async def is_paused(self) -> bool:
        """Прочитать текущее значение флага паузы."""
        async with self._session_factory() as session:
            return await scheduler_state_crud.is_paused(session)

    async def trigger_now(self) -> None:
        """Принудительный запуск тика (например, по ``/start_pipeline``).

        Игнорирует флаг паузы: команду пользователь шлёт сознательно,
        и блокировать её было бы неожиданным поведением. Любая ошибка
        runner'а пробрасывается вверх — пусть telegram-хендлер сам
        решит, как уведомить пользователя.
        """
        self._log.info("Pipeline tick triggered manually (ignoring pause flag)")
        await self._runner()

    # ---------- internals ----------

    async def _safe_tick(self) -> None:
        """Обёртка вокруг runner с проверкой паузы и подавлением исключений.

        Любая ошибка runner'а **логируется**, но дальше не пробрасывается:
        иначе APScheduler пометит job сломанным и (в зависимости от
        настроек) перестанет планировать следующие тики. Pipeline-runner
        внутри уже изолирует ошибки по монетам — сюда долетают только
        совсем неожиданные исключения.
        """
        if await self.is_paused():
            self._log.info("Pipeline tick skipped: scheduler paused")
            return
        try:
            await self._runner()
        except Exception:  # noqa: BLE001 — последний рубеж scheduler-цикла
            self._log.exception("Pipeline tick failed with unexpected error")

    def _build_trigger(self) -> BaseTrigger:
        """Сконструировать APScheduler-trigger по настройкам."""
        if self._config.mode == "cron":
            return CronTrigger(
                hour=self._config.cron_hours,
                minute=0,
                timezone="UTC",
            )
        if self._config.mode == "interval":
            return IntervalTrigger(minutes=self._config.interval_minutes)
        # ``SchedulerSettings`` уже валидирует ``mode``; этот if -
        # на случай, если кто-то соберёт инстанс с самописным config'ом.
        raise ValueError(
            f"Unsupported SCHEDULER_MODE={self._config.mode!r} "
            "(expected 'cron' or 'interval')"
        )

    def _should_run_on_startup(self) -> bool:
        """Запуск немедленного тика осмысленен только для interval-режима.

        В cron-режиме у пользователя есть явное ожидание «тики в
        00/06/12/18 UTC», и неожиданный «бонусный» тик на старте только
        собьёт расписание и испортит ``coalesce``-логику.
        """
        return self._config.mode == "interval" and self._config.run_on_startup

    async def _write_paused(self, paused: bool) -> None:
        """Записать флаг паузы в БД с автоматическим коммитом."""
        async with self._session_factory() as session:
            await scheduler_state_crud.set_paused(session, paused=paused)
            await session.commit()


def _describe_trigger(trigger: BaseTrigger) -> str:
    """Короткое человекочитаемое описание trigger'а для лога.

    APScheduler-классы умеют ``str(trigger)`` (например,
    ``"cron[hour='0,6,12,18']"``), этого вполне достаточно — ничего
    самописного парсить не нужно.
    """
    if isinstance(trigger, IntervalTrigger):
        # У IntervalTrigger ``str`` отдаёт relativedelta-формат; для
        # человека читабельнее «N минут».
        minutes = trigger.interval.total_seconds() / 60
        return f"interval(minutes={minutes:g})"
    return str(trigger)


__all__ = [
    "PIPELINE_JOB_ID",
    "PipelineRunner",
    "PipelineScheduler",
]
