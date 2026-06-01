# Комиссии бирж + бесплатные API для цен

## Спот-комиссии (базовый уровень, без VIP)

| Биржа | Maker | Taker | Скидка за нативный токен | Минимум для VIP-уровней | Комиссия вывода |
|---|---|---|---|---|---|
| **Binance** | 0.10% | 0.10% | −25% при оплате BNB → 0.075% | от $1M объёма / 30 дней | зависит от сети, BTC ~0.0002 BTC |
| **Coinbase Advanced** | 0.40% | 0.60% | нет | от $10k объёма / 30 дней | сетевая + спред |
| **Kraken** | 0.16% | 0.26% | нет | от $50k объёма / 30 дней | фиксированная по монете |
| **Bybit** | 0.10% | 0.10% | нет на споте напрямую | от $1M объёма / 30 дней | фиксированная по сети |
| **OKX** | 0.08% | 0.10% | −20% при оплате OKB | от $10M активов или объёма | фиксированная по сети |
| **Bitstamp** | 0.40% | 0.40% | нет | от $1k объёма / 30 дней | фикс. по монете |
| **KuCoin** | 0.10% | 0.10% | −20% при оплате KCS | от $50k объёма / 30 дней | фикс. по сети |
| **Gate.io** | 0.20% | 0.20% | −20% при оплате GT | от $300k объёма / 30 дней | фикс. по сети |
| **MEXC** | 0.00% | 0.05% | — | — | фикс. по сети |
| **Bitget** | 0.10% | 0.10% | — | от $5M объёма / 30 дней | фикс. по сети |

> Maker — кто добавляет ликвидность (лимитный ордер «висит»). Taker — кто забирает её (рыночный ордер). Подробнее — в [teor.md](teor.md).

## Фьючерсы (perpetuals) — обычно дешевле спота

| Биржа | Maker | Taker |
|---|---|---|
| Binance Futures | 0.02% | 0.05% |
| Bybit | 0.02% | 0.055% |
| OKX | 0.02% | 0.05% |
| Bitget | 0.02% | 0.06% |
| MEXC | 0.00% | 0.02% |

Плюс **funding rate** каждые 8 часов (обычно ±0.01%, но в экстремумах до ±0.75%).

## Бесплатные API для цен (REST + WebSocket)

| Биржа | REST endpoint цены | WebSocket | Ключ нужен? | Лимит REST |
|---|---|---|---|---|
| **Binance** | `GET api.binance.com/api/v3/ticker/price?symbol=BTCUSDT` | `wss://stream.binance.com:9443/ws/btcusdt@trade` | нет | 6000 weight/min |
| **Coinbase** | `GET api.coinbase.com/v2/prices/BTC-USD/spot` | `wss://advanced-trade-ws.coinbase.com` | нет (public) | 10k req/час |
| **Kraken** | `GET api.kraken.com/0/public/Ticker?pair=XBTUSD` | `wss://ws.kraken.com` | нет | ~1 req/sec |
| **Bybit** | `GET api.bybit.com/v5/market/tickers?category=spot&symbol=BTCUSDT` | `wss://stream.bybit.com/v5/public/spot` | нет | 120 req/sec |
| **OKX** | `GET www.okx.com/api/v5/market/ticker?instId=BTC-USDT` | `wss://ws.okx.com:8443/ws/v5/public` | нет | 20 req / 2 sec |
| **Bitstamp** | `GET www.bitstamp.net/api/v2/ticker/btcusd/` | `wss://ws.bitstamp.net` | нет | 8000 req / 10 мин |
| **KuCoin** | `GET api.kucoin.com/api/v1/market/orderbook/level1?symbol=BTC-USDT` | да (нужен token из REST) | нет на public | 30 req / 3 sec |
| **Gate.io** | `GET api.gateio.ws/api/v4/spot/tickers?currency_pair=BTC_USDT` | `wss://api.gateio.ws/ws/v4/` | нет | 200 req / 10 sec |
| **MEXC** | `GET api.mexc.com/api/v3/ticker/price?symbol=BTCUSDT` | `wss://wbs.mexc.com/ws` | нет | 20 req/sec |
| **Bitget** | `GET api.bitget.com/api/v2/spot/market/tickers?symbol=BTCUSDT` | `wss://ws.bitget.com/v2/ws/public` | нет | 20 req/sec |

## Что выбрать новичку

- **Самые низкие комиссии при малом объёме**: MEXC (taker 0.05%), Binance с BNB (0.075%), OKX (0.08%).
- **Лучшая ликвидность (узкий спред, меньше проскальзывания)**: Binance, Bybit, OKX, Coinbase.
- **Самый удобный API для бота**: Binance — лучшая документация, много готовых SDK, стабильный WebSocket.
- **Цена комиссии для одной сделки туда-обратно**: 2 × taker. Например на Binance это 0.2% (или 0.15% с BNB). Минимальное движение, при котором ты в нулях.

> Цифры актуальны на 2026-06 и могут меняться. Перед боевым запуском бота — сверяйся с официальной страницей комиссий конкретной биржи.

## Связанные файлы

- [teor.md](teor.md) — как трейдеры зарабатывают и где теряют
- [api-table.md](api-table.md) — больше API (включая агрегаторы — CoinGecko, CoinCap и др.)
