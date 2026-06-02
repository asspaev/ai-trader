"""APScheduler-обёртка вокруг pipeline runner.

Назначение модуля — отделить «когда запускать тик» от «что делать на
тике». Сам тик умеет :func:`app.services.pipeline.runner.run_pipeline_once`,
а здесь живёт логика расписания: cron-времена UTC либо периодический
interval, защита от перекрытий, immediate-run-on-startup и проверка
флага паузы в БД.

Режимы — из ``SCHEDULER_MODE``:

* ``cron``  — :class:`OrTrigger` из списка :class:`CronTrigger` (по
  одному на каждую пару ``HH:MM`` из ``SCHEDULER_CRON_TIMES``, например
  ``"00:00,06:00,09:30,14:15"``). Один CronTrigger не умеет «9:30 ИЛИ
  14:15» (поля ``hour`` и ``minute`` независимы — получится декартово
  произведение), поэтому собираем OrTrigger.
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

Команда ``/reload_schedule`` использует :meth:`reload`, которая
перечитывает ``SCHEDULER_*``-переменные напрямую из файла ``.env``
(минуя ``os.environ`` — это важно для Docker, см. docstring у
``reload``), пересобирает trigger и регистрирует job с
``replace_existing=True``.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.base import BaseTrigger
from apscheduler.triggers.combining import OrTrigger
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import SchedulerSettings, settings
from app.crud import scheduler_state as scheduler_state_crud


PIPELINE_JOB_ID: Final[str] = "pipeline_tick"
"""Стабильный ID job'а в APScheduler — нужен для ``replace_existing``."""


@dataclass(frozen=True, slots=True)
class ReloadResult:
    """Результат :meth:`PipelineScheduler.reload`.

    Используется Telegram-командой ``/reload_schedule`` для отчёта
    пользователю: что было, что стало, когда следующий тик.
    """

    old_description: str
    new_description: str
    next_run: datetime | None


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

    # ---------- reload ----------

    async def reload(self) -> ReloadResult:
        """Перечитать ``SCHEDULER_*`` из ``.env`` и перерегистрировать job.

        Под капотом — пересоздание :class:`SchedulerSettings`, но с
        предварительным удалением ``SCHEDULER_*`` из ``os.environ``:
        в Docker docker-compose ``env_file: .env`` инжектит значения
        в среду контейнера один раз при старте, и без этой подчистки
        pydantic-settings всегда возвращал бы запечённые значения, а
        не текущее содержимое смонтированного ``.env``.

        Безопасно для asyncio (single-threaded loop): между ``pop`` и
        ``update`` другой корутины с чтением ``os.environ`` быть не
        может.

        Возвращает :class:`ReloadResult` — описание старого и нового
        trigger'а плюс время следующего запуска. Telegram-хендлер
        использует это для ответа пользователю.

        Ошибки валидации :class:`SchedulerSettings` пробрасываются
        наружу: пусть вызывающий код сам решает, как сообщить о
        битом ``.env`` (и оставляет действующий trigger без изменений).
        """
        old_trigger = self._scheduler.get_job(PIPELINE_JOB_ID)
        old_description = (
            _describe_trigger(old_trigger.trigger)
            if old_trigger is not None
            else _describe_trigger(self._build_trigger())
        )

        new_config = _load_scheduler_settings_from_env_file()
        self._config = new_config
        new_trigger = self._build_trigger()

        self._scheduler.add_job(
            self._safe_tick,
            trigger=new_trigger,
            id=PIPELINE_JOB_ID,
            name="pipeline_tick",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )

        job = self._scheduler.get_job(PIPELINE_JOB_ID)
        next_run = job.next_run_time if job is not None else None

        new_description = _describe_trigger(new_trigger)
        self._log.info(
            "Pipeline schedule reloaded: {old} -> {new} (next_run={next})",
            old=old_description,
            new=new_description,
            next=next_run.isoformat() if next_run is not None else "n/a",
        )

        return ReloadResult(
            old_description=old_description,
            new_description=new_description,
            next_run=next_run,
        )

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
        """Сконструировать APScheduler-trigger по настройкам.

        Для cron-режима строим :class:`OrTrigger` из одного
        :class:`CronTrigger` на каждую пару ``HH:MM`` — одиночный
        CronTrigger с CSV-полями ``hour`` и ``minute`` даёт декартово
        произведение, а нам нужно «9:30 ИЛИ 14:15 ИЛИ 18:00». Для
        случая одной пары OrTrigger остаётся эквивалентным простому
        CronTrigger — APScheduler корректно с ним работает.
        """
        if self._config.mode == "cron":
            pairs = self._config.cron_pairs()
            return OrTrigger(
                [
                    CronTrigger(hour=h, minute=m, timezone="UTC")
                    for h, m in pairs
                ]
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
    """Короткое человекочитаемое описание trigger'а для лога/Telegram.

    Для cron-расписаний из OrTrigger собираем компактный список
    ``HH:MM`` — это понятнее, чем дамп APScheduler-полей.
    """
    if isinstance(trigger, IntervalTrigger):
        # У IntervalTrigger ``str`` отдаёт relativedelta-формат; для
        # человека читабельнее «N минут».
        minutes = trigger.interval.total_seconds() / 60
        return f"interval(minutes={minutes:g})"
    if isinstance(trigger, OrTrigger):
        times = [_describe_cron_trigger(t) for t in trigger.triggers]
        times = [t for t in times if t is not None]
        if times:
            return f"cron[UTC]({', '.join(times)})"
    if isinstance(trigger, CronTrigger):
        described = _describe_cron_trigger(trigger)
        if described is not None:
            return f"cron[UTC]({described})"
    return str(trigger)


def _describe_cron_trigger(trigger: BaseTrigger) -> str | None:
    """Вернуть ``HH:MM`` из CronTrigger, если у него только hour+minute.

    Любой нестандартный набор полей (day_of_week, month, ...) возвращает
    ``None`` — описывать его в виде HH:MM было бы враньём.
    """
    if not isinstance(trigger, CronTrigger):
        return None
    hour: int | None = None
    minute: int | None = None
    for field in trigger.fields:
        if field.is_default:
            continue
        # CronTrigger хранит каждое поле как объект с .name и .expressions;
        # для простых cron из (hour=H, minute=M) у нас будут ровно эти два.
        values = [str(expr) for expr in field.expressions]
        if len(values) != 1:
            return None
        try:
            value = int(values[0])
        except ValueError:
            return None
        if field.name == "hour":
            hour = value
        elif field.name == "minute":
            minute = value
        else:
            return None
    if hour is None or minute is None:
        return None
    return f"{hour:02d}:{minute:02d}"


def _load_scheduler_settings_from_env_file() -> SchedulerSettings:
    """Перечитать ``SchedulerSettings`` напрямую из ``.env``.

    Pydantic-settings по умолчанию читает в порядке: init kwargs →
    ``os.environ`` → ``.env``-файл → defaults. В Docker contant'ы из
    ``.env`` инжектятся в ``os.environ`` ещё при старте контейнера
    (``env_file: .env`` в compose), и обычный ``SchedulerSettings()``
    при reload вернул бы те же запечённые значения.

    Поэтому временно убираем все ``SCHEDULER_*`` из ``os.environ`` на
    время инстанцирования: pydantic-settings провалится на шаг ниже —
    к ``.env``-файлу (который у нас смонтирован в контейнер как
    ``/app/.env``). После — восстанавливаем переменные, чтобы не
    портить никому другому окружение.

    Safe для asyncio (single-threaded loop): между ``pop`` и
    ``update`` другая корутина с чтением ``os.environ`` не вклинится.
    """
    backup: dict[str, str] = {}
    keys = [k for k in list(os.environ.keys()) if k.startswith("SCHEDULER_")]
    try:
        for key in keys:
            backup[key] = os.environ.pop(key)
        return SchedulerSettings()
    finally:
        os.environ.update(backup)


__all__ = [
    "PIPELINE_JOB_ID",
    "PipelineRunner",
    "PipelineScheduler",
    "ReloadResult",
]
