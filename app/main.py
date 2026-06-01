"""Точка входа приложения (фаза 9).

Один процесс держит и планировщик (:mod:`app.services.pipeline.scheduler`),
и Telegram-бота (:mod:`app.services.telegram.bot`) — оба запускаются
параллельно через ``asyncio.gather``-подобную конструкцию, как описано
в ``architecture.md`` §11 / §10.

Последовательность старта:

1. Конфигурируем логирование.
2. Достаём из БД одного init-пользователя; если его нет — сразу
   завершаем процесс с понятной диагностикой (надо сначала запустить
   ``scripts/init_user.py``).
3. Поднимаем долгоживущие клиенты: Binance, CoinDesk Data, OpenRouter,
   загружаем кэш ``exchangeInfo``.
4. Собираем :class:`PipelineContext`, ``aiogram.Bot`` (если задан токен),
   :class:`TelegramNotifier`, :class:`PipelineScheduler` и
   :class:`TelegramBotRunner` — в таком порядке, чтобы разорвать
   циклическую зависимость scheduler ↔ bot.
5. Стартуем scheduler (runner внутри зовёт ``run_pipeline_once`` с
   notifier'ом) и polling бота. Ждём сигнал / ошибку.
6. На ``KeyboardInterrupt`` / ``SIGTERM`` корректно гасим всё:
   отменяем задачи, шатдауним scheduler/bot, закрываем httpx-клиенты,
   освобождаем pool БД.

Если ``TELEGRAM_BOT_TOKEN`` не задан, бот не запускается, а pipeline
работает с :class:`NoOpNotifier` — это безопасный режим для локальной
отладки без Telegram.
"""

from __future__ import annotations

import asyncio
import signal
from contextlib import asynccontextmanager

from loguru import logger

from app.config import settings
from app.core.db import SessionLocal, dispose_engine
from app.core.logger import configure_logging
from app.crud import user as user_crud
from app.services.binance.client import BinanceClient
from app.services.binance.exchange_info import (
    ExchangeInfoCache,
    load_exchange_info,
)
from app.services.llm.openrouter import OpenRouterClient
from app.services.news.coindesk import CoinDeskNewsClient
from app.services.pipeline.crypto_step import PipelineContext
from app.services.pipeline.notifier import NoOpNotifier, PipelineNotifier
from app.services.pipeline.runner import run_pipeline_once
from app.services.pipeline.scheduler import PipelineScheduler
from app.services.telegram.bot import TelegramBotRunner
from app.services.telegram.handlers import HandlerDeps
from app.services.telegram.notifier import TelegramNotifier


async def _run() -> None:
    """Полный async-life-cycle процесса (см. docstring модуля)."""
    configure_logging()
    log = logger.bind(component="main")

    async with SessionLocal() as session:
        user = await user_crud.get_singleton(session)
    if user is None:
        log.error(
            "No init user in DB — run `python -m scripts.init_user` first"
        )
        return

    log.info(
        "Starting AI-Trader: user_id={uid}, telegram_id={tg}",
        uid=user.id,
        tg=user.telegram_id,
    )

    async with BinanceClient() as binance_client, \
            CoinDeskNewsClient() as news_client, \
            OpenRouterClient() as openrouter_client:
        symbols = tuple(s.upper() for s in settings.trading.symbols)
        quote_asset = settings.trading.quote_asset
        pairs = [f"{s}{quote_asset}" for s in symbols]
        exchange_info: ExchangeInfoCache = await load_exchange_info(
            binance_client, pairs
        )

        context = PipelineContext.build(
            user_id=user.id,
            binance_client=binance_client,
            news_client=news_client,
            openrouter_client=openrouter_client,
            exchange_info=exchange_info,
            session_factory=SessionLocal,
        )

        # ---- bot + notifier ----
        token = settings.telegram.bot_token
        if token:
            aio_bot = TelegramBotRunner.make_bot(token)
            notifier: PipelineNotifier = TelegramNotifier(
                bot=aio_bot,
                chat_id=user.telegram_id,
                session_factory=SessionLocal,
                binance_client=binance_client,
                user_id=user.id,
                quote_asset=quote_asset,
                symbols=symbols,
            )
        else:
            log.warning(
                "TELEGRAM_BOT_TOKEN is empty — bot is disabled, "
                "pipeline runs with NoOpNotifier"
            )
            aio_bot = None
            notifier = NoOpNotifier()

        # ---- scheduler ----
        async def runner_callable() -> None:
            await run_pipeline_once(context=context, notifier=notifier)

        scheduler = PipelineScheduler(
            runner=runner_callable,
            session_factory=SessionLocal,
        )

        # ---- bot dispatcher (после scheduler — нужна ссылка) ----
        bot_runner: TelegramBotRunner | None = None
        if aio_bot is not None:
            deps = HandlerDeps(
                session_factory=SessionLocal,
                scheduler=scheduler,
                binance_client=binance_client,
                user_id=user.id,
                allowed_telegram_id=user.telegram_id,
                quote_asset=quote_asset,
                symbols=symbols,
                history_limit_default=settings.telegram.history_limit_default,
                history_limit_max=settings.telegram.history_limit_max,
            )
            bot_runner = TelegramBotRunner.build(bot=aio_bot, deps=deps)

        await _supervised_run(scheduler=scheduler, bot_runner=bot_runner, log=log)


async def _supervised_run(
    *,
    scheduler: PipelineScheduler,
    bot_runner: TelegramBotRunner | None,
    log,
) -> None:
    """Запустить scheduler и (опц.) бот; ждать сигнал/ошибку и погасить.

    Любой ``KeyboardInterrupt`` / ``SIGTERM`` транслируется в отмену
    всех задач — после чего корректно гасим scheduler/bot и закрываем
    pool БД.
    """
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _signal_handler() -> None:
        if not stop_event.is_set():
            log.info("Shutdown signal received, stopping services")
            stop_event.set()

    for sig_name in ("SIGINT", "SIGTERM"):
        sig = getattr(signal, sig_name, None)
        if sig is None:
            continue
        try:
            loop.add_signal_handler(sig, _signal_handler)
        except (NotImplementedError, RuntimeError):
            # На Windows и в некоторых embedded-окружениях это недоступно —
            # KeyboardInterrupt доходит до asyncio.run сам.
            pass

    scheduler.start()

    try:
        async with _maybe_bot(bot_runner) as bot_task:
            stop_task = asyncio.create_task(stop_event.wait(), name="stop-wait")
            waiters: list[asyncio.Task] = [stop_task]
            if bot_task is not None:
                waiters.append(bot_task)
            done, pending = await asyncio.wait(
                waiters, return_when=asyncio.FIRST_COMPLETED
            )
            for task in pending:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
            # Если бот упал с исключением — пробрасываем его, чтобы supervisor
            # перезапустил контейнер.
            for task in done:
                if task is stop_task:
                    continue
                exc = task.exception()
                if exc is not None:
                    raise exc
    finally:
        await scheduler.shutdown(wait=False)
        await dispose_engine()


@asynccontextmanager
async def _maybe_bot(bot_runner: TelegramBotRunner | None):
    """Контекст: запустить polling бота, если он есть; иначе — no-op."""
    if bot_runner is None:
        yield None
        return
    async with bot_runner:
        task = asyncio.create_task(bot_runner.start_polling(), name="bot-polling")
        try:
            yield task
        finally:
            if not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass


def main() -> None:
    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        logger.info("Interrupted by user")


if __name__ == "__main__":
    main()
