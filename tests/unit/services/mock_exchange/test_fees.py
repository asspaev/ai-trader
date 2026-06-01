"""Чистые тесты формул :mod:`app.services.mock_exchange.fees`."""

from __future__ import annotations

from decimal import Decimal

from app.services.mock_exchange.fees import quote_buy, quote_sell


FEE = Decimal("0.001")  # 0.10% taker


def test_buy_basic_fraction_consumes_gross_plus_fee() -> None:
    free = Decimal("1000")
    ask = Decimal("100")

    q = quote_buy(free_usdt=free, fraction=Decimal("0.5"), ask_price=ask, fee_rate=FEE)

    assert q.gross_usdt == Decimal("500")
    assert q.fee_usdt == Decimal("0.5")
    assert q.spend_usdt == Decimal("500.5")
    assert q.amount_crypto == Decimal("5")
    assert q.ask_price == ask


def test_buy_full_fraction_caps_at_free_usdt() -> None:
    """При fraction=1.0 spend = gross + fee > free → подгоняем под free."""
    free = Decimal("1000")
    ask = Decimal("100")

    q = quote_buy(free_usdt=free, fraction=Decimal("1.0"), ask_price=ask, fee_rate=FEE)

    assert q.spend_usdt == free
    assert q.gross_usdt + q.fee_usdt == free
    expected_gross = free / (Decimal("1") + FEE)
    assert q.gross_usdt == expected_gross
    assert q.amount_crypto == expected_gross / ask


def test_buy_zero_free_returns_zero_quote() -> None:
    q = quote_buy(
        free_usdt=Decimal("0"),
        fraction=Decimal("0.5"),
        ask_price=Decimal("100"),
        fee_rate=FEE,
    )
    assert q.gross_usdt == Decimal("0")
    assert q.amount_crypto == Decimal("0")
    assert q.spend_usdt == Decimal("0")


def test_buy_negative_fraction_returns_zero_quote() -> None:
    q = quote_buy(
        free_usdt=Decimal("1000"),
        fraction=Decimal("-0.5"),
        ask_price=Decimal("100"),
        fee_rate=FEE,
    )
    assert q.gross_usdt == Decimal("0")
    assert q.amount_crypto == Decimal("0")


def test_sell_basic_net_equals_gross_minus_fee() -> None:
    q = quote_sell(
        amount_crypto=Decimal("2"),
        bid_price=Decimal("100"),
        fee_rate=FEE,
    )
    assert q.gross_usdt == Decimal("200")
    assert q.fee_usdt == Decimal("0.2")
    assert q.net_usdt == Decimal("199.8")
    assert q.amount_crypto == Decimal("2")


def test_sell_zero_amount_returns_zero_quote() -> None:
    q = quote_sell(
        amount_crypto=Decimal("0"),
        bid_price=Decimal("100"),
        fee_rate=FEE,
    )
    assert q.gross_usdt == Decimal("0")
    assert q.net_usdt == Decimal("0")


def test_buy_high_precision_does_not_round_silently() -> None:
    """Decimal-арифметика не теряет знаки точности."""
    q = quote_buy(
        free_usdt=Decimal("1000"),
        fraction=Decimal("0.333"),
        ask_price=Decimal("67432.12"),
        fee_rate=FEE,
    )
    assert q.gross_usdt == Decimal("333.000")
    assert q.fee_usdt == Decimal("0.333000")
    # spend < free → формула без корректировки
    assert q.spend_usdt == Decimal("333.333000")
