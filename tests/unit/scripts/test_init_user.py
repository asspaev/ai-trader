"""Тесты ядра ``scripts/init_user.py`` — БД-сценарии через прямой
вызов :func:`scripts.init_user.init_user`.

Сетевую часть (получение курса) подменяем готовым :class:`FxRate`,
поэтому тесты не зависят ни от Binance, ни от CoinGecko. Юнит-тесты
чистой функции ``convert_rub_to_usdt`` вынесены в
``test_convert_rub_to_usdt.py`` — они не используют БД.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.crud import user as user_crud
from app.crud import wallet as wallet_crud
from app.services.fx import FxRate
from scripts.init_user import (
    InitParams,
    UserAlreadyExistsError,
    init_user,
)


pytestmark = pytest.mark.asyncio(loop_scope="session")


async def test_init_user_creates_user_and_wallets(session) -> None:
    params = InitParams(
        telegram_id=42,
        username="trader",
        initial_capital_rub=Decimal("100000"),
    )
    fx = FxRate(rate=Decimal("90.5"), source="binance")

    result = await init_user(
        session,
        params,
        fx,
        crypto_symbols=["BTC", "ETH", "TON"],
        quote_asset="USDT",
    )

    assert result.user.telegram_id == 42
    assert result.user.username == "trader"
    assert result.user.initial_capital_rub == Decimal("100000")
    assert result.initial_capital_usdt == Decimal("1104.97237569")
    assert result.fx_rate == Decimal("90.50000000")
    assert result.fx_source == "binance"

    fetched = await user_crud.get_by_telegram_id(session, 42)
    assert fetched is not None

    wallets = {
        w.asset: w.balance
        for w in await wallet_crud.list_for_user(session, user_id=result.user.id)
    }
    assert wallets["USDT"] == Decimal("1104.97237569")
    assert wallets["BTC"] == Decimal("0")
    assert wallets["ETH"] == Decimal("0")
    assert wallets["TON"] == Decimal("0")


async def test_init_user_uses_settings_defaults(session) -> None:
    """Если symbols/quote не заданы — подставляются из TRADING_*."""
    params = InitParams(
        telegram_id=7,
        username=None,
        initial_capital_rub=Decimal("50000"),
    )
    fx = FxRate(rate=Decimal("100"), source="coingecko")

    result = await init_user(session, params, fx)

    wallets = await wallet_crud.list_for_user(session, user_id=result.user.id)
    asset_set = {w.asset for w in wallets}
    # Дефолт TradingSettings.symbols = ["BTC", "ETH", "TON"], quote = "USDT".
    assert asset_set == {"USDT", "BTC", "ETH", "TON"}

    usdt_wallet = next(w for w in wallets if w.asset == "USDT")
    assert usdt_wallet.balance == Decimal("500.00000000")


async def test_init_user_is_idempotent(session) -> None:
    params = InitParams(
        telegram_id=1,
        username="first",
        initial_capital_rub=Decimal("10000"),
    )
    fx = FxRate(rate=Decimal("90"), source="binance")

    await init_user(
        session,
        params,
        fx,
        crypto_symbols=["BTC", "ETH", "TON"],
    )

    second = InitParams(
        telegram_id=2,
        username="second",
        initial_capital_rub=Decimal("999999"),
    )
    with pytest.raises(UserAlreadyExistsError):
        await init_user(
            session,
            second,
            fx,
            crypto_symbols=["BTC", "ETH", "TON"],
        )

    # Убедимся, что вторую запись не записали.
    assert await user_crud.get_by_telegram_id(session, 2) is None


async def test_init_user_quantizes_rate_to_8_decimals(session) -> None:
    params = InitParams(
        telegram_id=100,
        username="precise",
        initial_capital_rub=Decimal("100"),
    )
    # Курс с 12 знаками после запятой — должен быть округлён до 8.
    fx = FxRate(rate=Decimal("90.123456789012"), source="binance")

    result = await init_user(
        session,
        params,
        fx,
        crypto_symbols=["BTC"],
    )

    assert result.fx_rate == Decimal("90.12345679")
    assert result.user.initial_usdt_rub_rate == Decimal("90.12345679")
