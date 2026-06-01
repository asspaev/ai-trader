"""Тесты ``execute_decision``: BUY/SELL/HOLD + фильтры биржи + кошельки."""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest

from app.crud import decision as decision_crud
from app.crud import user as user_crud
from app.crud import wallet as wallet_crud
from app.models.enums import DecisionAction, TransactionAction
from app.services.binance.exchange_info import SymbolFilters
from app.services.binance.prices import BookTicker
from app.services.mock_exchange.executor import (
    NotExecutedReason,
    execute_decision,
)


pytestmark = pytest.mark.asyncio(loop_scope="session")


FEE = Decimal("0.001")
QUOTE = "USDT"
SYMBOL = "BTCUSDT"
ASSET = "BTC"


def _btc_filters(
    step: str = "0.00001",
    min_qty: str = "0.00001",
    min_notional: str = "10",
) -> SymbolFilters:
    return SymbolFilters(
        symbol=SYMBOL,
        base_asset=ASSET,
        quote_asset=QUOTE,
        step_size=Decimal(step),
        min_qty=Decimal(min_qty),
        tick_size=Decimal("0.01"),
        min_notional=Decimal(min_notional),
    )


def _ticker(bid: str = "67000", ask: str = "67100") -> BookTicker:
    return BookTicker(symbol=SYMBOL, bid_price=Decimal(bid), ask_price=Decimal(ask))


async def _seed_user_with_balances(
    session, *, usdt: str = "1000", btc: str = "0"
) -> int:
    user = await user_crud.create(
        session,
        telegram_id=42,
        username="trader",
        initial_capital_rub=Decimal("100000"),
        initial_capital_usdt=Decimal("1100"),
        initial_usdt_rub_rate=Decimal("90.9"),
    )
    await wallet_crud.upsert(session, user_id=user.id, asset=QUOTE, balance=Decimal(usdt))
    await wallet_crud.upsert(session, user_id=user.id, asset=ASSET, balance=Decimal(btc))
    return user.id


async def _make_decision(
    session,
    *,
    user_id: int,
    action: DecisionAction,
    buy_fraction: Decimal | None = None,
):
    return await decision_crud.create(
        session,
        user_id=user_id,
        pipeline_run_id=uuid.uuid4(),
        asset=ASSET,
        action=action,
        buy_fraction=buy_fraction,
    )


async def test_hold_marks_executed_without_transaction(session) -> None:
    user_id = await _seed_user_with_balances(session)
    decision = await _make_decision(session, user_id=user_id, action=DecisionAction.HOLD)

    result = await execute_decision(
        session,
        decision=decision,
        symbol=SYMBOL,
        quote_asset=QUOTE,
        filters=_btc_filters(),
        book_ticker=_ticker(),
        fee_rate=FEE,
    )

    assert result.executed is True
    assert result.transaction is None
    assert decision.executed is True


async def test_buy_creates_transaction_and_updates_wallets(session) -> None:
    user_id = await _seed_user_with_balances(session, usdt="1000")
    decision = await _make_decision(
        session,
        user_id=user_id,
        action=DecisionAction.BUY,
        buy_fraction=Decimal("0.5"),
    )

    result = await execute_decision(
        session,
        decision=decision,
        symbol=SYMBOL,
        quote_asset=QUOTE,
        filters=_btc_filters(),
        book_ticker=_ticker(bid="67000", ask="67100"),
        fee_rate=FEE,
    )

    assert result.executed is True
    assert result.transaction is not None
    tx = result.transaction
    assert tx.action is TransactionAction.BUY
    assert tx.symbol == SYMBOL
    assert tx.price_usdt == Decimal("67100")

    # 500 USDT * 0.5 предварительно, после квантования и шага 0.00001:
    # amount_pre = 500 / 67100 ≈ 0.0074515... → 0.00745 BTC
    assert tx.amount_crypto == Decimal("0.00745")
    assert tx.gross_usdt == Decimal("0.00745") * Decimal("67100")

    usdt_wallet = await wallet_crud.get(session, user_id=user_id, asset=QUOTE)
    btc_wallet = await wallet_crud.get(session, user_id=user_id, asset=ASSET)
    assert usdt_wallet is not None
    assert btc_wallet is not None
    assert usdt_wallet.balance == tx.usdt_balance_after
    assert btc_wallet.balance == tx.asset_balance_after
    assert usdt_wallet.balance == Decimal("1000") - tx.net_usdt
    assert btc_wallet.balance == Decimal("0.00745")


async def test_buy_below_min_notional_does_not_execute(session) -> None:
    user_id = await _seed_user_with_balances(session, usdt="100")
    decision = await _make_decision(
        session,
        user_id=user_id,
        action=DecisionAction.BUY,
        buy_fraction=Decimal("0.05"),  # ~5 USDT gross < min_notional=10
    )

    result = await execute_decision(
        session,
        decision=decision,
        symbol=SYMBOL,
        quote_asset=QUOTE,
        filters=_btc_filters(min_notional="10"),
        book_ticker=_ticker(),
        fee_rate=FEE,
    )

    assert result.executed is False
    assert result.not_executed_reason == NotExecutedReason.MIN_NOTIONAL.value
    assert result.transaction is None
    # балансы не тронуты
    usdt_wallet = await wallet_crud.get(session, user_id=user_id, asset=QUOTE)
    assert usdt_wallet is not None
    assert usdt_wallet.balance == Decimal("100")


async def test_buy_below_lot_size_returns_lot_size_reason(session) -> None:
    user_id = await _seed_user_with_balances(session, usdt="1000")
    decision = await _make_decision(
        session,
        user_id=user_id,
        action=DecisionAction.BUY,
        buy_fraction=Decimal("0.5"),
    )

    # экстремально крупный шаг — после квантования получится 0 BTC
    filters = _btc_filters(step="100", min_qty="100", min_notional="0")

    result = await execute_decision(
        session,
        decision=decision,
        symbol=SYMBOL,
        quote_asset=QUOTE,
        filters=filters,
        book_ticker=_ticker(),
        fee_rate=FEE,
    )

    assert result.executed is False
    assert result.not_executed_reason == NotExecutedReason.LOT_SIZE.value
    assert result.transaction is None


async def test_buy_with_invalid_fraction_returns_invalid_fraction(session) -> None:
    user_id = await _seed_user_with_balances(session, usdt="1000")
    decision = await _make_decision(
        session,
        user_id=user_id,
        action=DecisionAction.BUY,
        buy_fraction=Decimal("0"),  # вне (0, 1]
    )

    result = await execute_decision(
        session,
        decision=decision,
        symbol=SYMBOL,
        quote_asset=QUOTE,
        filters=_btc_filters(),
        book_ticker=_ticker(),
        fee_rate=FEE,
    )

    assert result.executed is False
    assert result.not_executed_reason == NotExecutedReason.INVALID_FRACTION.value


async def test_buy_with_empty_usdt_wallet_returns_insufficient_funds(session) -> None:
    user_id = await _seed_user_with_balances(session, usdt="0")
    decision = await _make_decision(
        session,
        user_id=user_id,
        action=DecisionAction.BUY,
        buy_fraction=Decimal("0.5"),
    )

    result = await execute_decision(
        session,
        decision=decision,
        symbol=SYMBOL,
        quote_asset=QUOTE,
        filters=_btc_filters(),
        book_ticker=_ticker(),
        fee_rate=FEE,
    )

    assert result.executed is False
    assert result.not_executed_reason == NotExecutedReason.INSUFFICIENT_FUNDS.value


async def test_sell_full_position_creates_transaction(session) -> None:
    user_id = await _seed_user_with_balances(session, usdt="500", btc="0.01")
    decision = await _make_decision(session, user_id=user_id, action=DecisionAction.SELL)

    result = await execute_decision(
        session,
        decision=decision,
        symbol=SYMBOL,
        quote_asset=QUOTE,
        filters=_btc_filters(),
        book_ticker=_ticker(bid="67000"),
        fee_rate=FEE,
    )

    assert result.executed is True
    tx = result.transaction
    assert tx is not None
    assert tx.action is TransactionAction.SELL
    assert tx.amount_crypto == Decimal("0.01")
    assert tx.price_usdt == Decimal("67000")

    expected_gross = Decimal("0.01") * Decimal("67000")  # 670
    expected_fee = expected_gross * FEE  # 0.67
    expected_net = expected_gross - expected_fee  # 669.33
    assert tx.gross_usdt == expected_gross
    assert tx.fee_usdt == expected_fee
    assert tx.net_usdt == expected_net

    usdt_wallet = await wallet_crud.get(session, user_id=user_id, asset=QUOTE)
    btc_wallet = await wallet_crud.get(session, user_id=user_id, asset=ASSET)
    assert usdt_wallet is not None and btc_wallet is not None
    assert usdt_wallet.balance == Decimal("500") + expected_net
    assert btc_wallet.balance == Decimal("0")


async def test_sell_empty_position_returns_empty_position(session) -> None:
    user_id = await _seed_user_with_balances(session, usdt="500", btc="0")
    decision = await _make_decision(session, user_id=user_id, action=DecisionAction.SELL)

    result = await execute_decision(
        session,
        decision=decision,
        symbol=SYMBOL,
        quote_asset=QUOTE,
        filters=_btc_filters(),
        book_ticker=_ticker(),
        fee_rate=FEE,
    )

    assert result.executed is False
    assert result.not_executed_reason == NotExecutedReason.EMPTY_POSITION.value


async def test_sell_below_min_notional_returns_min_notional(session) -> None:
    """Очень маленькая позиция: gross < min_notional."""
    user_id = await _seed_user_with_balances(
        session, usdt="500", btc="0.00001"  # 1 шаг = 0.00001 BTC
    )
    decision = await _make_decision(session, user_id=user_id, action=DecisionAction.SELL)

    result = await execute_decision(
        session,
        decision=decision,
        symbol=SYMBOL,
        quote_asset=QUOTE,
        filters=_btc_filters(min_notional="10"),
        book_ticker=_ticker(bid="100"),  # 0.00001 * 100 = 0.001 USDT
        fee_rate=FEE,
    )

    assert result.executed is False
    assert result.not_executed_reason == NotExecutedReason.MIN_NOTIONAL.value
    btc_wallet = await wallet_crud.get(session, user_id=user_id, asset=ASSET)
    assert btc_wallet is not None
    assert btc_wallet.balance == Decimal("0.00001")  # не тронули
