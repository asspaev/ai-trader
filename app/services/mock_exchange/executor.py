"""Атомарное исполнение решения TRADER-агента на mock-бирже.

Принимает уже сохранённую запись :class:`Decision` (executed=None),
актуальные :class:`SymbolFilters` и :class:`BookTicker`, применяет
формулы из :mod:`app.services.mock_exchange.fees`, проверяет фильтры
биржи (LOT_SIZE, MIN_NOTIONAL) и в одной БД-транзакции:

* создаёт :class:`Transaction`,
* обновляет балансы в :class:`Wallet`,
* проставляет ``decision.executed`` и ``not_executed_reason``.

Commit делает вызывающая сторона (pipeline) — здесь только ``flush``.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.crud import decision as decision_crud
from app.crud import transaction as transaction_crud
from app.crud import wallet as wallet_crud
from app.models import Decision, Transaction
from app.models.enums import DecisionAction, TransactionAction
from app.services.binance.exchange_info import SymbolFilters
from app.services.binance.prices import BookTicker
from app.services.mock_exchange.fees import quote_buy, quote_sell


class NotExecutedReason(StrEnum):
    """Стандартизированные причины неисполнения сделки."""

    MIN_NOTIONAL = "MIN_NOTIONAL"
    LOT_SIZE = "LOT_SIZE"
    EMPTY_POSITION = "EMPTY_POSITION"
    INSUFFICIENT_FUNDS = "INSUFFICIENT_FUNDS"
    INVALID_FRACTION = "INVALID_FRACTION"


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    """Итог попытки исполнить решение.

    Attributes:
        executed: ``True`` — сделка проведена (BUY/SELL) либо это HOLD;
            ``False`` — биржевые фильтры или состояние кошелька не
            позволили исполнить.
        not_executed_reason: Код причины (см. :class:`NotExecutedReason`),
            заполнен только при ``executed=False``.
        transaction: Созданная запись о сделке (только для BUY/SELL,
            прошедших фильтры). Для HOLD — ``None``.
    """

    executed: bool
    not_executed_reason: str | None
    transaction: Transaction | None


async def execute_decision(
    session: AsyncSession,
    *,
    decision: Decision,
    symbol: str,
    quote_asset: str,
    filters: SymbolFilters,
    book_ticker: BookTicker,
    fee_rate: Decimal,
) -> ExecutionResult:
    """Исполнить решение в текущей сессии (без commit).

    Args:
        session: Открытая async-сессия SQLAlchemy.
        decision: Уже сохранённая запись решения (executed=None).
        symbol: Полный тикер пары (``BTCUSDT``).
        quote_asset: Котировочный актив (``USDT``).
        filters: Биржевые фильтры по символу (lot, min_notional, …).
        book_ticker: Текущие bid/ask из ``bookTicker``.
        fee_rate: Доля комиссии taker (``0.001`` = 0.10%).

    Returns:
        :class:`ExecutionResult`.
    """
    bound = logger.bind(
        component="mock_exchange.executor",
        decision_id=decision.id,
        asset=decision.asset,
        action=decision.action.value,
    )

    if decision.action is DecisionAction.HOLD:
        await decision_crud.mark_executed(
            session, decision_id=decision.id, executed=True
        )
        bound.info("HOLD decision marked as executed (no trade)")
        return ExecutionResult(executed=True, not_executed_reason=None, transaction=None)

    if decision.action is DecisionAction.BUY:
        return await _execute_buy(
            session,
            decision=decision,
            symbol=symbol,
            quote_asset=quote_asset,
            filters=filters,
            book_ticker=book_ticker,
            fee_rate=fee_rate,
            bound=bound,
        )

    if decision.action is DecisionAction.SELL:
        return await _execute_sell(
            session,
            decision=decision,
            symbol=symbol,
            quote_asset=quote_asset,
            filters=filters,
            book_ticker=book_ticker,
            fee_rate=fee_rate,
            bound=bound,
        )

    raise ValueError(f"Unsupported decision action: {decision.action!r}")


async def _execute_buy(
    session: AsyncSession,
    *,
    decision: Decision,
    symbol: str,
    quote_asset: str,
    filters: SymbolFilters,
    book_ticker: BookTicker,
    fee_rate: Decimal,
    bound,
) -> ExecutionResult:
    fraction = decision.buy_fraction
    if fraction is None or fraction <= 0 or fraction > 1:
        return await _mark_not_executed(
            session, decision, NotExecutedReason.INVALID_FRACTION, bound
        )

    usdt_wallet = await wallet_crud.get(
        session, user_id=decision.user_id, asset=quote_asset
    )
    free_usdt = usdt_wallet.balance if usdt_wallet else Decimal("0")
    if free_usdt <= 0:
        return await _mark_not_executed(
            session, decision, NotExecutedReason.INSUFFICIENT_FUNDS, bound
        )

    quote = quote_buy(
        free_usdt=free_usdt,
        fraction=fraction,
        ask_price=book_ticker.ask_price,
        fee_rate=fee_rate,
    )

    amount_quantized = filters.quantize_amount(quote.amount_crypto)
    if amount_quantized <= 0:
        return await _mark_not_executed(
            session, decision, NotExecutedReason.LOT_SIZE, bound
        )

    # Пересчёт после квантования — реальная gross может оказаться меньше.
    gross = amount_quantized * book_ticker.ask_price
    fee = gross * fee_rate
    spend = gross + fee

    if gross < filters.min_notional:
        return await _mark_not_executed(
            session, decision, NotExecutedReason.MIN_NOTIONAL, bound
        )
    if spend > free_usdt:
        return await _mark_not_executed(
            session, decision, NotExecutedReason.INSUFFICIENT_FUNDS, bound
        )

    new_usdt_balance = free_usdt - spend
    asset_wallet = await wallet_crud.get(
        session, user_id=decision.user_id, asset=decision.asset
    )
    current_asset_balance = asset_wallet.balance if asset_wallet else Decimal("0")
    new_asset_balance = current_asset_balance + amount_quantized

    await wallet_crud.upsert(
        session,
        user_id=decision.user_id,
        asset=quote_asset,
        balance=new_usdt_balance,
    )
    await wallet_crud.upsert(
        session,
        user_id=decision.user_id,
        asset=decision.asset,
        balance=new_asset_balance,
    )

    tx = await transaction_crud.create(
        session,
        user_id=decision.user_id,
        decision_id=decision.id,
        symbol=symbol,
        asset=decision.asset,
        action=TransactionAction.BUY,
        amount_crypto=amount_quantized,
        price_usdt=book_ticker.ask_price,
        gross_usdt=gross,
        fee_usdt=fee,
        net_usdt=spend,
        usdt_balance_after=new_usdt_balance,
        asset_balance_after=new_asset_balance,
    )
    await decision_crud.mark_executed(
        session, decision_id=decision.id, executed=True
    )

    bound.info(
        "BUY executed: amount={amount}, price={price}, spend={spend}",
        amount=str(amount_quantized),
        price=str(book_ticker.ask_price),
        spend=str(spend),
    )
    return ExecutionResult(executed=True, not_executed_reason=None, transaction=tx)


async def _execute_sell(
    session: AsyncSession,
    *,
    decision: Decision,
    symbol: str,
    quote_asset: str,
    filters: SymbolFilters,
    book_ticker: BookTicker,
    fee_rate: Decimal,
    bound,
) -> ExecutionResult:
    asset_wallet = await wallet_crud.get(
        session, user_id=decision.user_id, asset=decision.asset
    )
    current_asset_balance = asset_wallet.balance if asset_wallet else Decimal("0")
    if current_asset_balance <= 0:
        return await _mark_not_executed(
            session, decision, NotExecutedReason.EMPTY_POSITION, bound
        )

    amount_quantized = filters.quantize_amount(current_asset_balance)
    if amount_quantized <= 0:
        return await _mark_not_executed(
            session, decision, NotExecutedReason.LOT_SIZE, bound
        )

    quote = quote_sell(
        amount_crypto=amount_quantized,
        bid_price=book_ticker.bid_price,
        fee_rate=fee_rate,
    )

    if quote.gross_usdt < filters.min_notional:
        return await _mark_not_executed(
            session, decision, NotExecutedReason.MIN_NOTIONAL, bound
        )

    usdt_wallet = await wallet_crud.get(
        session, user_id=decision.user_id, asset=quote_asset
    )
    current_usdt_balance = usdt_wallet.balance if usdt_wallet else Decimal("0")
    new_usdt_balance = current_usdt_balance + quote.net_usdt
    new_asset_balance = current_asset_balance - amount_quantized

    await wallet_crud.upsert(
        session,
        user_id=decision.user_id,
        asset=quote_asset,
        balance=new_usdt_balance,
    )
    await wallet_crud.upsert(
        session,
        user_id=decision.user_id,
        asset=decision.asset,
        balance=new_asset_balance,
    )

    tx = await transaction_crud.create(
        session,
        user_id=decision.user_id,
        decision_id=decision.id,
        symbol=symbol,
        asset=decision.asset,
        action=TransactionAction.SELL,
        amount_crypto=amount_quantized,
        price_usdt=book_ticker.bid_price,
        gross_usdt=quote.gross_usdt,
        fee_usdt=quote.fee_usdt,
        net_usdt=quote.net_usdt,
        usdt_balance_after=new_usdt_balance,
        asset_balance_after=new_asset_balance,
    )
    await decision_crud.mark_executed(
        session, decision_id=decision.id, executed=True
    )

    bound.info(
        "SELL executed: amount={amount}, price={price}, net={net}",
        amount=str(amount_quantized),
        price=str(book_ticker.bid_price),
        net=str(quote.net_usdt),
    )
    return ExecutionResult(executed=True, not_executed_reason=None, transaction=tx)


async def _mark_not_executed(
    session: AsyncSession,
    decision: Decision,
    reason: NotExecutedReason,
    bound,
) -> ExecutionResult:
    """Проставить ``executed=False`` + причину; ничего больше не пишем."""
    await decision_crud.mark_executed(
        session,
        decision_id=decision.id,
        executed=False,
        not_executed_reason=reason.value,
    )
    bound.warning("Decision not executed: reason={reason}", reason=reason.value)
    return ExecutionResult(
        executed=False, not_executed_reason=reason.value, transaction=None
    )


__all__ = ["ExecutionResult", "NotExecutedReason", "execute_decision"]
