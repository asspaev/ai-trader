# Бесплатные API для цен криптовалют

| API | Endpoint (пример) | Ключ | Лимиты free tier | WebSocket | Покрытие |
|---|---|---|---|---|---|
| **Binance** | `GET api.binance.com/api/v3/ticker/price?symbol=BTCUSDT` | не нужен | 1200 req/min по весу (≈ 6000 simple req/min), 6000 weight/min | да (`wss://stream.binance.com:9443`) | только листинг Binance |
| **Coinbase** | `GET api.coinbase.com/v2/prices/BTC-USD/spot` | не нужен | 10 000 req/час на IP | да (Advanced Trade WS) | листинг Coinbase |
| **Kraken** | `GET api.kraken.com/0/public/Ticker?pair=XBTUSD` | не нужен | ~1 req/sec на public endpoints | да | листинг Kraken |
| **Bybit** | `GET api.bybit.com/v5/market/tickers?category=spot&symbol=BTCUSDT` | не нужен | 120 req/sec на IP | да | листинг Bybit |
| **OKX** | `GET www.okx.com/api/v5/market/ticker?instId=BTC-USDT` | не нужен | 20 req/2sec на эндпоинт | да | листинг OKX |
| **Bitstamp** | `GET www.bitstamp.net/api/v2/ticker/btcusd/` | не нужен | 8000 req/10min | да | листинг Bitstamp |
| **CoinGecko** | `GET api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd` | не нужен (Demo — опционально) | 10–30 req/min, ~10k calls/мес | нет | 14 000+ монет, агрегатор |
| **CoinCap** | `GET api.coincap.io/v2/assets/bitcoin` | не нужен | 200 req/min без ключа, 500 req/min с ключом | да (`wss://ws.coincap.io/prices`) | агрегатор |
| **CoinPaprika** | `GET api.coinpaprika.com/v1/tickers/btc-bitcoin` | не нужен | 25 000 calls/мес | нет (на free) | агрегатор |
| **CryptoCompare** | `GET min-api.cryptocompare.com/data/price?fsym=BTC&tsyms=USD` | нужен (бесплатный) | 100 000 calls/мес | да | агрегатор |
| **CoinMarketCap** | `GET pro-api.coinmarketcap.com/v1/cryptocurrency/quotes/latest` | нужен (бесплатный) | 10 000 calls/мес, 333/день | нет (на free) | агрегатор |

## Рекомендации

- **Для торгового бота**: бери API той биржи, где торгуешь — цена совпадёт с исполнением. Используй WebSocket вместо REST-поллинга.
- **Для аналитики / нескольких бирж**: CoinGecko или CoinCap — агрегированная цена, не привязанная к одной площадке.
- **Для исторических данных**: CryptoCompare или Binance Klines (`/api/v3/klines`).

> Лимиты актуальны на момент составления (2026-06) и могут меняться — перед продом сверяйся с официальной докой.
