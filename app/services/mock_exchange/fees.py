"""Чистые формулы расчёта комиссии и net-суммы.

Здесь нет ни HTTP-запросов, ни обращений к БД — только арифметика над
``Decimal``. Это позволяет покрыть формулы юнит-тестами без поднятия
БД и проверить пограничные случаи (нулевой капитал, спред, округление).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class BuyQuote:
    """Параметры исполнения BUY на mock-бирже.

    Attributes:
        gross_usdt: Сколько USDT уходит на покупку до комиссии
            (``amount_crypto * ask_price``).
        fee_usdt: Комиссия taker в USDT.
        spend_usdt: Фактическое списание с USDT-кошелька
            (``gross + fee``).
        amount_crypto: Сколько базового актива приобрели.
        ask_price: Цена ask из bookTicker, по которой исполнено.
    """

    gross_usdt: Decimal
    fee_usdt: Decimal
    spend_usdt: Decimal
    amount_crypto: Decimal
    ask_price: Decimal


@dataclass(frozen=True, slots=True)
class SellQuote:
    """Параметры исполнения SELL.

    SELL = продажа всей позиции (см. clarifications). Поэтому здесь нет
    «доли продажи» — на входе уже округлённое до stepSize количество.
    """

    gross_usdt: Decimal
    fee_usdt: Decimal
    net_usdt: Decimal
    amount_crypto: Decimal
    bid_price: Decimal


def quote_buy(
    *,
    free_usdt: Decimal,
    fraction: Decimal,
    ask_price: Decimal,
    fee_rate: Decimal,
) -> BuyQuote:
    """Посчитать параметры BUY до проверки фильтров биржи.

    Если ``free_usdt * fraction`` + комиссия превысят свободный USDT —
    подрезаем gross так, чтобы spend ровно равнялся ``free_usdt``
    (см. формулы в ``architecture.md`` §6).

    Args:
        free_usdt: Текущий свободный USDT-баланс пользователя.
        fraction: Доля свободного USDT, которую AI хочет потратить
            (диапазон ``(0, 1]``).
        ask_price: Цена ask из ``bookTicker`` на момент сделки.
        fee_rate: Доля комиссии taker (``0.001`` = 0.10%).

    Returns:
        :class:`BuyQuote` — округление до ``stepSize`` остаётся на
        вызывающей стороне (executor), здесь только формулы.
    """
    if free_usdt <= 0 or fraction <= 0 or ask_price <= 0:
        return BuyQuote(
            gross_usdt=Decimal("0"),
            fee_usdt=Decimal("0"),
            spend_usdt=Decimal("0"),
            amount_crypto=Decimal("0"),
            ask_price=ask_price,
        )

    gross = free_usdt * fraction
    fee = gross * fee_rate
    spend = gross + fee

    if spend > free_usdt:
        # подгоняем под потолок свободных USDT: spend == free_usdt
        gross = free_usdt / (Decimal("1") + fee_rate)
        fee = free_usdt - gross
        spend = free_usdt

    amount_crypto = gross / ask_price

    return BuyQuote(
        gross_usdt=gross,
        fee_usdt=fee,
        spend_usdt=spend,
        amount_crypto=amount_crypto,
        ask_price=ask_price,
    )


def quote_sell(
    *,
    amount_crypto: Decimal,
    bid_price: Decimal,
    fee_rate: Decimal,
) -> SellQuote:
    """Посчитать параметры SELL по уже округлённому количеству.

    Args:
        amount_crypto: Сколько актива продаём (уже округлено вниз до
            stepSize вызывающей стороной).
        bid_price: Цена bid из ``bookTicker``.
        fee_rate: Доля комиссии taker.

    Returns:
        :class:`SellQuote` с ``net_usdt = gross - fee``.
    """
    if amount_crypto <= 0 or bid_price <= 0:
        return SellQuote(
            gross_usdt=Decimal("0"),
            fee_usdt=Decimal("0"),
            net_usdt=Decimal("0"),
            amount_crypto=Decimal("0"),
            bid_price=bid_price,
        )

    gross = amount_crypto * bid_price
    fee = gross * fee_rate
    net = gross - fee

    return SellQuote(
        gross_usdt=gross,
        fee_usdt=fee,
        net_usdt=net,
        amount_crypto=amount_crypto,
        bid_price=bid_price,
    )


__all__ = ["BuyQuote", "SellQuote", "quote_buy", "quote_sell"]
