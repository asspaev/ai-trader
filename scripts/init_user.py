"""Одноразовая инициализация пользователя.

Запрашивает у оператора ``telegram_id`` / ``username`` / стартовый
RUB-капитал, получает курс USDT/RUB (Binance bookTicker, fallback
CoinGecko), создаёт единственную запись в таблице ``users`` и
начальный набор кошельков:

* ``USDT`` — конвертированный стартовый капитал;
* ``BTC`` / ``ETH`` / ``TON`` (или то, что задано в
  ``TRADING_SYMBOLS``) — нулевой баланс.

Скрипт идемпотентен: если запись пользователя уже существует, он
ничего не меняет и сообщает об этом. Запускается из контейнера или
локально::

    python -m scripts.init_user
"""

from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal, InvalidOperation
from typing import Iterable

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.db import SessionLocal, dispose_engine
from app.core.logger import configure_logging
from app.crud import user as user_crud
from app.crud import wallet as wallet_crud
from app.models import User
from app.services.binance.client import BinanceClient
from app.services.fx import FxRate, FxRateError, fetch_usdt_rub_rate


# Точность колонок users.initial_capital_usdt и users.initial_usdt_rub_rate
# в схеме БД — Numeric(18, 8). Округляем вниз, чтобы не «переоценить»
# USDT-капитал относительно реального курса.
_USDT_AMOUNT_PRECISION = Decimal("0.00000001")
_USDT_RUB_RATE_PRECISION = Decimal("0.00000001")


class UserAlreadyExistsError(RuntimeError):
    """В БД уже есть init-запись пользователя — повторный init запрещён."""


@dataclass(frozen=True, slots=True)
class InitParams:
    """Параметры, которые оператор задаёт при инициализации."""

    telegram_id: int
    username: str | None
    initial_capital_rub: Decimal


@dataclass(frozen=True, slots=True)
class InitResult:
    """Что фактически было создано в БД."""

    user: User
    fx_rate: Decimal
    fx_source: str
    initial_capital_usdt: Decimal


def convert_rub_to_usdt(rub: Decimal, rate: Decimal) -> Decimal:
    """Сконвертировать RUB → USDT по заданному курсу «RUB за 1 USDT».

    Args:
        rub: Сумма в рублях.
        rate: Курс RUB/USDT.

    Returns:
        Decimal, округлённый вниз до 8 знаков после запятой.

    Raises:
        ValueError: Если курс ноль или отрицательный.
    """
    if rate <= 0:
        raise ValueError(f"USDT/RUB rate must be positive, got {rate!r}")
    return (rub / rate).quantize(_USDT_AMOUNT_PRECISION, rounding=ROUND_DOWN)


async def init_user(
    session: AsyncSession,
    params: InitParams,
    fx_rate: FxRate,
    *,
    crypto_symbols: Iterable[str] | None = None,
    quote_asset: str | None = None,
) -> InitResult:
    """Создать пользователя и начальные кошельки в единой транзакции.

    Args:
        session: Активная async-сессия SQLAlchemy.
        params: Введённые оператором параметры.
        fx_rate: Полученный курс USDT/RUB (rate + source).
        crypto_symbols: Список криптоактивов с нулевым стартовым
            балансом. ``None`` → берётся из ``TRADING_SYMBOLS``.
        quote_asset: Котировочный актив (``USDT``). ``None`` →
            ``TRADING_QUOTE_ASSET``.

    Returns:
        :class:`InitResult` с созданным пользователем и расчётом.

    Raises:
        UserAlreadyExistsError: Init-запись уже существует.
    """
    existing = await user_crud.get_singleton(session)
    if existing is not None:
        raise UserAlreadyExistsError(
            f"User #{existing.id} already exists "
            f"(telegram_id={existing.telegram_id})"
        )

    symbols = (
        list(crypto_symbols)
        if crypto_symbols is not None
        else list(settings.trading.symbols)
    )
    quote = quote_asset or settings.trading.quote_asset

    initial_capital_usdt = convert_rub_to_usdt(
        params.initial_capital_rub, fx_rate.rate
    )
    rate_quantized = fx_rate.rate.quantize(_USDT_RUB_RATE_PRECISION)

    user = await user_crud.create(
        session,
        telegram_id=params.telegram_id,
        username=params.username,
        initial_capital_rub=params.initial_capital_rub,
        initial_capital_usdt=initial_capital_usdt,
        initial_usdt_rub_rate=rate_quantized,
    )

    await wallet_crud.create(
        session,
        user_id=user.id,
        asset=quote,
        balance=initial_capital_usdt,
    )
    for asset in symbols:
        await wallet_crud.create(
            session,
            user_id=user.id,
            asset=asset.upper(),
            balance=Decimal("0"),
        )

    return InitResult(
        user=user,
        fx_rate=rate_quantized,
        fx_source=fx_rate.source,
        initial_capital_usdt=initial_capital_usdt,
    )


# ---------- CLI ----------


def _prompt_int(message: str) -> int:
    """Спросить у оператора целое число, повторяем до валидного ввода."""
    while True:
        raw = input(message).strip()
        try:
            return int(raw)
        except ValueError:
            print(f"  '{raw}' — не целое число, попробуйте ещё раз.")


def _prompt_optional_str(message: str) -> str | None:
    raw = input(message).strip()
    return raw or None


def _prompt_decimal(message: str, default: Decimal) -> Decimal:
    """Спросить ``Decimal``; пустой ввод → ``default``."""
    while True:
        raw = input(f"{message} [{default}]: ").strip()
        if not raw:
            return default
        try:
            return Decimal(raw)
        except InvalidOperation:
            print(f"  '{raw}' — не число, попробуйте ещё раз.")


def _collect_params_from_stdin() -> InitParams:
    """Собрать :class:`InitParams` интерактивным опросом stdin."""
    print("=== Инициализация пользователя AI-Trader ===")
    telegram_id = _prompt_int("Telegram ID (числом): ")
    username = _prompt_optional_str(
        "Telegram username (без @, можно оставить пустым): "
    )
    capital_rub = _prompt_decimal(
        "Стартовый капитал в RUB",
        settings.trading.initial_capital_rub,
    )
    return InitParams(
        telegram_id=telegram_id,
        username=username,
        initial_capital_rub=capital_rub,
    )


async def _async_main() -> int:
    configure_logging()
    params = _collect_params_from_stdin()

    async with BinanceClient() as binance:
        try:
            fx_rate = await fetch_usdt_rub_rate(binance)
        except FxRateError as exc:
            logger.error("Failed to fetch USDT/RUB rate: {err}", err=str(exc))
            print(f"Не удалось получить курс USDT/RUB: {exc}")
            return 2

    print(
        f"Курс USDT/RUB = {fx_rate.rate} "
        f"(источник: {fx_rate.source})"
    )

    async with SessionLocal() as session:
        try:
            result = await init_user(session, params, fx_rate)
        except UserAlreadyExistsError as exc:
            await session.rollback()
            logger.warning("Init aborted: {err}", err=str(exc))
            print(f"Пропускаю инициализацию: {exc}")
            return 1
        await session.commit()

    print(
        "Готово. Пользователь создан: "
        f"id={result.user.id}, telegram_id={result.user.telegram_id}, "
        f"USDT-капитал={result.initial_capital_usdt} "
        f"(из {params.initial_capital_rub} RUB по курсу {result.fx_rate})."
    )
    logger.info(
        "User initialized: id={uid}, telegram_id={tg}, "
        "capital_rub={rub}, capital_usdt={usdt}, rate={rate}, source={source}",
        uid=result.user.id,
        tg=result.user.telegram_id,
        rub=params.initial_capital_rub,
        usdt=result.initial_capital_usdt,
        rate=result.fx_rate,
        source=result.fx_source,
    )
    return 0


def main() -> int:
    try:
        return asyncio.run(_run_and_dispose())
    except KeyboardInterrupt:
        print("\nОтменено пользователем.")
        return 130


async def _run_and_dispose() -> int:
    try:
        return await _async_main()
    finally:
        await dispose_engine()


if __name__ == "__main__":
    sys.exit(main())
