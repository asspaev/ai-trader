"""Тесты чистой логики ``prices``: парсинг, агрегация, метрики."""

from __future__ import annotations

import json
import math
from decimal import Decimal
from pathlib import Path

from app.services.binance.prices import (
    TIMEFRAMES,
    aggregate_klines,
    aggregate_price_metrics,
    compute_metrics,
    parse_klines,
)


FIXTURES = Path(__file__).resolve().parents[3] / "fixtures"


def _load_1h_fixture() -> list[list]:
    return json.loads((FIXTURES / "binance_klines_1h.json").read_text())


def test_parse_klines_keeps_decimal_precision() -> None:
    raw = _load_1h_fixture()
    klines = parse_klines(raw)
    assert len(klines) == 6
    assert klines[0].open == Decimal("100.00")
    assert klines[0].close == Decimal("102.00")
    assert klines[-1].close == Decimal("111.00")


def test_aggregate_klines_factor_one_is_identity() -> None:
    klines = parse_klines(_load_1h_fixture())
    assert aggregate_klines(klines, 1) == klines


def test_aggregate_klines_groups_consecutive_candles() -> None:
    """6 свечей 1h → 2 свечи 3h: open=first.open, close=last.close, high/low — крайние."""
    klines = parse_klines(_load_1h_fixture())
    aggregated = aggregate_klines(klines, 3)

    assert len(aggregated) == 2

    first = aggregated[0]
    assert first.open == Decimal("100.00")
    assert first.close == Decimal("105.00")
    assert first.high == Decimal("110.00")
    assert first.low == Decimal("98.00")
    assert first.volume == Decimal("33.0")  # 10 + 12 + 11

    second = aggregated[1]
    assert second.open == Decimal("105.00")
    assert second.close == Decimal("111.00")
    assert second.high == Decimal("112.00")
    assert second.low == Decimal("99.00")
    assert second.volume == Decimal("42.0")  # 13 + 14 + 15


def test_aggregate_klines_drops_partial_tail() -> None:
    """Если хвост не набирает factor — отбрасываем."""
    klines = parse_klines(_load_1h_fixture())  # 6 штук
    # factor=4 → 1 полная группа, остальные 2 — отбрасываем
    aggregated = aggregate_klines(klines, 4)
    assert len(aggregated) == 1


def test_compute_metrics_basic_values() -> None:
    klines = parse_klines(_load_1h_fixture())
    metrics = compute_metrics("1h", klines, candle_count=6)

    assert metrics is not None
    assert metrics.timeframe == "1h"
    assert metrics.candles_used == 6
    assert metrics.close_now == Decimal("111.00")
    # (111 - 102) / 102 * 100 = 8.8235...
    assert metrics.change_pct.quantize(Decimal("0.0001")) == Decimal("8.8235")
    assert metrics.min_price == Decimal("98.00")
    assert metrics.max_price == Decimal("112.00")
    assert metrics.volatility_pct is not None


def test_compute_metrics_uses_only_last_n_candles() -> None:
    klines = parse_klines(_load_1h_fixture())
    metrics = compute_metrics("1h", klines, candle_count=2)

    assert metrics is not None
    assert metrics.candles_used == 2
    # last two: close 103 → 111
    # (111 - 103) / 103 * 100
    expected = (Decimal("111") - Decimal("103")) / Decimal("103") * Decimal("100")
    assert metrics.change_pct == expected
    assert metrics.close_now == Decimal("111.00")


def test_compute_metrics_returns_none_for_empty() -> None:
    assert compute_metrics("1h", [], candle_count=10) is None


def test_compute_metrics_volatility_matches_sample_stddev() -> None:
    klines = parse_klines(_load_1h_fixture())
    metrics = compute_metrics("1h", klines, candle_count=6)

    closes = [102.0, 107.0, 105.0, 101.0, 103.0, 111.0]
    returns = [
        (closes[i] - closes[i - 1]) / closes[i - 1] * 100.0
        for i in range(1, len(closes))
    ]
    mean = sum(returns) / len(returns)
    variance = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
    expected = math.sqrt(variance)

    assert metrics is not None
    assert metrics.volatility_pct is not None
    assert abs(float(metrics.volatility_pct) - expected) < 1e-9


def test_aggregate_price_metrics_produces_expected_timeframes() -> None:
    """Из одного 1h-набора берётся и нативный ``1h``, и агрегированный ``3h``."""
    raw_klines = _load_1h_fixture()
    metrics = aggregate_price_metrics({"1h": raw_klines})

    assert "1h" in metrics
    assert "3h" in metrics
    # 6 нативных свечей, для 1h candle_count=24 → метрики посчитаны на 6
    assert metrics["1h"].candles_used == 6
    # для 3h candle_count=16, но агрегированных всего 2
    assert metrics["3h"].candles_used == 2


def test_timeframes_cover_required_codes() -> None:
    """Спека таймфреймов должна перекрывать архитектурный набор."""
    expected = {
        "1m", "30m", "1h", "3h", "6h", "12h", "1d", "3d",
        "7d", "1M", "3M", "6M", "1Y", "3Y", "5Y",
    }
    codes = {spec.code for spec in TIMEFRAMES}
    assert codes == expected
