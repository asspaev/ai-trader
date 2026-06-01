"""Тесты разбора ``exchangeInfo`` и квантования количества."""

from __future__ import annotations

from decimal import Decimal

from app.services.binance.exchange_info import ExchangeInfoCache, _parse_symbol


def _btc_payload() -> dict:
    """Минимальный realistic payload по одному символу."""
    return {
        "symbol": "BTCUSDT",
        "baseAsset": "BTC",
        "quoteAsset": "USDT",
        "filters": [
            {
                "filterType": "PRICE_FILTER",
                "tickSize": "0.01000000",
            },
            {
                "filterType": "LOT_SIZE",
                "stepSize": "0.00001000",
                "minQty": "0.00001000",
                "maxQty": "9000.00000000",
            },
            {
                "filterType": "NOTIONAL",
                "minNotional": "10.00000000",
            },
        ],
    }


def test_parse_symbol_extracts_required_filters() -> None:
    sf = _parse_symbol(_btc_payload())
    assert sf.symbol == "BTCUSDT"
    assert sf.base_asset == "BTC"
    assert sf.quote_asset == "USDT"
    assert sf.step_size == Decimal("0.00001000")
    assert sf.min_qty == Decimal("0.00001000")
    assert sf.tick_size == Decimal("0.01000000")
    assert sf.min_notional == Decimal("10.00000000")


def test_parse_symbol_falls_back_to_min_notional_filter_type() -> None:
    """Старые ответы Binance используют ``MIN_NOTIONAL`` вместо ``NOTIONAL``."""
    payload = _btc_payload()
    payload["filters"][2] = {
        "filterType": "MIN_NOTIONAL",
        "minNotional": "5.00000000",
    }
    sf = _parse_symbol(payload)
    assert sf.min_notional == Decimal("5.00000000")


def test_quantize_amount_rounds_down_to_step_size() -> None:
    sf = _parse_symbol(_btc_payload())
    # step=0.00001, 0.001234567 → 0.00123 (123 шага по 0.00001)
    quantized = sf.quantize_amount(Decimal("0.00123456"))
    assert quantized == Decimal("0.00123")


def test_quantize_amount_below_min_qty_returns_zero() -> None:
    sf = _parse_symbol(_btc_payload())
    # 0.0000099 < min_qty=0.00001 → 0
    quantized = sf.quantize_amount(Decimal("0.0000099"))
    assert quantized == Decimal("0")


def test_exchange_info_cache_lookup() -> None:
    sf = _parse_symbol(_btc_payload())
    cache = ExchangeInfoCache([sf])
    assert "BTCUSDT" in cache
    assert cache.get("BTCUSDT") is sf
    assert cache.symbols() == ["BTCUSDT"]


def test_exchange_info_cache_raises_on_unknown_symbol() -> None:
    cache = ExchangeInfoCache([_parse_symbol(_btc_payload())])
    try:
        cache.get("ETHUSDT")
    except KeyError as exc:
        assert "ETHUSDT" in str(exc)
    else:
        raise AssertionError("Expected KeyError for unknown symbol")
