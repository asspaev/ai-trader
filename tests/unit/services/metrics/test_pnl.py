"""Тесты модуля :mod:`app.services.metrics.pnl` (фаза 10).

Покрытие:

* :func:`build_pnl_report` — чистая функция, гоняется на синтетических
  числах: позитивный/негативный PnL, баланс против HOLD, частичные
  исторические цены, нулевой капитал.
* :func:`format_pnl_lines` / :func:`format_pnl_inline` — форматтеры
  для ``/stats`` и ``notify_pipeline_summary``.
* :func:`fetch_init_prices`, :func:`fetch_current_bid_prices` —
  парсинг ответов Binance через локальный fake-клиент.
* :func:`compute_pnl` — интеграция: реальный pgvector-Postgres
  (testcontainers, как у соседних слоёв), kмок Binance, итоговый
  :class:`PnLReport` с baseline и без.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Mapping

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.crud import user as user_crud
from app.crud import wallet as wallet_crud
from app.services.metrics.pnl import (
    HoldBaseline,
    PnLReport,
    build_pnl_report,
    compute_pnl,
    fetch_current_bid_prices,
    fetch_init_prices,
    format_pnl_inline,
    format_pnl_lines,
)


_asyncio_session = pytest.mark.asyncio(loop_scope="session")
"""Маркер для async-тестов — собран по образцу tests/unit/services/telegram."""


# ---------- fake Binance ----------


class FakeBinance:
    """Минимальный fake поверх :class:`BinanceClient`.

    Поддерживает три эндпоинта, которых хватает для pnl-тестов:

    * ``/api/v3/klines`` — возвращает заранее заданный список свечей по
      ``(symbol, interval)``.
    * ``/api/v3/ticker/bookTicker`` — возвращает заранее заданный bid/ask.
    * Любая другая комбинация бросает :class:`AssertionError` (так
      проще диагностировать опечатки в тестах).
    """

    def __init__(
        self,
        *,
        klines: Mapping[tuple[str, str], list[list[Any]]] | None = None,
        book_tickers: Mapping[str, dict[str, str]] | None = None,
        klines_error_for: tuple[str, ...] = (),
    ) -> None:
        self._klines = dict(klines or {})
        self._book = dict(book_tickers or {})
        self._errors = set(klines_error_for)
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def get_json(
        self, path: str, params: Mapping[str, Any] | None = None
    ) -> Any:
        p = dict(params or {})
        self.calls.append((path, p))
        symbol = p.get("symbol", "")
        if path == "/api/v3/klines":
            if symbol in self._errors:
                raise RuntimeError(f"Boom on klines for {symbol}")
            interval = p.get("interval", "")
            key = (symbol, interval)
            return self._klines.get(key, [])
        if path == "/api/v3/ticker/bookTicker":
            return self._book[symbol]
        raise AssertionError(f"FakeBinance: unexpected path {path!r}")

    async def aclose(self) -> None:
        return None


def _kline(close: str | float | int) -> list[Any]:
    """Сконструировать минимально валидный kline-ответ Binance (12 полей)."""
    return [
        0,           # open_time
        "0",         # open
        "0",         # high
        "0",         # low
        str(close),  # close
        "0",         # volume
        0,           # close_time
        "0", 0, "0", "0", "0",
    ]


# ---------- build_pnl_report (pure) ----------


def test_build_pnl_report_positive_pnl_beats_hold() -> None:
    """Реальный портфель обогнал HOLD: delta > 0, PnL > 0."""
    report = build_pnl_report(
        initial_capital_usdt=Decimal("1000"),
        portfolio_value_usdt=Decimal("1200"),
        symbols=("BTC", "ETH", "TON"),
        init_prices={
            "BTC": Decimal("60000"),
            "ETH": Decimal("3000"),
            "TON": Decimal("5"),
        },
        current_prices={
            "BTC": Decimal("66000"),  # +10%
            "ETH": Decimal("3000"),   # 0%
            "TON": Decimal("5"),      # 0%
        },
    )
    assert report.pnl_usdt == Decimal("200")
    assert report.pnl_pct == Decimal("20")
    assert report.hold_baseline is not None
    # Per-asset = 1000/3 ≈ 333.33. Только BTC даёт +10%, итого hold ≈ 1000 + 33.33
    assert report.hold_baseline.per_asset_initial_usdt == Decimal("1000") / Decimal(3)
    expected_hold = (
        Decimal("1000") / Decimal(3) * Decimal("66000") / Decimal("60000")
        + Decimal("1000") / Decimal(3)
        + Decimal("1000") / Decimal(3)
    )
    assert report.hold_baseline.current_value_usdt == expected_hold
    # delta = (1200 - hold) / 1000 * 100
    expected_delta = (
        (Decimal("1200") - expected_hold) / Decimal("1000") * Decimal("100")
    )
    assert report.delta_vs_hold_pct == expected_delta
    assert report.delta_vs_hold_pct > 0


def test_build_pnl_report_negative_pnl_loses_to_hold() -> None:
    """Портфель просел больше, чем HOLD: PnL < 0, delta < 0."""
    report = build_pnl_report(
        initial_capital_usdt=Decimal("1000"),
        portfolio_value_usdt=Decimal("800"),
        symbols=("BTC",),
        init_prices={"BTC": Decimal("50000")},
        current_prices={"BTC": Decimal("55000")},  # HOLD: +10%
    )
    assert report.pnl_usdt == Decimal("-200")
    assert report.pnl_pct == Decimal("-20")
    assert report.hold_baseline is not None
    # HOLD: amount=1000/50000=0.02, value=0.02*55000=1100
    assert report.hold_baseline.current_value_usdt == Decimal("1100")
    # delta = (800-1100)/1000*100 = -30%
    assert report.delta_vs_hold_pct == Decimal("-30")


def test_build_pnl_report_no_init_prices_returns_no_baseline() -> None:
    """init_prices=None → hold_baseline=None, delta=None, PnL валиден."""
    report = build_pnl_report(
        initial_capital_usdt=Decimal("100"),
        portfolio_value_usdt=Decimal("120"),
        symbols=("BTC", "ETH"),
        init_prices=None,
        current_prices={"BTC": Decimal("60000"), "ETH": Decimal("3000")},
    )
    assert report.pnl_usdt == Decimal("20")
    assert report.pnl_pct == Decimal("20")
    assert report.hold_baseline is None
    assert report.delta_vs_hold_pct is None
    assert report.hold_value_usdt is None


def test_build_pnl_report_missing_one_init_price_drops_baseline() -> None:
    """Если по какому-то символу нет init-цены — baseline целиком отбрасывается."""
    report = build_pnl_report(
        initial_capital_usdt=Decimal("100"),
        portfolio_value_usdt=Decimal("110"),
        symbols=("BTC", "ETH"),
        init_prices={"BTC": Decimal("60000")},  # ETH отсутствует
        current_prices={"BTC": Decimal("66000"), "ETH": Decimal("3000")},
    )
    assert report.hold_baseline is None
    assert report.delta_vs_hold_pct is None


def test_build_pnl_report_missing_current_price_drops_baseline() -> None:
    """Если по какому-то символу нет текущей цены — baseline тоже отбрасывается."""
    report = build_pnl_report(
        initial_capital_usdt=Decimal("100"),
        portfolio_value_usdt=Decimal("100"),
        symbols=("BTC", "ETH"),
        init_prices={"BTC": Decimal("60000"), "ETH": Decimal("3000")},
        current_prices={"BTC": Decimal("60000")},  # ETH отсутствует
    )
    assert report.hold_baseline is None
    assert report.delta_vs_hold_pct is None


def test_build_pnl_report_zero_initial_capital_returns_zero_pct() -> None:
    """Нулевой капитал не должен ронять расчёт делением на ноль."""
    report = build_pnl_report(
        initial_capital_usdt=Decimal("0"),
        portfolio_value_usdt=Decimal("0"),
        symbols=("BTC",),
        init_prices={"BTC": Decimal("60000")},
        current_prices={"BTC": Decimal("60000")},
    )
    assert report.pnl_usdt == Decimal("0")
    assert report.pnl_pct == Decimal("0")
    assert report.hold_baseline is None
    assert report.delta_vs_hold_pct is None


def test_build_pnl_report_zero_init_price_drops_baseline() -> None:
    """Init-цена 0 (битые данные) — baseline недопустим, делений на ноль нет."""
    report = build_pnl_report(
        initial_capital_usdt=Decimal("100"),
        portfolio_value_usdt=Decimal("100"),
        symbols=("BTC",),
        init_prices={"BTC": Decimal("0")},
        current_prices={"BTC": Decimal("60000")},
    )
    assert report.hold_baseline is None
    assert report.delta_vs_hold_pct is None


# ---------- formatters ----------


def test_format_pnl_lines_positive_and_negative() -> None:
    report = PnLReport(
        initial_capital_usdt=Decimal("1000"),
        portfolio_value_usdt=Decimal("1024.55"),
        pnl_usdt=Decimal("24.55"),
        pnl_pct=Decimal("2.46"),
        hold_baseline=HoldBaseline(per_asset_initial_usdt=Decimal("333")),
        delta_vs_hold_pct=Decimal("0.85"),
    )
    lines = format_pnl_lines(report)
    assert lines[0] == "PnL: +24.55 USDT (+2.46%)"
    assert lines[1] == "vs HOLD: +0.85%"

    neg = PnLReport(
        initial_capital_usdt=Decimal("1000"),
        portfolio_value_usdt=Decimal("950"),
        pnl_usdt=Decimal("-50"),
        pnl_pct=Decimal("-5"),
        hold_baseline=HoldBaseline(per_asset_initial_usdt=Decimal("333")),
        delta_vs_hold_pct=Decimal("-3.21"),
    )
    lines_neg = format_pnl_lines(neg)
    assert lines_neg[0] == "PnL: −50.00 USDT (−5.00%)"
    assert lines_neg[1] == "vs HOLD: −3.21%"


def test_format_pnl_lines_no_baseline_marks_unavailable() -> None:
    report = PnLReport(
        initial_capital_usdt=Decimal("100"),
        portfolio_value_usdt=Decimal("120"),
        pnl_usdt=Decimal("20"),
        pnl_pct=Decimal("20"),
        hold_baseline=None,
        delta_vs_hold_pct=None,
    )
    lines = format_pnl_lines(report)
    assert lines[0] == "PnL: +20.00 USDT (+20.00%)"
    assert "недоступно" in lines[1]


def test_format_pnl_inline_joins_with_pipe() -> None:
    report = PnLReport(
        initial_capital_usdt=Decimal("1000"),
        portfolio_value_usdt=Decimal("1024.55"),
        pnl_usdt=Decimal("24.55"),
        pnl_pct=Decimal("2.46"),
        hold_baseline=HoldBaseline(per_asset_initial_usdt=Decimal("333")),
        delta_vs_hold_pct=Decimal("0.85"),
    )
    line = format_pnl_inline(report)
    assert line == "PnL: +24.55 USDT (+2.46%) | vs HOLD: +0.85%"


# ---------- fetch_init_prices ----------


@_asyncio_session
async def test_fetch_init_prices_parses_close_for_each_symbol() -> None:
    """Цена закрытия первой свечи становится init-ценой каждого символа."""
    binance = FakeBinance(
        klines={
            ("BTCUSDT", "1h"): [_kline("60000.50")],
            ("ETHUSDT", "1h"): [_kline("3000")],
            ("TONUSDT", "1h"): [_kline("5.25")],
        },
    )
    at = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
    result = await fetch_init_prices(
        binance,  # type: ignore[arg-type]
        symbols=("BTC", "ETH", "TON"),
        quote_asset="USDT",
        at=at,
    )
    assert result == {
        "BTC": Decimal("60000.50"),
        "ETH": Decimal("3000"),
        "TON": Decimal("5.25"),
    }
    # Все запросы — klines, и startTime отражает at в миллисекундах
    paths = [path for path, _ in binance.calls]
    assert paths == ["/api/v3/klines"] * 3
    expected_ms = int(at.timestamp() * 1000)
    assert all(p[1]["startTime"] == expected_ms for p in binance.calls)


@_asyncio_session
async def test_fetch_init_prices_skips_empty_and_errored_symbols() -> None:
    """Пустой ответ или ошибка отдельного символа не валит остальные."""
    binance = FakeBinance(
        klines={
            ("BTCUSDT", "1h"): [_kline("60000")],
            ("ETHUSDT", "1h"): [],  # пусто — пропускаем
        },
        klines_error_for=("TONUSDT",),
    )
    at = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
    result = await fetch_init_prices(
        binance,  # type: ignore[arg-type]
        symbols=("BTC", "ETH", "TON"),
        quote_asset="USDT",
        at=at,
    )
    assert result == {"BTC": Decimal("60000")}


@_asyncio_session
async def test_fetch_init_prices_normalizes_naive_datetime_to_utc() -> None:
    """Naive datetime интерпретируем как UTC — иначе ms-stamp поплывёт."""
    binance = FakeBinance(klines={("BTCUSDT", "1h"): [_kline("100")]})
    naive = datetime(2026, 6, 1, 12, 0)
    aware = naive.replace(tzinfo=timezone.utc)

    await fetch_init_prices(
        binance,  # type: ignore[arg-type]
        symbols=("BTC",),
        quote_asset="USDT",
        at=naive,
    )
    assert binance.calls[0][1]["startTime"] == int(aware.timestamp() * 1000)


# ---------- fetch_current_bid_prices ----------


@_asyncio_session
async def test_fetch_current_bid_prices_uses_book_ticker_bid() -> None:
    binance = FakeBinance(
        book_tickers={
            "BTCUSDT": {"symbol": "BTCUSDT", "bidPrice": "66000.5", "askPrice": "66100"},
            "ETHUSDT": {"symbol": "ETHUSDT", "bidPrice": "3000", "askPrice": "3005"},
        },
    )
    result = await fetch_current_bid_prices(
        binance,  # type: ignore[arg-type]
        symbols=("BTC", "ETH"),
        quote_asset="USDT",
    )
    assert result == {"BTC": Decimal("66000.5"), "ETH": Decimal("3000")}


@_asyncio_session
async def test_fetch_current_bid_prices_skips_failed_symbols() -> None:
    """Если по символу нет данных в фейке — он просто отсутствует в результате."""
    binance = FakeBinance(
        book_tickers={
            "BTCUSDT": {"symbol": "BTCUSDT", "bidPrice": "66000", "askPrice": "66100"},
        },
    )
    result = await fetch_current_bid_prices(
        binance,  # type: ignore[arg-type]
        symbols=("BTC", "ETH"),
        quote_asset="USDT",
    )
    assert result == {"BTC": Decimal("66000")}


# ---------- compute_pnl (integration) ----------


@pytest_asyncio.fixture(loop_scope="session")
async def session_factory(engine, session) -> async_sessionmaker:
    """async_sessionmaker поверх тестового engine — переиспользует фикстуру с truncate."""
    return async_sessionmaker(bind=engine, expire_on_commit=False)


@_asyncio_session
async def test_compute_pnl_end_to_end_with_baseline(
    session, session_factory
) -> None:
    """Полный путь: пользователь в БД, кошельки, Binance с klines и bid'ами."""
    user = await user_crud.create(
        session,
        telegram_id=99,
        username="t",
        initial_capital_rub=Decimal("100000"),
        initial_capital_usdt=Decimal("999"),
        initial_usdt_rub_rate=Decimal("100"),
    )
    # Реальный портфель: 200 USDT + 0.01 BTC по 66000 = 200 + 660 = 860 USDT
    await wallet_crud.upsert(
        session, user_id=user.id, asset="USDT", balance=Decimal("200")
    )
    await wallet_crud.upsert(
        session, user_id=user.id, asset="BTC", balance=Decimal("0.01")
    )
    await session.commit()

    binance = FakeBinance(
        klines={
            # Init-цены при users.created_at: BTC=60000, ETH=3000, TON=5
            ("BTCUSDT", "1h"): [_kline("60000")],
            ("ETHUSDT", "1h"): [_kline("3000")],
            ("TONUSDT", "1h"): [_kline("5")],
        },
        book_tickers={
            # Текущие bid'ы
            "BTCUSDT": {"symbol": "BTCUSDT", "bidPrice": "66000", "askPrice": "66100"},
            "ETHUSDT": {"symbol": "ETHUSDT", "bidPrice": "3000", "askPrice": "3005"},
            "TONUSDT": {"symbol": "TONUSDT", "bidPrice": "5", "askPrice": "5.01"},
        },
    )

    report = await compute_pnl(
        session_factory=session_factory,
        binance_client=binance,  # type: ignore[arg-type]
        user_id=user.id,
        quote_asset="USDT",
        symbols=("BTC", "ETH", "TON"),
    )

    # Портфель: USDT 200 + BTC 0.01 × 66000 = 860 USDT
    assert report.portfolio_value_usdt == Decimal("860")
    assert report.initial_capital_usdt == Decimal("999")
    assert report.pnl_usdt == Decimal("860") - Decimal("999")
    # baseline: 999/3 ≈ 333. BTC даёт +10%, ETH и TON ровно. hold ≈ 999 + 333 * 0.1 = 1032.3
    assert report.hold_baseline is not None
    assert report.delta_vs_hold_pct is not None
    assert report.hold_baseline.current_value_usdt == (
        Decimal("999") / Decimal(3) * (
            Decimal("66000") / Decimal("60000")
            + Decimal("1")
            + Decimal("1")
        )
    )


@_asyncio_session
async def test_compute_pnl_returns_no_baseline_when_klines_unavailable(
    session, session_factory
) -> None:
    """Если Binance не отдал klines — PnL посчитан, baseline=None."""
    user = await user_crud.create(
        session,
        telegram_id=77,
        username="t",
        initial_capital_rub=Decimal("100000"),
        initial_capital_usdt=Decimal("1000"),
        initial_usdt_rub_rate=Decimal("100"),
    )
    await wallet_crud.upsert(
        session, user_id=user.id, asset="USDT", balance=Decimal("1000")
    )
    await session.commit()

    binance = FakeBinance(
        # klines пуст → fetch_init_prices не сможет ничего набрать
        klines={},
        book_tickers={
            "BTCUSDT": {"symbol": "BTCUSDT", "bidPrice": "60000", "askPrice": "60100"},
        },
    )

    report = await compute_pnl(
        session_factory=session_factory,
        binance_client=binance,  # type: ignore[arg-type]
        user_id=user.id,
        quote_asset="USDT",
        symbols=("BTC",),
    )
    # Портфель = 1000 USDT (только USDT, BTC=0)
    assert report.portfolio_value_usdt == Decimal("1000")
    assert report.pnl_usdt == Decimal("0")
    assert report.hold_baseline is None
    assert report.delta_vs_hold_pct is None


@_asyncio_session
async def test_compute_pnl_missing_user_raises(session_factory) -> None:
    """Безопаснее упасть, чем считать метрику для несуществующего пользователя."""
    binance = FakeBinance()
    with pytest.raises(ValueError, match="not found"):
        await compute_pnl(
            session_factory=session_factory,
            binance_client=binance,  # type: ignore[arg-type]
            user_id=9999,
            quote_asset="USDT",
            symbols=("BTC",),
        )
