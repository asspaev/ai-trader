"""Mock-биржа: чистые формулы fee/spread и атомарное исполнение сделки в БД.

В MVP реальных ордеров нет (даже на testnet). Здесь только модель
исполнения: цена берётся из ``bookTicker`` (ask на BUY, bid на SELL),
комиссия — taker 0.10% от gross в USDT, фильтры биржи (LOT_SIZE,
MIN_NOTIONAL) проверяются жёстко.
"""

from app.services.mock_exchange.executor import (
    ExecutionResult,
    NotExecutedReason,
    execute_decision,
)
from app.services.mock_exchange.fees import (
    BuyQuote,
    SellQuote,
    quote_buy,
    quote_sell,
)


__all__ = [
    "BuyQuote",
    "SellQuote",
    "quote_buy",
    "quote_sell",
    "ExecutionResult",
    "NotExecutedReason",
    "execute_decision",
]
