"""Расчёт PnL и метрики ``delta_vs_hold_pct`` (фаза 10).

Согласно ``architecture.md`` §13, успех системы оценивается по двум
числам:

* **PnL** — разница между текущей стоимостью портфеля и стартовым
  капиталом в USDT (плюс то же самое в процентах).
* **delta_vs_hold_pct** — разница (в процентах от стартового капитала)
  между реальной стоимостью портфеля и гипотетическим baseline
  «купил равные доли BTC/ETH/TON на ``initial_capital_usdt`` в момент
  ``users.created_at`` и держу до сих пор». Этот baseline отражает
  пассивную стратегию: AI обязан её обыгрывать, иначе смысла нет.

Модуль разделён на два слоя:

* :func:`build_pnl_report` — **чистая** функция: получает все нужные
  цены/балансы как параметры и собирает :class:`PnLReport`. Не делает
  ни сетевых, ни DB-запросов — легко тестируется.
* :func:`fetch_init_prices`, :func:`fetch_current_bid_prices`,
  :func:`compute_pnl` — тонкие обёртки, которые подтягивают цены из
  Binance public API и пользователя из БД, а затем зовут
  :func:`build_pnl_report`.

Если по какому-то символу не удалось получить инициирующую цену
(Binance не отдал свечу, символ ещё не торговался на момент init и т.д.),
``hold_baseline`` будет ``None`` — ``delta_vs_hold_pct`` в этом случае
тоже ``None``, а ``PnL`` остаётся вычислимым.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.crud import user as user_crud
from app.services.binance.client import BinanceClient
from app.services.binance.prices import fetch_book_ticker
from app.services.telegram.notifier import (
    PortfolioSnapshot,
    build_portfolio_snapshot,
)


# ---------- dataclass'ы отчёта ----------


@dataclass(frozen=True, slots=True)
class HoldBaseline:
    """Гипотетический «купил равными долями и держу» портфель.

    Attributes:
        per_asset_initial_usdt: Сколько USDT было бы вложено в один
            актив при init (``initial / N``, где N — число символов).
        amounts_by_asset: Сколько монеты было бы куплено
            (``per_asset / init_price``) — упорядочено по символам.
        init_prices_by_asset: Использованные исторические цены входа.
        current_prices_by_asset: Использованные текущие bid-цены.
        current_value_usdt: Сумма ``amount × current_price`` по всем
            активам — текущая стоимость HOLD-портфеля в USDT.
    """

    per_asset_initial_usdt: Decimal
    amounts_by_asset: Mapping[str, Decimal] = field(default_factory=dict)
    init_prices_by_asset: Mapping[str, Decimal] = field(default_factory=dict)
    current_prices_by_asset: Mapping[str, Decimal] = field(default_factory=dict)
    current_value_usdt: Decimal = Decimal("0")


@dataclass(frozen=True, slots=True)
class PnLReport:
    """Сводный отчёт о PnL пользователя.

    Хранит достаточно деталей, чтобы Telegram-форматтер мог как
    отрендерить простую строку «+24.55 USDT (+2.46%)», так и собрать
    более подробный вид с разбивкой по активам baseline.
    """

    initial_capital_usdt: Decimal
    portfolio_value_usdt: Decimal
    pnl_usdt: Decimal
    pnl_pct: Decimal
    hold_baseline: HoldBaseline | None
    delta_vs_hold_pct: Decimal | None

    @property
    def hold_value_usdt(self) -> Decimal | None:
        """Сокращение ``hold_baseline.current_value_usdt`` (или None)."""
        return (
            self.hold_baseline.current_value_usdt
            if self.hold_baseline is not None
            else None
        )


# ---------- pure: build_pnl_report ----------


def build_pnl_report(
    *,
    initial_capital_usdt: Decimal,
    portfolio_value_usdt: Decimal,
    symbols: Sequence[str],
    init_prices: Mapping[str, Decimal] | None,
    current_prices: Mapping[str, Decimal],
) -> PnLReport:
    """Собрать :class:`PnLReport` из готовых чисел.

    Чистая функция: ничего не дёргает извне. ``init_prices=None``
    означает «исторические цены недоступны» — ``hold_baseline`` и
    ``delta_vs_hold_pct`` будут ``None``, PnL всё равно посчитается.

    Args:
        initial_capital_usdt: Стартовый капитал в USDT (из
            ``users.initial_capital_usdt``).
        portfolio_value_usdt: Текущая стоимость портфеля
            (``PortfolioSnapshot.total_usdt``).
        symbols: Активы, которые входят в HOLD-baseline (``["BTC",
            "ETH", "TON"]``). Порядок сохраняется в
            ``amounts_by_asset``.
        init_prices: Цены каждого символа на момент ``users.created_at``
            (тикер в верхнем регистре). ``None`` если получить не
            удалось.
        current_prices: Текущие bid-цены каждого символа.
    """
    pnl_usdt = portfolio_value_usdt - initial_capital_usdt
    pnl_pct = (
        (pnl_usdt / initial_capital_usdt) * Decimal("100")
        if initial_capital_usdt > 0
        else Decimal("0")
    )

    hold_baseline = _build_hold_baseline(
        initial_capital_usdt=initial_capital_usdt,
        symbols=symbols,
        init_prices=init_prices,
        current_prices=current_prices,
    )
    if hold_baseline is None or initial_capital_usdt <= 0:
        delta_vs_hold_pct: Decimal | None = None
    else:
        delta_vs_hold_pct = (
            (portfolio_value_usdt - hold_baseline.current_value_usdt)
            / initial_capital_usdt
            * Decimal("100")
        )

    return PnLReport(
        initial_capital_usdt=initial_capital_usdt,
        portfolio_value_usdt=portfolio_value_usdt,
        pnl_usdt=pnl_usdt,
        pnl_pct=pnl_pct,
        hold_baseline=hold_baseline,
        delta_vs_hold_pct=delta_vs_hold_pct,
    )


def _build_hold_baseline(
    *,
    initial_capital_usdt: Decimal,
    symbols: Sequence[str],
    init_prices: Mapping[str, Decimal] | None,
    current_prices: Mapping[str, Decimal],
) -> HoldBaseline | None:
    """Собрать :class:`HoldBaseline` или ``None``.

    Возвращает ``None``, если хотя бы по одному символу нет либо
    исторической, либо текущей цены — без полного покрытия baseline
    нельзя оценить честно.
    """
    if not symbols or init_prices is None or initial_capital_usdt <= 0:
        return None
    n = len(symbols)
    per_asset = initial_capital_usdt / Decimal(n)
    amounts: dict[str, Decimal] = {}
    used_init: dict[str, Decimal] = {}
    used_current: dict[str, Decimal] = {}
    total_hold = Decimal("0")

    for asset in symbols:
        asset_u = asset.upper()
        ip = init_prices.get(asset_u)
        cp = current_prices.get(asset_u)
        if ip is None or cp is None or ip <= 0 or cp <= 0:
            return None
        amount = per_asset / ip
        amounts[asset_u] = amount
        used_init[asset_u] = ip
        used_current[asset_u] = cp
        total_hold += amount * cp

    return HoldBaseline(
        per_asset_initial_usdt=per_asset,
        amounts_by_asset=amounts,
        init_prices_by_asset=used_init,
        current_prices_by_asset=used_current,
        current_value_usdt=total_hold,
    )


# ---------- network: fetch ----------


async def fetch_init_prices(
    binance_client: BinanceClient,
    *,
    symbols: Sequence[str],
    quote_asset: str,
    at: datetime,
) -> dict[str, Decimal]:
    """Получить цены каждого символа на момент ``at`` (UTC).

    Запрашиваем по одной 1h-свече, ``startTime=at`` — берём цену
    закрытия (поле ``[4]``). Для актива, по которому Binance не отдал
    данных, ключ в результате отсутствует — вызывающая сторона решает,
    считать ли baseline частично.
    """
    log = logger.bind(component="metrics.pnl")
    at_utc = at.astimezone(timezone.utc) if at.tzinfo else at.replace(tzinfo=timezone.utc)
    start_ms = int(at_utc.timestamp() * 1000)

    result: dict[str, Decimal] = {}
    quote_upper = quote_asset.upper()
    for raw in symbols:
        asset = raw.upper()
        symbol = f"{asset}{quote_upper}"
        try:
            payload = await binance_client.get_json(
                "/api/v3/klines",
                params={
                    "symbol": symbol,
                    "interval": "1h",
                    "startTime": start_ms,
                    "limit": 1,
                },
            )
        except Exception as exc:  # noqa: BLE001 — изоляция отказа Binance
            log.warning(
                "Failed to fetch init price for {symbol}: {err}",
                symbol=symbol,
                err=f"{type(exc).__name__}: {exc}",
            )
            continue
        close = _parse_kline_close(payload)
        if close is None:
            log.warning(
                "Empty/malformed klines for {symbol} at {at}",
                symbol=symbol,
                at=at_utc.isoformat(),
            )
            continue
        result[asset] = close
    return result


async def fetch_current_bid_prices(
    binance_client: BinanceClient,
    *,
    symbols: Sequence[str],
    quote_asset: str,
) -> dict[str, Decimal]:
    """Bid-цены каждого символа сейчас (``/api/v3/ticker/bookTicker``)."""
    log = logger.bind(component="metrics.pnl")
    result: dict[str, Decimal] = {}
    quote_upper = quote_asset.upper()
    for raw in symbols:
        asset = raw.upper()
        symbol = f"{asset}{quote_upper}"
        try:
            ticker = await fetch_book_ticker(binance_client, symbol)
        except Exception as exc:  # noqa: BLE001 — изоляция отказа Binance
            log.warning(
                "Failed to fetch bid price for {symbol}: {err}",
                symbol=symbol,
                err=f"{type(exc).__name__}: {exc}",
            )
            continue
        if ticker.bid_price > 0:
            result[asset] = ticker.bid_price
    return result


# ---------- orchestrator: compute_pnl ----------


async def compute_pnl(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    binance_client: BinanceClient,
    user_id: int,
    quote_asset: str,
    symbols: Sequence[str],
    portfolio: PortfolioSnapshot | None = None,
) -> PnLReport:
    """Собрать :class:`PnLReport` для пользователя.

    Делает три вещи:

    1. Достаёт ``users.initial_capital_usdt`` и ``created_at``.
    2. Гарантирует :class:`PortfolioSnapshot` (если не передан — строит
       сам через :func:`build_portfolio_snapshot`).
    3. Запрашивает init- и current-цены и зовёт
       :func:`build_pnl_report`.

    Bid-цены, уже подсчитанные внутри ``portfolio`` для активов с
    ненулевым балансом, переиспользуются — не дёргаем Binance дважды.
    """
    async with session_factory() as session:
        user = await user_crud.get_by_id(session, user_id)
    if user is None:
        raise ValueError(f"User {user_id} not found")

    if portfolio is None:
        portfolio = await build_portfolio_snapshot(
            session_factory=session_factory,
            binance_client=binance_client,
            user_id=user_id,
            quote_asset=quote_asset,
            symbols=symbols,
        )

    upper_symbols = [s.upper() for s in symbols]
    current_prices: dict[str, Decimal] = {
        item.asset.upper(): item.bid_price
        for item in portfolio.items
        if item.bid_price is not None and item.asset.upper() in upper_symbols
    }
    missing = [s for s in upper_symbols if s not in current_prices]
    if missing:
        extra = await fetch_current_bid_prices(
            binance_client, symbols=missing, quote_asset=quote_asset
        )
        current_prices.update(extra)

    init_prices_raw = await fetch_init_prices(
        binance_client,
        symbols=upper_symbols,
        quote_asset=quote_asset,
        at=user.created_at,
    )
    init_prices: dict[str, Decimal] | None
    if len(init_prices_raw) == len(upper_symbols):
        init_prices = init_prices_raw
    else:
        init_prices = None

    return build_pnl_report(
        initial_capital_usdt=user.initial_capital_usdt,
        portfolio_value_usdt=portfolio.total_usdt,
        symbols=upper_symbols,
        init_prices=init_prices,
        current_prices=current_prices,
    )


# ---------- formatting ----------


def format_pnl_lines(report: PnLReport) -> list[str]:
    """Подготовить строки PnL для вставки в Telegram-сообщения.

    Используется в ``/stats`` — по одной строке на каждый показатель.
    Формат, согласно ``architecture.md`` §11.2:

    * ``PnL: +24.55 USDT (+2.46%)``
    * ``vs HOLD: +0.85%`` (либо «недоступно», если baseline не собран)
    """
    pnl_sign = "+" if report.pnl_usdt >= 0 else "−"
    pct_sign = "+" if report.pnl_pct >= 0 else "−"
    lines = [
        f"PnL: {pnl_sign}{_fmt_money(abs(report.pnl_usdt))} USDT "
        f"({pct_sign}{_fmt_pct(abs(report.pnl_pct))}%)"
    ]
    if report.delta_vs_hold_pct is None:
        lines.append("vs HOLD: недоступно (нет исторических цен)")
    else:
        delta = report.delta_vs_hold_pct
        delta_sign = "+" if delta >= 0 else "−"
        lines.append(f"vs HOLD: {delta_sign}{_fmt_pct(abs(delta))}%")
    return lines


def format_pnl_inline(report: PnLReport) -> str:
    """То же, что :func:`format_pnl_lines`, но одной строкой через ``|``.

    Используется в ``notify_pipeline_summary`` — там сводка PnL живёт
    на одной строке вместе с разделителем.
    """
    return " | ".join(format_pnl_lines(report))


# ---------- helpers ----------


def _parse_kline_close(payload: object) -> Decimal | None:
    """Достать ``close`` из первой свечи в ответе ``/api/v3/klines``."""
    if not isinstance(payload, list) or not payload:
        return None
    first = payload[0]
    if not isinstance(first, (list, tuple)) or len(first) < 5:
        return None
    try:
        return Decimal(str(first[4]))
    except (ValueError, TypeError):
        return None


def _fmt_money(value: Decimal) -> str:
    """2 знака после запятой — для USDT-сумм."""
    return f"{value:.2f}"


def _fmt_pct(value: Decimal) -> str:
    """2 знака после запятой — для процентов."""
    return f"{value:.2f}"


__all__ = [
    "HoldBaseline",
    "PnLReport",
    "build_pnl_report",
    "compute_pnl",
    "fetch_current_bid_prices",
    "fetch_init_prices",
    "format_pnl_inline",
    "format_pnl_lines",
]
