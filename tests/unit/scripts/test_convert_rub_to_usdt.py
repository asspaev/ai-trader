"""Юнит-тесты для чистой функции конвертации RUB → USDT.

Отделено от тестов БД-сценариев: эти тесты не требуют postgres-
контейнера и не задействуют event-loop пытоновского asyncio.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from scripts.init_user import convert_rub_to_usdt


def test_rounds_down_to_8_decimals() -> None:
    # 100 000 / 90.5 = 1104.97237569...
    assert convert_rub_to_usdt(Decimal("100000"), Decimal("90.5")) == Decimal(
        "1104.97237569"
    )


def test_exact_division() -> None:
    assert convert_rub_to_usdt(Decimal("905"), Decimal("90.5")) == Decimal(
        "10.00000000"
    )


def test_zero_capital_yields_zero() -> None:
    result = convert_rub_to_usdt(Decimal("0"), Decimal("90.5"))
    assert result == Decimal("0")
    # Хранится с явной точностью 8 знаков.
    assert result.as_tuple().exponent == -8


def test_rejects_zero_rate() -> None:
    with pytest.raises(ValueError):
        convert_rub_to_usdt(Decimal("100000"), Decimal("0"))


def test_rejects_negative_rate() -> None:
    with pytest.raises(ValueError):
        convert_rub_to_usdt(Decimal("100000"), Decimal("-1"))


def test_truncates_does_not_round_up() -> None:
    # 1 / 3 = 0.33333333333..., обрезаем до 0.33333333 (не 0.33333334).
    assert convert_rub_to_usdt(Decimal("1"), Decimal("3")) == Decimal(
        "0.33333333"
    )
