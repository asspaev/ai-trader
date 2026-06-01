"""Команды Telegram-бота (фаза 9 + 10).

Команды (только для авторизованного ``telegram_id`` из таблицы
``users``):

* ``/start`` — приветствие, подтверждение авторизации.
* ``/balance`` — балансы кошельков + общая стоимость портфеля в USDT и RUB.
* ``/history [N]`` — последние N (default 10, max настраивается через
  ``TELEGRAM_HISTORY_LIMIT_MAX``) фактических сделок.
* ``/stats`` — счётчики решений и сделок, текущая стоимость портфеля,
  PnL и ``delta_vs_hold_pct`` (см. ``services/metrics/pnl.py``).
* ``/start_pipeline`` — форс-запуск тика вне расписания (через
  :meth:`PipelineScheduler.trigger_now`).
* ``/stop`` / ``/resume`` — переключение флага ``scheduler_state.paused``.

Авторизация реализована :class:`AuthMiddleware`: неавторизованным
любым обращением отвечаем ``Not authorized`` и дальше handler не
зовём. Так у нас всего один источник истины «кто допущен», и handler'ы
никогда не получают чужой ``Message``.

Хендлеры — методы класса :class:`CommandHandlers` (а не модульные
функции с замыканием), чтобы зависимости были явными и легко
мокировались в тестах. Зарегистрировать в Router помогает
:func:`build_router`.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from html import escape as _html_escape
from typing import Any

from aiogram import BaseMiddleware, F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import Message, TelegramObject
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.crud import decision as decision_crud
from app.crud import transaction as transaction_crud
from app.models import Decision, Transaction
from app.models.enums import DecisionAction, TransactionAction
from app.services.binance.client import BinanceClient
from app.services.fx import FxRate, FxRateError, fetch_usdt_rub_rate
from app.services.metrics.pnl import (
    PnLReport,
    compute_pnl,
    format_pnl_lines,
)
from app.services.pipeline.scheduler import PipelineScheduler
from app.services.telegram.notifier import (
    PortfolioSnapshot,
    build_portfolio_snapshot,
    format_balance_message,
)


# ---------- Зависимости и middleware ----------


@dataclass(frozen=True, slots=True)
class HandlerDeps:
    """Контейнер зависимостей, прокидываемых в команды.

    Хранится снаружи aiogram-роутера и захватывается замыканием
    :func:`build_router`. Это делает зависимости явными (никаких
    глобальных ``settings``-обращений в handler'ах) и упрощает
    мокинг в тестах.
    """

    session_factory: async_sessionmaker[AsyncSession]
    scheduler: PipelineScheduler
    binance_client: BinanceClient
    user_id: int
    allowed_telegram_id: int
    quote_asset: str
    symbols: tuple[str, ...]
    history_limit_default: int
    history_limit_max: int


class AuthMiddleware(BaseMiddleware):
    """Outer middleware: пропускает только сообщения от ``allowed_telegram_id``.

    Любое сообщение от другого пользователя получает ``Not authorized``
    и до handler'ов не доходит. Пустые ``from_user`` (каналы, бизнес-
    события) — тоже отсекаем, в MVP-боте нечего им отвечать.
    """

    def __init__(self, allowed_telegram_id: int) -> None:
        self._allowed = allowed_telegram_id
        self._log = logger.bind(component="telegram.auth")

    async def __call__(self, handler, event: TelegramObject, data: dict[str, Any]):
        if isinstance(event, Message):
            uid = event.from_user.id if event.from_user else None
            if uid != self._allowed:
                self._log.info(
                    "Rejecting message from unauthorized telegram_id={uid}",
                    uid=uid,
                )
                if event.text and event.text.startswith("/"):
                    try:
                        await event.answer("Not authorized")
                    except Exception as exc:  # noqa: BLE001
                        self._log.warning(
                            "Failed to send Not authorized reply: {err}",
                            err=f"{type(exc).__name__}: {exc}",
                        )
                return None
        return await handler(event, data)


# ---------- Хендлеры ----------


class CommandHandlers:
    """Реализация бизнес-логики команд бота.

    Каждый метод — корутина, принимающая ``aiogram.types.Message``.
    Регистрация в роутере и фильтрация по команде живут в
    :func:`build_router` ниже.
    """

    def __init__(self, deps: HandlerDeps) -> None:
        self._deps = deps
        self._log = logger.bind(component="telegram.handlers")

    # ----- /start -----

    async def on_start(self, message: Message) -> None:
        """Приветствие + явное подтверждение авторизации."""
        await message.answer(
            "👋 <b>AI-Trader на связи</b>\n"
            f"{_SEPARATOR}\n"
            "<b>Доступные команды:</b>\n"
            "• /balance — портфель в USDT и RUB\n"
            "• /history [N] — последние сделки "
            f"<i>(по умолчанию {self._deps.history_limit_default})</i>\n"
            "• /stats — сводка по решениям\n"
            "• /start_pipeline — принудительно запустить тик\n"
            "• /stop — поставить планировщик на паузу\n"
            "• /resume — возобновить планировщик"
        )

    # ----- /balance -----

    async def on_balance(self, message: Message) -> None:
        """Баланс кошельков + RUB-эквивалент по текущему курсу."""
        portfolio = await self._build_portfolio()
        fx_rate = await self._fetch_fx_rate_or_none()
        text = format_balance_message(portfolio=portfolio, fx_rate=fx_rate)
        await message.answer(text)

    # ----- /history -----

    async def on_history(
        self, message: Message, command: CommandObject | None = None
    ) -> None:
        """Последние N сделок (по умолчанию ``history_limit_default``)."""
        limit = self._parse_history_limit(command.args if command else None)
        async with self._deps.session_factory() as session:
            txs = await transaction_crud.list_recent_for_user(
                session, user_id=self._deps.user_id, limit=limit
            )

        if not txs:
            await message.answer("📭 <i>История сделок пуста.</i>")
            return

        await message.answer(_format_history_message(txs, limit=limit))

    # ----- /stats -----

    async def on_stats(self, message: Message) -> None:
        """Сводка: счётчики решений + сделок + портфель + PnL.

        PnL и ``delta_vs_hold_pct`` (фаза 10) считаются по запросу.
        Если посчитать не удалось (например, Binance не отдал
        исторические цены), строка про PnL опускается — остальная
        статистика приходит к пользователю в любом случае.
        """
        async with self._deps.session_factory() as session:
            decisions = await decision_crud.list_all_for_user(
                session, user_id=self._deps.user_id
            )
            transactions = await transaction_crud.list_all_for_user(
                session, user_id=self._deps.user_id
            )

        portfolio = await self._build_portfolio()
        fx_rate = await self._fetch_fx_rate_or_none()
        pnl_report = await self._compute_pnl_or_none(portfolio)
        text = _format_stats_message(
            decisions=decisions,
            transactions=transactions,
            portfolio=portfolio,
            fx_rate=fx_rate,
            pnl_report=pnl_report,
        )
        await message.answer(text)

    # ----- /start_pipeline -----

    async def on_start_pipeline(self, message: Message) -> None:
        """Принудительно запустить один тик pipeline.

        Сама задача исполняется в background: один тик может идти
        минутами (особенно из-за LLM-вызовов), а ответ боту нужно
        вернуть быстро. О результатах пользователь узнает из
        ``notify_step`` / ``notify_pipeline_summary``.
        """
        await message.answer(
            "🚀 <b>Запускаю pipeline вне расписания…</b>"
        )

        async def _run() -> None:
            try:
                await self._deps.scheduler.trigger_now()
            except Exception as exc:  # noqa: BLE001
                self._log.exception("Forced pipeline run failed")
                try:
                    await message.answer(
                        "⚠️ <b>Pipeline упал:</b> "
                        f"<code>{_esc(type(exc).__name__)}: {_esc(exc)}</code>"
                    )
                except Exception:  # noqa: BLE001
                    pass

        asyncio.create_task(_run(), name="manual-pipeline-trigger")

    # ----- /stop -----

    async def on_stop(self, message: Message) -> None:
        await self._deps.scheduler.pause()
        await message.answer(
            "⏸ <b>Pipeline на паузе.</b>\n"
            "<i>Используйте /resume чтобы возобновить.</i>"
        )

    # ----- /resume -----

    async def on_resume(self, message: Message) -> None:
        await self._deps.scheduler.resume()
        await message.answer("▶️ <b>Pipeline возобновлён.</b>")

    # ----- внутреннее -----

    def _parse_history_limit(self, raw: str | None) -> int:
        """Распарсить ``N`` в ``/history N``: bounded в [1, max]."""
        if not raw:
            return self._deps.history_limit_default
        first_token = raw.strip().split()[0] if raw.strip() else ""
        try:
            value = int(first_token)
        except ValueError:
            return self._deps.history_limit_default
        if value <= 0:
            return self._deps.history_limit_default
        return min(value, self._deps.history_limit_max)

    async def _build_portfolio(self) -> PortfolioSnapshot:
        return await build_portfolio_snapshot(
            session_factory=self._deps.session_factory,
            binance_client=self._deps.binance_client,
            user_id=self._deps.user_id,
            quote_asset=self._deps.quote_asset,
            symbols=self._deps.symbols,
        )

    async def _fetch_fx_rate_or_none(self) -> Decimal | None:
        """Получить курс USDT/RUB, либо ``None`` при ошибке.

        В Telegram-команде нет смысла падать, если внешний сервис
        моргнул — пользователю всё равно покажем USDT-часть.
        """
        try:
            rate: FxRate = await fetch_usdt_rub_rate(self._deps.binance_client)
            return rate.rate
        except FxRateError as exc:
            self._log.warning(
                "Failed to fetch USDT/RUB rate for command: {err}", err=str(exc)
            )
        except Exception as exc:  # noqa: BLE001
            self._log.warning(
                "Unexpected error while fetching USDT/RUB rate: {err}",
                err=f"{type(exc).__name__}: {exc}",
            )
        return None

    async def _compute_pnl_or_none(
        self, portfolio: PortfolioSnapshot
    ) -> PnLReport | None:
        """Посчитать PnL или вернуть ``None`` при ошибке.

        В команде ``/stats`` падать из-за метрики не хочется: остальная
        сводка (решения, сделки, портфель) и так информативна.
        """
        try:
            return await compute_pnl(
                session_factory=self._deps.session_factory,
                binance_client=self._deps.binance_client,
                user_id=self._deps.user_id,
                quote_asset=self._deps.quote_asset,
                symbols=self._deps.symbols,
                portfolio=portfolio,
            )
        except Exception as exc:  # noqa: BLE001
            self._log.warning(
                "Failed to compute PnL for /stats: {err}",
                err=f"{type(exc).__name__}: {exc}",
            )
            return None


# ---------- Router builder ----------


def build_router(deps: HandlerDeps) -> Router:
    """Собрать aiogram-роутер со всеми командами и middleware.

    Middleware вешается на роутер — это даёт более узкую область
    действия, чем outer middleware на dispatcher (полезно, если в
    будущем добавим публичные команды).
    """
    router = Router(name="commands")
    router.message.outer_middleware(AuthMiddleware(deps.allowed_telegram_id))

    handlers = CommandHandlers(deps)

    router.message.register(handlers.on_start, CommandStart())
    router.message.register(handlers.on_balance, Command("balance"))
    router.message.register(handlers.on_history, Command("history"))
    router.message.register(handlers.on_stats, Command("stats"))
    router.message.register(handlers.on_start_pipeline, Command("start_pipeline"))
    router.message.register(handlers.on_stop, Command("stop"))
    router.message.register(handlers.on_resume, Command("resume"))

    # Все прочие сообщения от авторизованного пользователя — мягко игнорим.
    @router.message(F.text)
    async def _fallback(message: Message) -> None:  # pragma: no cover — тривиально
        await message.answer(
            "🤔 <i>Не понял команду.</i>\n"
            "<b>Доступно:</b> /balance, /history, /stats, "
            "/start_pipeline, /stop, /resume."
        )

    return router


# ---------- helpers (форматирование сообщений) ----------

_SEPARATOR = "━━━━━━━━━━━━━━━"


def _esc(value: object) -> str:
    """Экранировать произвольный текст для HTML parse mode Telegram.

    Безопасно вызывать на любом значении — числа, ``None`` и Enum'ы
    приводятся к строке, у строк экранируются ``<`` / ``>`` / ``&``.
    """
    if value is None:
        return ""
    return _html_escape(str(value), quote=True)


def _format_history_message(
    transactions: list[Transaction], *, limit: int
) -> str:
    """Текст ответа на ``/history`` (HTML)."""
    lines: list[str] = [
        f"📜 <b>Последние {len(transactions)} сделок</b> "
        f"<i>(≤ {limit})</i>",
        _SEPARATOR,
    ]
    for tx in transactions:
        emoji = "🟢" if tx.action is TransactionAction.BUY else "🔴"
        verb = "куплено" if tx.action is TransactionAction.BUY else "продано"
        lines.append(
            f"{emoji} <code>{_esc(_fmt_timestamp(tx.created_at))}</code> — "
            f"<b>{_esc(tx.asset)}</b>"
        )
        lines.append(
            f"    {verb} <code>{_fmt_qty(tx.amount_crypto)}</code> "
            f"по <code>{_fmt_price(tx.price_usdt)}</code> USDT"
        )
        lines.append(
            f"    <i>нетто <code>{_fmt_money(tx.net_usdt)}</code> USDT, "
            f"комиссия <code>{_fmt_money(tx.fee_usdt)}</code> USDT</i>"
        )
    return "\n".join(lines)


def _format_stats_message(
    *,
    decisions: list[Decision],
    transactions: list[Transaction],
    portfolio: PortfolioSnapshot,
    fx_rate: Decimal | None,
    pnl_report: PnLReport | None,
) -> str:
    """Текст ответа на ``/stats`` (HTML)."""
    counts: dict[DecisionAction, int] = {a: 0 for a in DecisionAction}
    executed = 0
    skipped = 0
    for d in decisions:
        counts[d.action] = counts.get(d.action, 0) + 1
        if d.executed is True:
            executed += 1
        elif d.executed is False:
            skipped += 1

    buys = sum(1 for tx in transactions if tx.action is TransactionAction.BUY)
    sells = sum(1 for tx in transactions if tx.action is TransactionAction.SELL)
    fees = sum((tx.fee_usdt for tx in transactions), Decimal("0"))

    rub_value = portfolio.total_rub(fx_rate)
    rub_part = (
        f" <i>(≈ <code>{_fmt_rub(rub_value)}</code> RUB)</i>"
        if rub_value is not None
        else ""
    )

    lines = [
        "📊 <b>Статистика</b>",
        _SEPARATOR,
        f"🧠 Решений всего: <b>{len(decisions)}</b>",
        f"    BUY×<b>{counts[DecisionAction.BUY]}</b> · "
        f"SELL×<b>{counts[DecisionAction.SELL]}</b> · "
        f"HOLD×<b>{counts[DecisionAction.HOLD]}</b>",
        f"    <i>исполнено: {executed}, пропущено: {skipped}</i>",
        f"💱 Сделок: <b>{len(transactions)}</b> "
        f"(BUY×<b>{buys}</b> · SELL×<b>{sells}</b>)",
        f"    <i>комиссии всего: <code>{_fmt_money(fees)}</code> USDT</i>",
        _SEPARATOR,
        f"💼 Портфель: <b><code>{_fmt_money(portfolio.total_usdt)}</code> "
        f"USDT</b>{rub_part}",
    ]
    if pnl_report is not None:
        lines.append(_SEPARATOR)
        for pnl_line in format_pnl_lines(pnl_report):
            lines.append(f"📈 <b>{_esc(pnl_line)}</b>")
    return "\n".join(lines)


# Тонкие обёртки над форматтерами из notifier — чтобы не плодить
# зависимости (модуль notifier уже импортирован).


def _fmt_money(value: Decimal) -> str:
    return f"{value:.2f}"


def _fmt_rub(value: Decimal) -> str:
    rounded = int(value.quantize(Decimal("1")))
    return f"{rounded:,}".replace(",", " ")


def _fmt_qty(value: Decimal) -> str:
    text = f"{value:.8f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _fmt_price(value: Decimal) -> str:
    if value >= Decimal("1"):
        return f"{value:.2f}"
    if value >= Decimal("0.01"):
        return f"{value:.4f}"
    text = f"{value:.8f}".rstrip("0").rstrip(".")
    return text or "0"


def _fmt_timestamp(value: datetime) -> str:
    """UTC-таймштамп в виде ``2026-06-01 12:34Z``."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    else:
        value = value.astimezone(timezone.utc)
    return value.strftime("%Y-%m-%d %H:%MZ")


__all__ = [
    "AuthMiddleware",
    "CommandHandlers",
    "HandlerDeps",
    "build_router",
]
