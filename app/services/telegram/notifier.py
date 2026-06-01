"""Telegram-нотификатор pipeline (фаза 9).

Содержит:

* :class:`TelegramNotifier` — рабочая реализация
  :class:`PipelineNotifier` поверх ``aiogram.Bot``.
* Чистые форматтеры :func:`format_step_message` и
  :func:`format_summary_message`, которые не зависят от ``aiogram`` и
  тестируются обычными ассертами на строки.
* :func:`build_portfolio_snapshot` — считает текущую стоимость
  портфеля (USDT + актив × bid). Используется в summary-сообщении и
  в команде ``/balance``.

Сам Protocol :class:`PipelineNotifier` и :class:`NoOpNotifier` живут в
``app/services/pipeline/notifier.py`` — иначе pipeline-runner получил
бы зависимость от Telegram-пакета (а через него — от ``aiogram``).

Все сообщения шлются plain-text (без ``parse_mode``): обоснование
TRADER-агента — произвольный пользовательский текст, экранировать
Markdown/HTML на каждый чих не хочется, а эмодзи прекрасно работают и
так. Падения отправки сообщений никогда не валят pipeline — все ошибки
``aiogram`` логируются и подавляются.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.crud import wallet as wallet_crud
from app.models import Wallet
from app.models.enums import DecisionAction, TransactionAction
from app.services.binance.client import BinanceClient
from app.services.binance.prices import fetch_book_ticker
from app.services.fx import FxRate, FxRateError, fetch_usdt_rub_rate
from app.services.pipeline.crypto_step import CryptoStepResult
from app.services.pipeline.notifier import NoOpNotifier, PipelineNotifier
from app.services.pipeline.runner import PipelineRunResult

if TYPE_CHECKING:
    # Импорт под TYPE_CHECKING: модуль ``metrics.pnl`` сам импортирует
    # ``PortfolioSnapshot`` из этого файла — runtime-импорт сюда
    # породил бы цикл. PnLReport нужен только в аннотациях, поэтому
    # этого достаточно.
    from app.services.metrics.pnl import PnLReport


_ACTION_EMOJI: dict[DecisionAction, str] = {
    DecisionAction.BUY: "📈",
    DecisionAction.SELL: "📉",
    DecisionAction.HOLD: "⏸",
}


# ---------- Portfolio snapshot ----------


@dataclass(frozen=True, slots=True)
class AssetValue:
    """Стоимость одного актива в портфеле.

    Attributes:
        asset: Тикер актива (``USDT`` для котировочного).
        balance: Баланс актива в его единицах.
        bid_price: Bid-цена в USDT (``None`` для самого USDT).
        value_usdt: Оценочная стоимость в USDT (``balance * bid_price``
            для крипты, ``balance`` для USDT).
    """

    asset: str
    balance: Decimal
    bid_price: Decimal | None
    value_usdt: Decimal


@dataclass(frozen=True, slots=True)
class PortfolioSnapshot:
    """Снимок портфеля для summary-сообщения и команды ``/balance``."""

    items: tuple[AssetValue, ...]
    total_usdt: Decimal

    def total_rub(self, fx_rate: Decimal | None) -> Decimal | None:
        """Перевести общий USDT в RUB по заданному курсу.

        Возвращает ``None``, если курс недоступен (FxRateError при
        получении котировки).
        """
        if fx_rate is None or fx_rate <= 0:
            return None
        return self.total_usdt * fx_rate


async def build_portfolio_snapshot(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    binance_client: BinanceClient,
    user_id: int,
    quote_asset: str,
    symbols: Sequence[str],
) -> PortfolioSnapshot:
    """Сложить балансы кошельков и оценить крипту по bid-цене.

    Args:
        session_factory: Фабрика async-сессий БД.
        binance_client: Клиент Binance public API.
        user_id: ID пользователя.
        quote_asset: Котировочный актив (``USDT``).
        symbols: Активы, для которых дёргаем bid-цены. Кошельки за
            пределами этого списка попадают в снапшот «как есть» с
            ``bid_price=None`` и ``value_usdt=0`` — чтобы не падать,
            если в БД остался хвост от тестов или старых конфигов.

    Returns:
        :class:`PortfolioSnapshot` с упорядоченным списком активов
        (USDT первым, далее в порядке появления в кошельках).
    """
    async with session_factory() as session:
        wallets = await wallet_crud.list_for_user(session, user_id=user_id)

    by_asset: dict[str, Wallet] = {w.asset.upper(): w for w in wallets}
    quote_upper = quote_asset.upper()
    items: list[AssetValue] = []

    usdt_wallet = by_asset.get(quote_upper)
    usdt_balance = usdt_wallet.balance if usdt_wallet else Decimal("0")
    items.append(
        AssetValue(
            asset=quote_upper,
            balance=usdt_balance,
            bid_price=None,
            value_usdt=usdt_balance,
        )
    )

    seen = {quote_upper}
    for raw_asset in symbols:
        asset = raw_asset.upper()
        if asset in seen:
            continue
        seen.add(asset)
        wallet = by_asset.get(asset)
        balance = wallet.balance if wallet else Decimal("0")
        bid_price: Decimal | None = None
        value_usdt = Decimal("0")
        if balance > 0:
            try:
                ticker = await fetch_book_ticker(
                    binance_client, f"{asset}{quote_upper}"
                )
                bid_price = ticker.bid_price
                value_usdt = balance * bid_price
            except Exception as exc:  # noqa: BLE001 — изоляция отказа Binance
                logger.bind(component="telegram.notifier", asset=asset).warning(
                    "Failed to fetch bid price for portfolio snapshot: {err}",
                    err=f"{type(exc).__name__}: {exc}",
                )
        items.append(
            AssetValue(
                asset=asset,
                balance=balance,
                bid_price=bid_price,
                value_usdt=value_usdt,
            )
        )

    for asset, wallet in by_asset.items():
        if asset in seen:
            continue
        items.append(
            AssetValue(
                asset=asset,
                balance=wallet.balance,
                bid_price=None,
                value_usdt=Decimal("0"),
            )
        )

    total = sum((it.value_usdt for it in items), Decimal("0"))
    return PortfolioSnapshot(items=tuple(items), total_usdt=total)


# ---------- Форматтеры (pure) ----------


def format_step_message(result: CryptoStepResult) -> str:
    """Собрать plain-text сообщение по итогу одной монеты.

    Поддерживаются три ветки:

    * Pipeline-failure (``failure_reason`` есть, ``execution is None``)
      — выводим короткое уведомление об ошибке шага.
    * Биржевой отказ (``execution.executed is False``) — выводим
      причину неисполнения и обоснование TRADER.
    * Успех (``execution.executed is True``) — детальный отчёт по
      HOLD/BUY/SELL с балансами «было → стало».
    """
    asset = result.asset.upper()
    lines: list[str] = [f"🪙 {asset}"]

    if result.execution is None:
        lines.append(
            f"⚠️ Шаг не завершён: {result.failure_reason or 'UNKNOWN'}"
        )
        if result.error_text:
            lines.append(result.error_text)
        lines.append(f"Длительность: {_format_duration(result.duration_seconds)}")
        return "\n".join(lines)

    decision = result.decision
    emoji = _ACTION_EMOJI.get(decision.action, "•")
    action_text = _format_decision_headline(decision.action, decision.buy_fraction)
    lines.append(f"Решение: {emoji} {action_text}")

    if not result.execution.executed:
        reason = result.execution.not_executed_reason or "UNKNOWN"
        lines.append(f"Не исполнено: {reason}")
        _append_reasoning(lines, decision.reasoning)
        return "\n".join(lines)

    tx = result.execution.transaction
    if tx is not None:
        side = "ask" if tx.action is TransactionAction.BUY else "bid"
        lines.append(f"Цена: {_fmt_price(tx.price_usdt)} USDT ({side})")
        verb = "Куплено" if tx.action is TransactionAction.BUY else "Продано"
        lines.append(
            f"{verb}: {_fmt_qty(tx.amount_crypto)} {asset} "
            f"за {_fmt_money(tx.gross_usdt)} USDT "
            f"(комиссия {_fmt_money(tx.fee_usdt)} USDT)"
        )
        usdt_before = _balance_before(tx)
        lines.append(
            f"Баланс USDT: {_fmt_money(usdt_before)} → "
            f"{_fmt_money(tx.usdt_balance_after)}"
        )

    _append_reasoning(lines, decision.reasoning)
    return "\n".join(lines)


def format_summary_message(
    run: PipelineRunResult,
    *,
    portfolio: PortfolioSnapshot | None,
    fx_rate: Decimal | None,
    pnl_report: "PnLReport | None" = None,
) -> str:
    """Собрать plain-text итоговое сообщение по тику.

    PnL и ``delta_vs_hold_pct`` (фаза 10, см. ``architecture.md`` §13)
    добавляются отдельной строкой в формате
    ``PnL: +24.55 USDT (+2.46%) | vs HOLD: +0.85%``. Если
    ``pnl_report`` не передан — строка PnL не выводится.
    """
    # Поздний импорт, чтобы избежать цикла telegram.notifier ↔
    # metrics.pnl (pnl.py импортирует PortfolioSnapshot отсюда).
    from app.services.metrics.pnl import format_pnl_inline

    counts = _count_decisions(run)
    lines: list[str] = [
        f"🔁 Pipeline #{str(run.pipeline_run_id)[:8]} завершён "
        f"за {_format_duration(run.duration_seconds)}",
        "Решений: "
        + ", ".join(
            f"{action.value}×{counts[action]}"
            for action in (DecisionAction.BUY, DecisionAction.SELL, DecisionAction.HOLD)
        ),
    ]
    skipped = sum(
        1
        for step in run.steps
        if step.failure_reason is not None
    )
    if skipped:
        lines.append(f"С ошибками: {skipped} из {len(run.steps)}")

    if portfolio is not None:
        rub_value = portfolio.total_rub(fx_rate)
        rub_part = (
            f" (≈ {_fmt_rub(rub_value)} RUB)" if rub_value is not None else ""
        )
        lines.append(
            f"Портфель: {_fmt_money(portfolio.total_usdt)} USDT{rub_part}"
        )

    if pnl_report is not None:
        lines.append(format_pnl_inline(pnl_report))

    return "\n".join(lines)


def format_balance_message(
    *,
    portfolio: PortfolioSnapshot,
    fx_rate: Decimal | None,
) -> str:
    """Текст ответа на команду ``/balance`` (см. handlers.py)."""
    lines: list[str] = ["💼 Баланс"]
    for item in portfolio.items:
        if item.asset == "USDT":
            lines.append(f"• {item.asset}: {_fmt_money(item.balance)}")
            continue
        if item.bid_price is None or item.balance <= 0:
            lines.append(f"• {item.asset}: {_fmt_qty(item.balance)}")
        else:
            lines.append(
                f"• {item.asset}: {_fmt_qty(item.balance)} "
                f"(≈ {_fmt_money(item.value_usdt)} USDT "
                f"по bid {_fmt_price(item.bid_price)})"
            )

    rub_value = portfolio.total_rub(fx_rate)
    rub_part = f" (≈ {_fmt_rub(rub_value)} RUB)" if rub_value is not None else ""
    lines.append(f"Всего: {_fmt_money(portfolio.total_usdt)} USDT{rub_part}")
    return "\n".join(lines)


# ---------- TelegramNotifier ----------


class TelegramNotifier:
    """Реальный нотификатор поверх ``aiogram.Bot``.

    Не подвергает pipeline риску: любая ошибка отправки сообщения
    логируется и подавляется. В Phase 9 формат — plain-text без
    ``parse_mode``: эмодзи UTF-8 работают и так, а Markdown-эскейп
    произвольного текста от LLM был бы лишней точкой отказа.
    """

    def __init__(
        self,
        *,
        bot,  # aiogram.Bot — без аннотации, чтобы модуль импортировался без aiogram
        chat_id: int,
        session_factory: async_sessionmaker[AsyncSession],
        binance_client: BinanceClient,
        user_id: int,
        quote_asset: str,
        symbols: Sequence[str],
    ) -> None:
        self._bot = bot
        self._chat_id = chat_id
        self._session_factory = session_factory
        self._binance_client = binance_client
        self._user_id = user_id
        self._quote_asset = quote_asset
        self._symbols = tuple(s.upper() for s in symbols)
        self._log = logger.bind(component="telegram.notifier", chat_id=chat_id)

    async def notify_step(self, result: CryptoStepResult) -> None:
        """Отправить уведомление о результате одной монеты."""
        text = format_step_message(result)
        await self._safe_send(text, context="step")

    async def notify_pipeline_summary(self, run: PipelineRunResult) -> None:
        """Отправить итоговое сообщение тика (портфель + PnL + сводка)."""
        # Поздний импорт во избежание цикла telegram.notifier ↔ metrics.pnl.
        from app.services.metrics.pnl import PnLReport, compute_pnl

        portfolio: PortfolioSnapshot | None = None
        fx_rate: Decimal | None = None
        pnl_report: PnLReport | None = None
        try:
            portfolio = await build_portfolio_snapshot(
                session_factory=self._session_factory,
                binance_client=self._binance_client,
                user_id=self._user_id,
                quote_asset=self._quote_asset,
                symbols=self._symbols,
            )
        except Exception as exc:  # noqa: BLE001
            self._log.warning(
                "Failed to build portfolio snapshot for summary: {err}",
                err=f"{type(exc).__name__}: {exc}",
            )
        try:
            rate = await fetch_usdt_rub_rate(self._binance_client)
            fx_rate = rate.rate
        except FxRateError as exc:
            self._log.warning(
                "Failed to fetch USDT/RUB rate for summary: {err}", err=str(exc)
            )
        except Exception as exc:  # noqa: BLE001
            self._log.warning(
                "Unexpected error while fetching USDT/RUB rate: {err}",
                err=f"{type(exc).__name__}: {exc}",
            )

        if portfolio is not None:
            try:
                pnl_report = await compute_pnl(
                    session_factory=self._session_factory,
                    binance_client=self._binance_client,
                    user_id=self._user_id,
                    quote_asset=self._quote_asset,
                    symbols=self._symbols,
                    portfolio=portfolio,
                )
            except Exception as exc:  # noqa: BLE001
                self._log.warning(
                    "Failed to compute PnL for summary: {err}",
                    err=f"{type(exc).__name__}: {exc}",
                )

        text = format_summary_message(
            run, portfolio=portfolio, fx_rate=fx_rate, pnl_report=pnl_report
        )
        await self._safe_send(text, context="summary")

    async def _safe_send(self, text: str, *, context: str) -> None:
        """Отправить сообщение и подавить любую ошибку aiogram."""
        try:
            await self._bot.send_message(chat_id=self._chat_id, text=text)
        except Exception as exc:  # noqa: BLE001
            self._log.warning(
                "Failed to deliver {context} message: {err}",
                context=context,
                err=f"{type(exc).__name__}: {exc}",
            )


# ---------- helpers ----------


def _count_decisions(run: PipelineRunResult) -> dict[DecisionAction, int]:
    """Подсчитать BUY/SELL/HOLD в результате тика."""
    counts: dict[DecisionAction, int] = {
        DecisionAction.BUY: 0,
        DecisionAction.SELL: 0,
        DecisionAction.HOLD: 0,
    }
    for step in run.steps:
        action = step.decision.action
        counts[action] = counts.get(action, 0) + 1
    return counts


def _format_decision_headline(
    action: DecisionAction, buy_fraction: Decimal | None
) -> str:
    """Шапка решения — «BUY 25% свободного USDT» и т.п."""
    if action is DecisionAction.BUY:
        percent = (
            (buy_fraction * Decimal("100")).quantize(Decimal("1"))
            if buy_fraction is not None
            else Decimal("0")
        )
        return f"BUY {percent}% свободного USDT"
    if action is DecisionAction.SELL:
        return "SELL (вся позиция)"
    return "HOLD"


def _append_reasoning(lines: list[str], reasoning: str | None) -> None:
    """Дописать обоснование TRADER (если оно есть)."""
    if reasoning:
        lines.append(f"Обоснование: {reasoning}")


def _balance_before(tx) -> Decimal:
    """Вычислить USDT-баланс ДО сделки из ``usdt_balance_after``/``net_usdt``.

    Для BUY ``net_usdt`` — это сумма списания (gross + fee), поэтому
    ``before = after + net``. Для SELL ``net_usdt`` — это сумма
    поступления (gross − fee), поэтому ``before = after − net``.
    """
    if tx.action is TransactionAction.BUY:
        return tx.usdt_balance_after + tx.net_usdt
    return tx.usdt_balance_after - tx.net_usdt


def _format_duration(seconds: float) -> str:
    """«2 мин 14 сек» / «53 сек» — компактный вид длительности."""
    seconds_int = max(0, int(round(seconds)))
    if seconds_int < 60:
        return f"{seconds_int} сек"
    minutes, remainder = divmod(seconds_int, 60)
    if remainder == 0:
        return f"{minutes} мин"
    return f"{minutes} мин {remainder} сек"


def _fmt_money(value: Decimal) -> str:
    """2 знака после запятой — для USDT-сумм."""
    return f"{value:.2f}"


def _fmt_rub(value: Decimal) -> str:
    """RUB печатаем без копеек, с пробелом-разделителем тысяч."""
    rounded = int(value.quantize(Decimal("1")))
    return f"{rounded:,}".replace(",", " ")


def _fmt_qty(value: Decimal) -> str:
    """8 знаков после запятой, без хвостовых нулей."""
    text = f"{value:.8f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _fmt_price(value: Decimal) -> str:
    """Цена: 2 знака для крупных, до 8 для копеечных альтов."""
    if value >= Decimal("1"):
        return f"{value:.2f}"
    if value >= Decimal("0.01"):
        return f"{value:.4f}"
    text = f"{value:.8f}".rstrip("0").rstrip(".")
    return text or "0"


__all__ = [
    "AssetValue",
    "NoOpNotifier",
    "PipelineNotifier",
    "PortfolioSnapshot",
    "TelegramNotifier",
    "build_portfolio_snapshot",
    "format_balance_message",
    "format_step_message",
    "format_summary_message",
]
# ``NoOpNotifier`` и ``PipelineNotifier`` живут в pipeline-пакете, но
# реэкспортируем их через ``app.services.telegram`` для удобства
# импортов в ``app/main.py`` и тестах Telegram-слоя.
