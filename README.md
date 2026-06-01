# AI-Trader

Команда из 3 LLM-агентов (**PRICE**, **NEWS**, **TRADER**) принимает решения
BUY / SELL / HOLD по `BTCUSDT`, `ETHUSDT`, `TONUSDT` и исполняет **mock-сделки**
против реальных рыночных данных Binance public API. Уведомления и команды —
через Telegram-бота. Реальные ордера не отправляются (даже на testnet).

Подробности: [`docs/architecture.md`](docs/architecture.md).
Жёсткие правила разработки: [`CLAUDE.md`](CLAUDE.md).

---

## Что нужно для запуска

* Docker + Docker Compose (рекомендуемый путь) **или** Python 3.12 + Poetry для
  локального запуска.
* Telegram bot-token (получить у [@BotFather](https://t.me/BotFather)) и свой
  `telegram_id` (получить у [@userinfobot](https://t.me/userinfobot)).
* API-ключи для:
  * [OpenRouter](https://openrouter.ai/) — LLM + embeddings.
  * [CryptoPanic](https://cryptopanic.com/developers/api/) — новости.

Binance public API ключей **не требует**.

---

## Запуск через Docker Compose

```bash
# 1) Конфиг
cp .env.example .env
# отредактируй .env: DB_PASSWORD, OPENROUTER_API_KEY, CRYPTOPANIC_API_KEY,
# TELEGRAM_BOT_TOKEN. Остальное можно оставить по умолчанию.

# 2) Сборка и старт БД + приложения (alembic upgrade head запускается из
#    scripts/entrypoint.sh автоматически).
docker compose up -d --build

# 3) Инициализация init-пользователя (одноразово).
#    Скрипт спросит telegram_id, username и стартовый капитал в RUB,
#    конвертирует его в USDT по текущему курсу и создаст запись users +
#    кошельки (USDT = стартовый капитал, BTC/ETH/TON = 0).
docker compose exec app python -m scripts.init_user

# 4) Логи
docker compose logs -f app
```

После этого приложение поднимет Telegram-бота и планировщик. По умолчанию
расписание — cron `0,6,12,18` UTC (4 раза в сутки).

### Быстрый dev-прогон одного тика

В `.env` поставь:

```dotenv
SCHEDULER_MODE=interval
SCHEDULER_INTERVAL_MINUTES=1
SCHEDULER_RUN_ON_STARTUP=true
```

и перезапусти `app`: `docker compose up -d --force-recreate app`. Первый тик
стартует сразу после старта сервиса; в Telegram придут 3 уведомления по монетам
+ итог по pipeline.

---

## Локальный запуск без Docker

```bash
# Зависимости
poetry install

# Подними PostgreSQL 16 с pgvector — например, из compose:
docker compose up -d db

# В .env поменяй DB_HOST=db → DB_HOST=localhost

# Миграции и инициализация
poetry run alembic upgrade head
poetry run python -m scripts.init_user

# Запуск
poetry run python -m app.main
```

---

## Команды Telegram

Доступны только тому `telegram_id`, который указан в `users` (одна init-запись).
Всем остальным бот отвечает `Not authorized`.

| Команда            | Что делает                                                          |
| ------------------ | ------------------------------------------------------------------- |
| `/start`           | Приветствие и проверка авторизации.                                 |
| `/balance`         | Балансы по активам, общая стоимость портфеля в USDT и RUB.          |
| `/history [N]`     | Последние N транзакций (default 10, max — `TELEGRAM_HISTORY_LIMIT_MAX`). |
| `/stats`           | PnL %, абсолютный USDT и `delta_vs_hold_pct` vs baseline.            |
| `/start_pipeline`  | Принудительно запустить один тик pipeline вне расписания.            |
| `/stop`            | Поставить планировщик на паузу (флаг хранится в БД).                 |
| `/resume`          | Снять паузу.                                                         |

---

## Тесты

```bash
poetry run pytest -q
```

Часть тестов CRUD поднимает временный PostgreSQL через `testcontainers` — нужен
запущенный Docker. Группы тестов соответствуют разделу 14
`docs/architecture.md`.

---

## Архитектура — TL;DR

```
APScheduler tick → pipeline.runner
  for asset in [BTC, ETH, TON]:        # последовательно
    crypto_step(asset):
      asyncio.gather(
        PRICE branch  →  klines × таймфреймы → PriceAgent,
        NEWS  branch  →  CryptoPanic → NewsAgent×3 (+ RAG по pgvector),
      )
      TraderAgent → решение (JSON)
      mock_exchange.execute (если BUY/SELL) — учёт спреда (bookTicker) и
                                                taker-комиссии 0.10%
      сохранение decision + transaction + wallet (одна БД-транзакция)
      Telegram notify_step
  Telegram notify_pipeline_summary (PnL + delta_vs_hold_pct)
```

Все БД-операции — только через `app/crud/*`. Все LLM-вызовы — только через
`LLMCallTracker` с записью в `llm_calls`.
