# CLAUDE.md

> Единый рабочий контекст для Claude. Источники правды: `docs/idea.md` (постановка) → `docs/clarifications.md` (ответы по неоднозначностям) → `docs/architecture.md` (финальная инструкция). Если в этих файлах конфликт — приоритет у `architecture.md`.

---

## 1. Что это за проект

AI-Trader: команда из 3 LLM-агентов (PRICE, NEWS, TRADER) принимает решения BUY/SELL/HOLD по `BTCUSDT`, `ETHUSDT`, `TONUSDT`, исполняет **mock-сделки** против реальных данных Binance public API и считает честный PnL с учётом комиссии taker 0.10% и спреда из `bookTicker`. Уведомления и команды — через Telegram-бота (aiogram 3).

Реальные ордера не отправляем нигде (даже на testnet). Никаких фьючерсов, плеча, шортов, SL/TP в MVP.

---

## 2. Технологический стек

Python 3.12, Poetry, asyncio, httpx, SQLAlchemy 2.0 (async), Alembic, PostgreSQL 16 + pgvector, pydantic-settings, loguru, APScheduler (AsyncIOScheduler), aiogram 3, pytest + pytest-asyncio, Docker compose.

LLM — OpenRouter (по умолчанию `deepseek/deepseek-chat` для всех 3 агентов, embedding — `openai/text-embedding-3-small`, dim=1536). Модель каждого агента отдельно настраивается через `.env`.

---

## 3. Структура каталогов

```
app/
├── main.py            # запуск scheduler + telegram bot через asyncio.gather
├── config.py          # pydantic-settings, разбит по группам (DatabaseSettings, BinanceSettings, ...)
├── core/              # async engine + session factory (db.py), loguru config (logger.py)
├── models/            # SQLAlchemy ORM
├── crud/              # ВСЕ обращения к БД только отсюда
└── services/
    ├── binance/       # public API (client, exchange_info, prices)
    ├── news/          # coindesk, deduplicator
    ├── llm/           # openrouter (с LLMCallTracker), embeddings
    ├── mock_exchange/ # executor, fees
    ├── agents/        # price_agent, news_agent, trader_agent + prompts/*.md
    ├── pipeline/      # runner, crypto_step, scheduler
    ├── telegram/      # bot, handlers, notifier
    └── metrics/       # pnl.py
alembic/  docs/  scripts/  tests/
```

Подробное разложение — `docs/architecture.md` раздел 3.

---

## 4. Жёсткие правила разработки

- **Язык кода:** docstring (google-style), комментарии внутри функций, prompts агентов, тексты Telegram — **на русском**. Логи loguru — **на английском**. Идентификаторы — английские.
- **Логи:** loguru обязателен, но без шума — содержательные сообщения, обязательный `logger.bind(pipeline_run_id=..., asset=..., llm_call_id=...)` где применимо.
- **БД:** все `select`/`insert`/`update` — только через `app/crud/*`. Никакие сервисы и агенты не пишут SQL/ORM сами.
- **Конфиг:** `config.py` — несколько `BaseSettings`-классов с префиксами (`DB_`, `BINANCE_`, `OPENROUTER_`, `AGENT_`, `COINDESK_`, `TELEGRAM_`, `SCHEDULER_`, `TRADING_`, `LOG_`), композируются в один `Settings`. При любом изменении `config.py` синхронно обновлять `.env.example`.
- **SOLID:** для 3 криптовалют — общий интерфейс/конфиг, никакого копипаста с заменой констант. Каждая монета — параметр, не отдельный класс.
- **Тесты:** при добавлении/изменении кода — пишем/правим юнит-тесты в `tests/`. Список обязательных групп — `architecture.md` раздел 14.
- **Атомарность сделки:** запись `decision → transaction → wallet update` — в одной транзакции БД.
- **Миграции:** `alembic upgrade head` выполняется автоматически из `scripts/entrypoint.sh` при старте `app`.

---

## 5. Зафиксированные ключевые решения

Эти решения уже приняты в `clarifications.md` — не пересогласовываем без явного запроса пользователя.

1. **Капитал:** старт 100 000 RUB → при init конвертируется в USDT по курсу `USDTRUB` (Binance `bookTicker`, fallback CoinGecko). Внутри системы всё в USDT. RUB — только в UI Telegram (курс снимается заново на момент ответа).
2. **Биржа:** только Binance public API (`/api/v3/klines`, `/api/v3/ticker/bookTicker`, `/api/v3/exchangeInfo`). API-ключи не нужны.
3. **Комиссия:** taker 0.10% (`BINANCE_TAKER_FEE=0.001`), всегда списывается в USDT. Спред моделируется через `bookTicker` (ask на BUY, bid на SELL). Доп. slippage не добавляем.
4. **Фильтры биржи:** `exchangeInfo` тянется на старте и кэшируется. При нарушении `LOT_SIZE`/`MIN_NOTIONAL` сделка не исполняется, `decision.executed=false`, `not_executed_reason="MIN_NOTIONAL"` или `"LOT_SIZE"`.
5. **Действия AI:** только `BUY` / `SELL` / `HOLD`. На `BUY` агент задаёт `buy_fraction ∈ (0, 1]` от свободного USDT. `SELL` — продажа всей позиции по этой монете. SL/TP нет.
6. **Pipeline:** монеты обрабатываются **последовательно** (BTC → ETH → TON). Внутри одной монеты ветки PRICE и NEWS идут **параллельно** через `asyncio.gather`. Общий `pipeline_run_id` (uuid) на тик.
7. **Расписание:** `SCHEDULER_MODE=cron` (`SCHEDULER_CRON_TIMES=00:00,06:00,12:00,18:00`, CSV из `HH:MM` в UTC; внутри — `OrTrigger` из `CronTrigger`-ов, по одному на время) или `SCHEDULER_MODE=interval` + `SCHEDULER_INTERVAL_MINUTES=N` (если `SCHEDULER_RUN_ON_STARTUP=true` — тик сразу при старте). `max_instances=1`, `coalesce=true`. Расписание перечитывается на лету командой `/reload_schedule` — она читает `.env` напрямую (минуя `os.environ`); для этого в `docker-compose.yml` смонтирован `./.env:/app/.env:ro`.
8. **Таймауты:** LLM-вызов 60 сек, обработка одной монеты 300 сек. По таймауту шага монеты — `decision` пишется как `HOLD` + `executed=false` + `not_executed_reason="STEP_TIMEOUT"`, идём к следующей монете.
9. **Ретрай LLM:** 4 попытки, экспоненциальный backoff `1s / 3s / 9s / 27s` на 429, 5xx, `TimeoutException`, `ConnectError`. После провала — `llm_calls.status=ERROR`, монета пропускается, в Telegram уходит уведомление.
10. **Таймфреймы PRICE-агента:** `1m, 30m, 1h, 3h, 6h, 12h, 1d, 3d, 7d (=1w), 1M, 3M, 6M, 1Y, 3Y, 5Y`. `3h` агрегируется из 1h, `3M/6M/1Y/3Y/5Y` — из 1M. Агенту отдаём агрегированные числа (close, change_pct, min, max, volatility), **не сырые свечи**.
11. **Новости:** CoinDesk Data News API (`GET https://data-api.coindesk.com/news/v1/article/list?lang=EN&categories=BTC&limit=20`), ключ в HTTP-заголовке `Authorization: Apikey <KEY>`. До 20 статей за 24h на монету, английский. Дедупликация по `external_id` (UNIQUE). Эмбеддим `title + " " + summary_text`. RAG: cosine top-5, исключая последние 24h (`vector_cosine_ops`, IVFFlat `lists=100`). Историческая справка: до 2026-04-01 источником был CryptoPanic (free Developer API закрылся).
12. **NEWS-агент = 3 LLM-вызова:** (1) summary каждой новой новости, (2) повестка по всем 24h-summary, (3) финальный score после RAG по 5 историческим релевантным.
13. **TRADER-агент:** строгий JSON-ответ `{"action": "BUY|SELL|HOLD", "buy_fraction": 0.0–1.0, "reasoning": "..."}`. При парс-ошибке — ретрай с пометкой.
14. **Telegram:** двусторонний бот. Команды только от `telegram_id` из таблицы `users` — остальным `Not authorized`. Формат — Markdown + эмодзи (`📈 BUY`, `📉 SELL`, `⏸ HOLD`, `🪙`, `🔁`). Команды: `/start`, `/balance`, `/history [N]`, `/stats`, `/start_pipeline`, `/stop`, `/resume`, `/reload_schedule`.
15. **БД-модели:** `users`, `wallets` (кэш балансов), `transactions` (источник правды), `decisions`, `news` (vector(1536)), `llm_calls` (jsonb для request/response + status `IN_PROGRESS|COMPLETE|ERROR`). Поля минимально из варианта `a` соответствующих пунктов 8.x clarifications, расширяем по необходимости.
16. **Метрика успеха:** `delta_vs_hold_pct` против baseline «купил равные доли BTC/ETH/TON на старте и держим». Считается по запросу (для `/stats` и summary-сообщения), отдельная таблица не нужна.
17. **Промпты:** в `app/services/agents/prompts/*.md`, версионируются с кодом, шаблонизация — `str.Template` (простой выбор; уточняется в фазе 6).

---

## 6. Pipeline одной монеты (crypto_step)

```
1. asyncio.gather:
   PRICE branch:                    NEWS branch:
     fetch klines per timeframe       fetch CoinDesk Data articles (24h)
     aggregate metrics                для новых: NEWS summary + embedding → save_news
     PRICE-agent → price_summary     NEWS agenda (по всем 24h summary)
                                     RAG: cosine top-5 (exclude last 24h)
                                     NEWS final score
2. read wallet + last 12 decisions по этому asset
3. TRADER-agent → решение → сохранить decision (executed=None)
4. HOLD: executed=true
   BUY/SELL: mock_exchange.execute() → transaction + wallet update → executed=true
   фильтры не дают: executed=false + reason
5. notify_step (Telegram)
```

После всех монет — `notify_pipeline_summary` с PnL и `delta_vs_hold_pct`.

---

## 7. План реализации (фазы)

Каждая фаза — самодостаточный задел, после неё проект собирается / соответствующие тесты зелёные.

0. Скелет: `pyproject.toml`, Dockerfile, compose, `config.py` (DB+Logging), `core/db.py`, `core/logger.py`, alembic init, entrypoint.
1. Модели + CRUD + миграции (включая `CREATE EXTENSION vector` и IVFFlat). Unit-тесты CRUD на testcontainers postgres.
2. Binance клиент + mock-биржа + таймфрейм-агрегация. Тесты комиссий, lot/notional.
3. `scripts/init_user.py` (RUB→USDT, создание users + wallets).
4. LLM-сервис + `LLMCallTracker` + ретраи + таймауты + `embeddings`. Тесты с `FakeOpenRouterClient`.
5. CoinDesk Data News API + дедуп + сохранение новости с эмбеддингом + RAG-запрос.
6. Агенты (PRICE, NEWS×3, TRADER) + промпты + парсинг JSON.
7. Pipeline (crypto_step + runner) + таймауты на шаг.
8. Scheduler (cron/interval) + защита от перекрытий + флаг `paused` в БД.
9. Telegram bot (handlers, notifier) + авторизация.
10. Metrics (`pnl.py`) + `/stats` + summary-сообщение.
11. Полировка: `.env.example`, короткий README, прогон полного тика.

---

## 8. Definition of Done (MVP)

- `docker compose up` поднимает `db` + `app`, alembic мигрирует автоматически.
- `python scripts/init_user.py` создаёт пользователя с конвертированным капиталом.
- При `SCHEDULER_MODE=interval`, `SCHEDULER_INTERVAL_MINUTES=1` тик запускается, в Telegram приходят 3 step-уведомления + summary.
- Все 9 групп unit-тестов из `architecture.md` §14 — зелёные.
- В БД заполняются `decisions` (3/тик), `transactions` (для исполненных), `news` (за 24h), `llm_calls` (на каждый вызов).
- `/balance` показывает USDT и RUB-эквивалент. `/stats` показывает `delta_vs_hold_pct`.

---

## 9. Что НЕ фиксировано (решаем по ходу)

- Точный набор полей `users` / `wallets` / `transactions` / `llm_calls` — стартуем с минимального (вариант `a` в clarifications), расширяем при необходимости.
- Jinja2 vs `str.Template` — стартуем с `str.Template`, пересматриваем в фазе 6 при первой пробе.
- IVFFlat `lists` — стартуем со 100, тюним при >10k новостей.
- Точное имя DeepSeek-модели в OpenRouter — стартуем с `deepseek/deepseek-chat`, пользователь меняет через `.env`.

---

## 10. Чего НЕ делать

- Не добавлять реальные ордера (даже Binance testnet).
- Не вводить шорты, фьючерсы, плечо, SL/TP.
- Не писать SQL/ORM-запросы вне `app/crud/*`.
- Не плодить параллельные классы под каждую монету — общий интерфейс с параметром `asset`.
- Не использовать русский в логах loguru и в идентификаторах кода.
- Не оставлять «голые» LLM-вызовы — только через `LLMCallTracker` с записью в `llm_calls`.
- Не добавлять новые сервисы в `docker-compose.yml` без необходимости (redis, отдельный bot-контейнер и т.п. — оверкилл для MVP).
- Не применять миграции самому (`alembic upgrade`, `alembic downgrade`, `alembic revision --autogenerate` и т.п.) — миграции применяет пользователь вручную. Создавать файлы миграций можно, запускать их — нет.
- Не делать `git commit` / `git push` без прямой просьбы пользователя. Менять файлы — можно, фиксировать в историю — только по явной команде.
