"""Метрики поверх кошельков и сделок (фаза 10).

Содержит расчёт PnL и ``delta_vs_hold_pct`` — единственная метрика
успеха MVP (см. ``architecture.md`` §13). Подробности — в
:mod:`app.services.metrics.pnl`.
"""

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
