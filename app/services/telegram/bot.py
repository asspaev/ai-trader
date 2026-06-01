"""Сборка и lifecycle aiogram-бота (фаза 9).

Модуль предоставляет :class:`TelegramBotRunner` — тонкая обёртка над
``aiogram.Bot`` + ``Dispatcher``, чтобы ``app/main.py`` мог запустить
бота параллельно с планировщиком одной строкой::

    async with TelegramBotRunner.build(deps) as runner:
        await asyncio.gather(scheduler_task, runner.start_polling())

Polling-режим выбран сознательно: webhook требует внешнего HTTPS-URL,
а в MVP сервис деплоится одним контейнером без публичного хоста.
"""

from __future__ import annotations

from typing import Final

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from loguru import logger

from app.services.telegram.handlers import HandlerDeps, build_router


_DROP_PENDING_ON_STARTUP: Final[bool] = True
"""При старте сбрасываем накопившийся long-poll backlog Telegram.

Если бот лежал час, у Telegram могла скопиться очередь обновлений
(в т.ч. от чужих чатов). Обрабатывать их «как только включились» —
скорее источник путаницы, чем пользы: пользователь обычно ожидает,
что после ``/stop``+перезапуска бот «начинается заново».
"""


class TelegramBotRunner:
    """Управляет жизненным циклом aiogram-бота.

    Использовать как async-контекст менеджер — это гарантирует, что
    HTTP-сессия aiogram'а будет закрыта даже при ошибке::

        async with TelegramBotRunner.build(deps=deps, token=...) as runner:
            await runner.start_polling()
    """

    def __init__(
        self,
        *,
        bot: Bot,
        dispatcher: Dispatcher,
        deps: HandlerDeps,
    ) -> None:
        self._bot = bot
        self._dispatcher = dispatcher
        self._deps = deps
        self._log = logger.bind(component="telegram.bot")

    @staticmethod
    def make_bot(token: str) -> Bot:
        """Создать «голый» ``aiogram.Bot`` (нужен до сборки notifier'а).

        Bot нужен раньше, чем мы можем построить :class:`HandlerDeps`
        (та хочет ссылку на scheduler, а scheduler — на notifier, а
        notifier — на ``aiogram.Bot``). Поэтому экземпляр Bot создаём
        отдельно, а позже передаём его сюда вместе с готовыми deps.
        """
        if not token:
            raise ValueError("Telegram bot token is empty; cannot build bot")
        return Bot(token=token, default=DefaultBotProperties())

    @classmethod
    def build(
        cls,
        *,
        bot: Bot,
        deps: HandlerDeps,
    ) -> "TelegramBotRunner":
        """Завернуть готовый ``Bot`` в Dispatcher с зарегистрированным роутером.

        Args:
            bot: Уже созданный ``aiogram.Bot`` (см. :meth:`make_bot`).
            deps: Зависимости handler'ов (со ссылкой на готовый
                :class:`PipelineScheduler`).

        Returns:
            Готовый :class:`TelegramBotRunner` — ещё не запущенный.
        """
        dispatcher = Dispatcher()
        dispatcher.include_router(build_router(deps))
        return cls(bot=bot, dispatcher=dispatcher, deps=deps)

    @property
    def bot(self) -> Bot:
        """Доступ к ``aiogram.Bot`` — нужен ``TelegramNotifier``."""
        return self._bot

    @property
    def dispatcher(self) -> Dispatcher:
        """Доступ к ``aiogram.Dispatcher`` (полезно в тестах)."""
        return self._dispatcher

    async def __aenter__(self) -> "TelegramBotRunner":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.shutdown()

    async def start_polling(self) -> None:
        """Запустить long-poll: блокируется до получения ``stop()``/Cancel."""
        me = await self._bot.get_me()
        self._log.info(
            "Telegram bot polling started: bot=@{username} (id={uid})",
            username=me.username,
            uid=me.id,
        )
        try:
            await self._dispatcher.start_polling(
                self._bot,
                handle_signals=False,  # сигналы ловит app/main.py
                allowed_updates=["message"],
                drop_pending_updates=_DROP_PENDING_ON_STARTUP,
            )
        finally:
            self._log.info("Telegram bot polling stopped")

    async def shutdown(self) -> None:
        """Корректно остановить polling и закрыть HTTP-сессию."""
        try:
            await self._dispatcher.stop_polling()
        except (RuntimeError, LookupError):
            # ``stop_polling`` бросает RuntimeError, если polling не запущен,
            # и LookupError — на некоторых версиях aiogram, когда стоп
            # вызвали дважды. Оба варианта безвредны.
            pass
        try:
            await self._bot.session.close()
        except Exception as exc:  # noqa: BLE001
            self._log.warning(
                "Failed to close bot HTTP session: {err}",
                err=f"{type(exc).__name__}: {exc}",
            )


__all__ = ["TelegramBotRunner"]
