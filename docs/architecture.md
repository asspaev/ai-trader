# AI-Trader — финальная инструкция реализации

> Документ — единый источник правды для разработки. Все решения, основанные на `docs/idea.md` + ответах в `docs/clarifications.md`, зафиксированы здесь. Если в коде возникает развилка — сверяемся с этим файлом, а не с idea.md.

---

## 1. Контекст и цели

**Цель.** AI-команда из 3 агентов (PRICE, NEWS, TRADER) принимает решения «купить/продать/держать» по 3 криптовалютам, исполняет mock-сделки против реальных рыночных данных Binance и фиксирует честный PnL с учётом комиссий и спреда.

**Что НЕ делаем в MVP.**
- Фьючерсы / плечо / шорты.
- Стоп-лосс / тейк-профит.
- Реальные ордера на бирже (даже на testnet).
- Бэктест по историческим данным.
- Множественные пользователи (один профиль из `init`).

**Что включено в MVP.**
- Pipeline по 4 раза в сутки (или N минут — режим `.env`).
- 3 пары: `BTCUSDT`, `ETHUSDT`, `TONUSDT`.
- Mock-сделки с учётом комиссии taker 0.10% и спреда из `bookTicker`.
- 3 LLM-агента + 1 embedding-модель (DeepSeek по умолчанию через OpenRouter).
- pgvector + RAG-поиск по новостям (источник: CoinDesk Data News API).
- Telegram-бот (write + команды).
- Метрика `delta_vs_hold_pct`.

---

## 2. Технологический стек

| Компонент | Версия / решение |
|---|---|
| Python | 3.12 |
| Менеджер пакетов | Poetry, lock-файл в репо |
| Async | asyncio |
| Web-клиент | httpx (async) |
| DB | PostgreSQL 16 + pgvector |
| ORM | SQLAlchemy 2.0 (async) |
| Миграции | Alembic |
| Settings | pydantic-settings |
| Логи | loguru (английский, с `loguru.bind` контекстом) |
| Планировщик | APScheduler (AsyncIOScheduler) |
| Telegram | aiogram 3 |
| LLM | OpenRouter HTTP API (любая совместимая модель) |
| Эмбеддинги | OpenAI-совместимый эндпоинт через OpenRouter, размерность 1536 |
| Тесты | pytest, pytest-asyncio, фикстуры в JSON |
| Контейнер | Docker + docker compose |

---

## 3. Структура каталогов

```
.
├── .env
├── .env.example
├── docker-compose.yml
├── Dockerfile
├── pyproject.toml
├── poetry.lock
├── alembic.ini
├── docs/
│   ├── idea.md
│   ├── clarifications.md
│   ├── architecture.md      ← этот файл
│   └── other/
├── scripts/
│   ├── init_user.py         ← разовое заполнение init-записи
│   └── entrypoint.sh        ← alembic upgrade head && python -m app.main
├── tests/
│   ├── conftest.py
│   ├── fixtures/            ← JSON-снимки API
│   └── unit/
└── app/
    ├── __init__.py
    ├── main.py              ← запуск: scheduler + telegram bot через asyncio.gather
    ├── config.py            ← pydantic-settings, разбит на классы
    ├── core/                ← общая инфраструктура (БД, логирование)
    │   ├── __init__.py
    │   ├── db.py            ← async engine + session factory
    │   └── logger.py        ← loguru configuration
    ├── alembic/             ← миграции (script_location = app/alembic в alembic.ini)
    │   ├── env.py
    │   └── versions/
    ├── models/              ← SQLAlchemy ORM
    │   ├── __init__.py
    │   ├── base.py
    │   ├── user.py          ← init
    │   ├── wallet.py
    │   ├── transaction.py
    │   ├── decision.py
    │   ├── news.py
    │   └── llm_call.py
    ├── crud/                ← все обращения к БД ТОЛЬКО отсюда
    │   ├── __init__.py
    │   ├── user.py
    │   ├── wallet.py
    │   ├── transaction.py
    │   ├── decision.py
    │   ├── news.py
    │   └── llm_call.py
    └── services/
        ├── __init__.py
        ├── binance/         ← public API клиент
        │   ├── client.py
        │   ├── exchange_info.py
        │   └── prices.py
        ├── news/
        │   ├── coindesk.py
        │   └── deduplicator.py
        ├── llm/
        │   ├── openrouter.py     ← обёртка с записью LLMCall
        │   └── embeddings.py
        ├── mock_exchange/
        │   ├── executor.py       ← BUY/SELL с комиссией и спредом
        │   └── fees.py
        ├── agents/
        │   ├── base.py
        │   ├── price_agent.py
        │   ├── news_agent.py
        │   ├── trader_agent.py
        │   └── prompts/
        │       ├── price_summary.md
        │       ├── news_summary.md
        │       ├── news_agenda.md
        │       ├── news_final_score.md
        │       └── trader_decision.md
        ├── pipeline/
        │   ├── runner.py         ← оркестрация одного тика
        │   ├── crypto_step.py    ← обработка одной монеты
        │   └── scheduler.py      ← APScheduler конфиг
        ├── telegram/
        │   ├── bot.py
        │   ├── handlers.py
        │   └── notifier.py       ← публичный API: notify_step / notify_trade / notify_summary
        └── metrics/
            └── pnl.py            ← PnL и delta_vs_hold
```

**Правило:** все взаимодействия с БД — через `app/crud/*`. Никакие сервисы не пишут `select`/`insert` сами.

**Соглашение по структуре `app/`.** Внутри `app/` модулями верхнего уровня остаются только `main.py` и `config.py`. Всё остальное — пакеты (`core/`, `models/`, `crud/`, `services/`, `alembic/`). Это упрощает навигацию и делает границы слоёв явными.

---

## 4. Конфигурация (.env)

`config.py` разбит на классы, каждый — отдельный `BaseSettings` с префиксом.

```python
# Группы:
class DatabaseSettings(BaseSettings):   # DB_*
class BinanceSettings(BaseSettings):    # BINANCE_*
class OpenRouterSettings(BaseSettings): # OPENROUTER_*
class AgentModelsSettings(BaseSettings):# AGENT_*  (модели per-agent)
class CoinDeskNewsSettings(BaseSettings):# COINDESK_*
class TelegramSettings(BaseSettings):   # TELEGRAM_*
class SchedulerSettings(BaseSettings):  # SCHEDULER_*
class TradingSettings(BaseSettings):    # TRADING_*
class LoggingSettings(BaseSettings):    # LOG_*

class Settings:  # композиция всех групп
    db: DatabaseSettings
    binance: BinanceSettings
    ...
```

### `.env.example` (полный список переменных)

```dotenv
# --- Database ---
DB_HOST=db
DB_PORT=5432
DB_NAME=ai_trader
DB_USER=ai_trader
DB_PASSWORD=change_me

# --- Binance (public API only, без ключей) ---
BINANCE_BASE_URL=https://api.binance.com
BINANCE_TAKER_FEE=0.001          # 0.10%

# --- OpenRouter ---
OPENROUTER_API_KEY=
OPENROUTER_BASE_URL=https://openrouter.ai/api/v1
OPENROUTER_TIMEOUT_SECONDS=60
OPENROUTER_MAX_RETRIES=4
OPENROUTER_RETRY_BACKOFF_BASE=3  # 1s, 3s, 9s, 27s

# --- Модели по умолчанию (DeepSeek через OpenRouter) ---
AGENT_PRICE_MODEL=deepseek/deepseek-chat
AGENT_NEWS_MODEL=deepseek/deepseek-chat
AGENT_TRADER_MODEL=deepseek/deepseek-chat
AGENT_EMBEDDING_MODEL=openai/text-embedding-3-small
AGENT_EMBEDDING_DIM=1536

# --- CoinDesk Data News API (новости) ---
# Бесплатный ключ — на data.coindesk.com (free-план). Передаётся в
# HTTP-заголовке `Authorization: Apikey <KEY>`.
COINDESK_API_KEY=
COINDESK_BASE_URL=https://data-api.coindesk.com
COINDESK_LANGUAGE=EN
COINDESK_NEWS_LIMIT_PER_CRYPTO=20

# --- Telegram ---
TELEGRAM_BOT_TOKEN=

# --- Scheduler ---
SCHEDULER_MODE=cron               # cron | interval
SCHEDULER_CRON_HOURS=0,6,12,18    # UTC, через запятую
SCHEDULER_INTERVAL_MINUTES=30
SCHEDULER_RUN_ON_STARTUP=true     # для interval-режима

# --- Trading ---
TRADING_INITIAL_CAPITAL_RUB=100000
TRADING_SYMBOLS=BTC,ETH,TON
TRADING_QUOTE_ASSET=USDT
TRADING_DECISIONS_HISTORY_LIMIT=12
TRADING_RAG_TOP_K=5
TRADING_RAG_EXCLUDE_LAST_HOURS=24
TRADING_PIPELINE_STEP_TIMEOUT_SECONDS=300

# --- Logging ---
LOG_LEVEL=INFO
LOG_DIR=./logs
```

---

## 5. Схема БД

### 5.1 `users` (init)

| Поле | Тип | Примечание |
|---|---|---|
| id | int PK | |
| telegram_id | bigint UNIQUE | |
| username | varchar(64) NULL | |
| initial_capital_rub | numeric(18, 2) | старт-капитал в RUB |
| initial_capital_usdt | numeric(18, 8) | конвертированный в USDT на момент init |
| initial_usdt_rub_rate | numeric(18, 8) | курс USDT/RUB на момент init |
| created_at | timestamptz | default now() |

> Одна запись. Создаётся `scripts/init_user.py`: спрашивает telegram_id/username, тянет текущий `USDTRUB` (Binance `bookTicker`, fallback CoinGecko), пишет запись.

### 5.2 `wallets`

| Поле | Тип |
|---|---|
| id | int PK |
| user_id | int FK users |
| asset | varchar(10) (`USDT`, `BTC`, `ETH`, `TON`) |
| balance | numeric(28, 12) |
| updated_at | timestamptz |

Unique index: `(user_id, asset)`. Кошелёк — кэш-сущность. Источник правды — `transactions`.

### 5.3 `transactions`

| Поле | Тип |
|---|---|
| id | int PK |
| user_id | int FK |
| decision_id | int FK decisions NULL |
| symbol | varchar(20) (`BTCUSDT` …) |
| asset | varchar(10) (`BTC`, `ETH`, `TON`) |
| action | enum(`BUY`, `SELL`) |
| amount_crypto | numeric(28, 12) |
| price_usdt | numeric(28, 8) (фактическая, с учётом спреда) |
| gross_usdt | numeric(28, 8) (amount × price) |
| fee_usdt | numeric(28, 8) |
| net_usdt | numeric(28, 8) (gross ± fee, фактическое изменение USDT-кошелька) |
| usdt_balance_after | numeric(28, 8) |
| asset_balance_after | numeric(28, 12) |
| created_at | timestamptz |

Index: `(user_id, asset, created_at desc)`.

### 5.4 `decisions`

| Поле | Тип |
|---|---|
| id | int PK |
| user_id | int FK |
| pipeline_run_id | uuid (общий для всех монет одного тика) |
| asset | varchar(10) |
| action | enum(`BUY`, `SELL`, `HOLD`) |
| buy_fraction | numeric(5, 4) NULL (доля свободного USDT 0–1, только для BUY) |
| executed | bool |
| not_executed_reason | varchar(128) NULL (например, `MIN_NOTIONAL`, `EMPTY_POSITION`) |
| price_summary | text |
| news_score | text |
| reasoning | text |
| created_at | timestamptz |

Index: `(user_id, asset, created_at desc)` (для «12 предыдущих по монете»).

### 5.5 `news`

| Поле | Тип |
|---|---|
| id | int PK |
| asset | varchar(10) |
| external_id | varchar(128) (id из CoinDesk Data) |
| url | varchar(512) UNIQUE |
| title | varchar(512) |
| source | varchar(128) |
| published_at | timestamptz |
| raw_text | text NULL |
| summary_text | text (на русском, от NEWS-агента) |
| embedding | vector(1536) |
| created_at | timestamptz |

Indexes:
- ivfflat по `embedding` (`vector_cosine_ops`, lists=100).
- `(asset, published_at desc)`.
- `(asset, external_id)` UNIQUE.

### 5.6 `llm_calls`

| Поле | Тип |
|---|---|
| id | int PK |
| pipeline_run_id | uuid NULL |
| agent_name | varchar(64) (`price` / `news_summary` / `news_agenda` / `news_score` / `trader` / `embedding`) |
| model | varchar(128) |
| status | enum(`IN_PROGRESS`, `COMPLETE`, `ERROR`) |
| prompt_tokens | int NULL |
| completion_tokens | int NULL |
| cost_usd | numeric(12, 6) NULL |
| request_payload | jsonb |
| response_payload | jsonb NULL |
| error_text | text NULL |
| created_at | timestamptz |
| finished_at | timestamptz NULL |

Index: `(agent_name, created_at desc)`, `(pipeline_run_id)`.

---

## 6. Mock-биржа: правила исполнения сделок

**Источник цены при сделке.** `GET /api/v3/ticker/bookTicker?symbol={SYMBOL}` → берём `askPrice` (buy) или `bidPrice` (sell). Это автоматически зашивает спред в финальную цену.

**Комиссия.** `fee = gross_usdt * BINANCE_TAKER_FEE`. Считается в USDT и всегда списывается с USDT-кошелька (упрощение, но соответствует defaults Binance Spot без BNB).

**Формулы.**
- BUY: AI даёт долю `f ∈ (0, 1]` от свободного USDT.
  ```
  gross_usdt   = free_usdt * f
  fee_usdt     = gross_usdt * fee_rate
  spend_usdt   = gross_usdt + fee_usdt
  if spend_usdt > free_usdt:
      gross_usdt = free_usdt / (1 + fee_rate)
      fee_usdt   = free_usdt - gross_usdt
      spend_usdt = free_usdt
  amount_crypto = gross_usdt / ask_price
  → проверка MIN_NOTIONAL / LOT_SIZE / stepSize (округление вниз до stepSize).
  ```
- SELL: продаём весь актив.
  ```
  amount_crypto = current_asset_balance (округлённый вниз до stepSize)
  gross_usdt    = amount_crypto * bid_price
  fee_usdt      = gross_usdt * fee_rate
  net_usdt      = gross_usdt - fee_usdt
  ```

**Фильтры биржи.** При старте `app` тянем `GET /api/v3/exchangeInfo` для 3 символов и кэшируем `stepSize`, `tickSize`, `minNotional`. Если `gross_usdt < minNotional` или `amount_crypto < stepSize` → сделка не исполняется, `decision.executed=false`, `not_executed_reason="MIN_NOTIONAL"` / `"LOT_SIZE"`.

**Атомарность.** Запись в БД (decision → transaction → wallet update) — в одной транзакции БД.

---

## 7. Внешние интеграции

### 7.1 Binance public

| Эндпоинт | Назначение | Кэш |
|---|---|---|
| `GET /api/v3/exchangeInfo?symbols=...` | фильтры lot/notional | при старте, в памяти |
| `GET /api/v3/klines?symbol=...&interval=...&limit=...` | свечи для PRICE-агента | на тик pipeline |
| `GET /api/v3/ticker/bookTicker?symbol=...` | bid/ask на момент сделки | per-trade |

Клиент — `httpx.AsyncClient`, общий keep-alive. Без API-ключа.

**Таймфреймы PRICE-агента (с учётом ограничений Binance).**

| Метрика для агента | Источник Binance | Способ |
|---|---|---|
| 1m, 30m, 1h, 6h, 12h, 1d, 3d | `klines` нативно | прямой запрос (limit ≈ 30–50) |
| 3h | агрегация из `1h` ×3 | на стороне приложения |
| 7d (=1w) | `klines interval=1w` | прямой запрос |
| 1M | `klines interval=1M` | прямой запрос |
| 3M, 6M, 1Y | агрегация из `1M` (3/6/12) | в приложении |
| 3Y, 5Y | агрегация из `1M` (36/60) | в приложении |

Для каждого периода считаем: `close_now`, `change_pct`, `min`, `max`, `volatility (stddev of pct returns)`.

### 7.2 CoinDesk Data News API

> Historical note: до 2026-04-01 источником был CryptoPanic (free Developer API). После закрытия его бесплатного плана перешли на CoinDesk Data (бывший CryptoCompare) — у него и формат ответа, и таксономия категорий по тикерам совместимы с нашим контрактом `NewsPost`.

`GET https://data-api.coindesk.com/news/v1/article/list?lang=EN&categories=BTC&limit=20` → top-N статей по активу. Ключ в HTTP-заголовке `Authorization: Apikey <KEY>`. Релевантные поля ответа: `ID`, `TITLE`, `URL`, `BODY`, `PUBLISHED_ON` (Unix-timestamp, секунды), `SOURCE_DATA.NAME`, `CATEGORY_DATA`.

Дедупликация: по `external_id` (= `ID` статьи, UNIQUE на пару `(asset, external_id)`). Если новость уже есть → не зовём embedding/summary повторно.

### 7.3 OpenRouter

POST `/v1/chat/completions` для chat-моделей. POST `/v1/embeddings` для эмбеддингов.

**Каждый вызов оборачивается классом `LLMCallTracker`:**

```python
async def call(agent_name, model, payload):
    record = await crud.llm_call.create(IN_PROGRESS, payload)
    try:
        with timeout(60s):
            for attempt in 1..MAX_RETRIES:
                try: response = await client.post(...); break
                except RetryableError: await sleep(backoff(attempt))
        await crud.llm_call.complete(record.id, response, usage)
        return response
    except Exception as e:
        await crud.llm_call.error(record.id, str(e))
        raise
```

Ретрай-условия: HTTP 429 / 5xx / `httpx.TimeoutException` / `httpx.ConnectError`.

---

## 8. AI-агенты

Каждый агент = класс с методом `async run(context) -> AgentOutput`. Промпт читается из `.md` файла, шаблонизация — Python `str.format` или Jinja2 (выбрать одно; в коде взять `str.Template` для простоты).

### 8.1 PRICE Agent — `price_summary.md`

**Вход:** агрегированные числа по всем периодам (см. таблицу выше) для одной монеты.
**Задача:** написать `price_summary` — текст на 4–8 предложений: краткосрочный тренд, долгосрочный тренд, аномалии.
**Выход:** `{"summary": "...", "sentiment": "bullish|bearish|neutral"}` (JSON в response через function calling или строгий парсинг).

### 8.2 NEWS Agent (3 вызова)

1. **`news_summary.md`** — для КАЖДОЙ свежей новости (за 24h):
   - вход: `title + raw_text`,
   - выход: `summary_text` (краткое содержание + потенциальное влияние на цену), `sentiment ∈ {bullish, bearish, neutral}`.
   - Только для новых (не дублей по `external_id`).
2. **`news_agenda.md`** — на всех summary за 24h:
   - вход: список summary,
   - выход: 1–3 главных тематик («ETF approval», «regulation», …).
3. **`news_final_score.md`** — после RAG (5 релевантных историч. новостей) + текущие 24h summary:
   - вход: текущая повестка + 5 исторических summary + их sentiment + что было с ценой,
   - выход: `news_score` (текст 4–8 предложений + общий sentiment).

### 8.3 EMBEDDING-сервис

Эмбеддим **`title + " " + summary_text`** через `openai/text-embedding-3-small`. Сохраняем в `news.embedding`.

### 8.4 TRADER Agent — `trader_decision.md`

**Вход:**
- `price_summary` + sentiment,
- `news_score` + sentiment,
- текущий кошелёк (USDT + позиция по этой монете),
- последние 12 решений по этой монете (action + executed + price_at_decision + price_now).

**Выход (строгий JSON):**
```json
{
  "action": "BUY|SELL|HOLD",
  "buy_fraction": 0.25,  // обязательно если action=BUY, диапазон (0,1]
  "reasoning": "..."     // 3–6 предложений на русском
}
```

Парсинг — через `json.loads(...)`, при ошибке — ретрай с пометкой «JSON parse failed».

### 8.5 Промпты — общие правила

- На русском.
- Заканчиваются явной инструкцией про формат ответа (JSON-блок).
- Системный месседж: «Ты — финансовый аналитик. Без воды, без markdown в JSON, без advisor-дисклеймеров».

---

## 9. Pipeline — пошагово

```
pipeline_run_id = uuid4()
for asset in [BTC, ETH, TON]:           # последовательно
    crypto_step(asset, pipeline_run_id)
notify_pipeline_summary(pipeline_run_id) # после всех монет
```

### Внутри `crypto_step(asset)`:

```
1. PRICE и NEWS ветки — параллельно (asyncio.gather):

   PRICE branch:
     a. fetch klines per timeframe → aggregate metrics
     b. PRICE-agent → price_summary

   NEWS branch:
     a. fetch CoinDesk Data articles за 24h
     b. для новых: NEWS-agent summary + embedding → save_news (one transaction per news)
     c. NEWS-agent agenda (по всем summary за 24h)
     d. RAG: cosine top-5 (исключая последние 24h)
     e. NEWS-agent final score

2. После завершения обеих веток:
   - read wallet
   - read last 12 decisions по этому asset
   - TRADER-agent → решение
   - сохранить decision (executed=null пока)

3. Выполнить действие:
   - HOLD → executed=true, ничего не делаем
   - BUY / SELL → mock_exchange.execute()
     → создать transaction
     → обновить wallet
     → пометить decision.executed
   - Если фильтры биржи не дают исполнить → decision.executed=false + reason

4. Telegram-уведомление по этой монете (notify_step) — что было сделано.
```

### Таймауты

- На вызов OpenRouter: 60 сек (см. `OPENROUTER_TIMEOUT_SECONDS`).
- На обработку одной монеты: 300 сек (`TRADING_PIPELINE_STEP_TIMEOUT_SECONDS`). По таймауту: пишем `decision` с `action=HOLD`, `executed=false`, `not_executed_reason="STEP_TIMEOUT"`, переходим к следующей монете.

---

## 10. Планировщик

`SCHEDULER_MODE=cron`:
- `AsyncIOScheduler.add_job(pipeline_runner, "cron", hour="0,6,12,18", timezone="UTC")`.

`SCHEDULER_MODE=interval`:
- `AsyncIOScheduler.add_job(pipeline_runner, "interval", minutes=N)`.
- Если `SCHEDULER_RUN_ON_STARTUP=true` — запуск первого тика сразу при старте сервиса.

Защита от перекрытий: `max_instances=1`, `coalesce=true`. Если предыдущий тик не закончился — следующий пропускается.

---

## 11. Telegram-бот

### 11.1 Команды (только от `telegram_id` из `users`)

| Команда | Действие |
|---|---|
| `/start` | приветствие, проверка авторизации |
| `/balance` | USDT + RUB-эквивалент по текущему `USDTRUB`, разбивка по активам, общая стоимость портфеля в USDT и в RUB |
| `/history [N]` | последние N (default 10) транзакций |
| `/stats` | PnL %, абсолютный USDT, `delta_vs_hold_pct`, число решений по типам |
| `/start_pipeline` | форс-запуск pipeline вне расписания |
| `/stop` | гасит планировщик (флаг в БД, можно `/resume`) |
| `/resume` | возобновляет планировщик |

Неавторизованным — `"Not authorized"` и игнор.

### 11.2 Уведомления (publish-only)

`notify_step(asset, decision, transaction)` — после каждой монеты в pipeline:
```
🪙 BTC
Решение: 📈 BUY 25% свободного USDT
Цена: 67 432.12 USDT (ask)
Куплено: 0.00371 BTC за 250.40 USDT (комиссия 0.25 USDT)
Баланс USDT: 749.35 → 498.95
Обоснование: <reasoning от TRADER>
```

`notify_pipeline_summary(pipeline_run_id)` — в конце:
```
🔁 Pipeline #abcdef завершён за 2 мин 14 сек
Решений: BUY×1, SELL×0, HOLD×2
Портфель: 1024.55 USDT (≈ 102 455 RUB)
PnL: +24.55 USDT (+2.46%) | vs HOLD: +0.85%
```

Формат — Markdown, эмодзи: `📈 BUY`, `📉 SELL`, `⏸ HOLD`, `🪙` (актив), `🔁` (pipeline).

### 11.3 Обработка ошибок

При любой ошибке шага агента — `notify_step` всё равно отправляется с описанием ошибки и «решение отложено».

---

## 12. Логирование

- `loguru` пишет в stdout + ротирующийся файл (`LOG_DIR/app.log`, 10 MB × 7).
- Каждый pipeline-тик: `logger.bind(pipeline_run_id=..., asset=...)`.
- Каждый LLM-вызов: `logger.bind(llm_call_id=..., agent=..., model=...)`.
- Уровень `INFO` по умолчанию, `DEBUG` для содержимого запросов/ответов.

---

## 13. Метрики (`services/metrics/pnl.py`)

```python
async def compute_pnl(user_id) -> PnLReport:
    # текущая стоимость портфеля = USDT + Σ asset_balance × bid_price
    # baseline = "купил равные доли BTC/ETH/TON при init на initial_capital_usdt"
    # delta_vs_hold_pct = (portfolio_value - hold_value) / initial * 100
    ...
```

Считается «по запросу» (для `/stats` и `notify_pipeline_summary`). В отдельную таблицу не сохраняем — выводимо из транзакций.

---

## 14. Тесты

### Unit (обязательно для MVP):

| Что | Модуль |
|---|---|
| Расчёт fee и net при BUY/SELL | `services/mock_exchange/fees.py` |
| Округление `stepSize`, проверка `minNotional` | `services/mock_exchange/executor.py` |
| Атомарность: транзакция + wallet update | mocked DB session |
| Дедупликация новостей | `services/news/deduplicator.py` |
| CRUD всех моделей (in-memory SQLite не подходит из-за pgvector → testcontainers postgres) | `tests/unit/crud/` |
| Парсинг ответа TRADER (валидный JSON / битый JSON / неизвестное action) | `services/agents/trader_agent.py` |
| Состояние LLMCall: IN_PROGRESS → COMPLETE / ERROR | `services/llm/openrouter.py` |
| Маппинг и агрегация таймфреймов | `services/binance/prices.py` |
| Конвертация RUB → USDT при init | `scripts/init_user.py` |

### Фикстуры

- `tests/fixtures/binance_klines_*.json` — снимки разных таймфреймов.
- `tests/fixtures/coindesk_news_articles.json`.
- `tests/fixtures/openrouter_chat_response.json`, `..._embedding_response.json`.
- LLM-клиент в тестах подменяется `FakeOpenRouterClient`, возвращающим фикстуры.

### Запуск

```
docker compose -f docker-compose.test.yml run --rm tests
```

или локально:

```
poetry run pytest -q
```

---

## 15. Docker

### `docker-compose.yml`

```yaml
services:
  db:
    image: pgvector/pgvector:pg16
    environment:
      POSTGRES_DB: ${DB_NAME}
      POSTGRES_USER: ${DB_USER}
      POSTGRES_PASSWORD: ${DB_PASSWORD}
    volumes:
      - db_data:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U ${DB_USER} -d ${DB_NAME}"]
      interval: 5s
      retries: 10

  app:
    build: .
    depends_on:
      db: { condition: service_healthy }
    env_file: .env
    command: ["./scripts/entrypoint.sh"]
    volumes:
      - ./logs:/app/logs

volumes:
  db_data:
```

`scripts/entrypoint.sh`:
```bash
#!/bin/sh
set -e
alembic upgrade head
exec python -m app.main
```

### `Dockerfile`
- `python:3.12-slim`
- Установка `poetry`, `poetry install --no-root --without dev`.
- COPY всего проекта, `WORKDIR /app`.

---

## 16. План реализации (incremental, по фазам)

> Каждая фаза — отдельный задел, после которого приложение должно запускаться (или тесты соответствующей фазы — проходить).

### Фаза 0 — скелет
- `pyproject.toml`, базовые зависимости.
- `Dockerfile`, `docker-compose.yml`.
- `app/config.py` (только Database + Logging).
- `app/core/db.py`, `app/core/logger.py`.
- `alembic` init, пустая первая миграция.
- `scripts/entrypoint.sh`.

### Фаза 1 — модели и CRUD
- Все ORM-модели + миграции (включая `CREATE EXTENSION vector`, IVFFlat-индекс).
- CRUD-модули для всех моделей.
- Unit-тесты CRUD с testcontainers postgres.

### Фаза 2 — Binance клиент + Mock-биржа
- `services/binance/client.py`, `exchange_info.py`, `prices.py`.
- Маппинг таймфреймов + агрегация.
- `services/mock_exchange/executor.py`, `fees.py`.
- Unit-тесты комиссий, lot size, min notional.

### Фаза 3 — Скрипт инициализации
- `scripts/init_user.py`: запрос telegram_id, USDTRUB-курс, конвертация, создание `users` + начальный `wallets` (USDT = `initial_capital_usdt`, остальные = 0).
- Unit-тест конвертации.

### Фаза 4 — LLM-сервис
- `services/llm/openrouter.py` с `LLMCallTracker`.
- `services/llm/embeddings.py`.
- Ретрай, таймауты, запись в `llm_calls`.
- Unit-тесты со `FakeOpenRouterClient` (фикстуры).

### Фаза 5 — Новости + RAG
- `services/news/coindesk.py`, `deduplicator.py`.
- Сохранение новости + эмбеддинг.
- RAG-запрос (cosine top-5, exclude last 24h).
- Unit-тесты дедупа и RAG-выборки.

### Фаза 6 — Агенты
- `services/agents/base.py`.
- Файлы промптов в `prompts/`.
- `PriceAgent`, `NewsAgent` (3 вызова), `TraderAgent`.
- Парсинг JSON ответа TRADER.
- Unit-тесты парсинга и сценариев ошибок.

### Фаза 7 — Pipeline
- `services/pipeline/crypto_step.py` (параллельные ветки PRICE/NEWS внутри одной монеты).
- `services/pipeline/runner.py` (последовательно по 3 монетам, общий `pipeline_run_id`).
- Таймауты на step.

### Фаза 8 — Scheduler
- `services/pipeline/scheduler.py`.
- Режимы cron / interval через `.env`.
- Защита от перекрытий.
- Флаг `paused` в БД для `/stop` `/resume`.

### Фаза 9 — Telegram
- `services/telegram/bot.py`, `handlers.py`, `notifier.py`.
- Команды + авторизация по `telegram_id`.
- Уведомления `notify_step` / `notify_pipeline_summary`.

### Фаза 10 — Метрики
- `services/metrics/pnl.py`.
- `/stats` и summary-сообщение.

### Фаза 11 — Полировка
- Финальный `.env.example`.
- `README.md` короткий: запуск + init.
- Прогон полного тика на dev-окружении.

---

## 17. Зафиксированные «пограничные» решения

1. **USDT — внутренняя валюта, RUB — только UI.** Старт-капитал 100 000 RUB → конвертация в USDT при инициализации, далее всё в USDT. Курс USDT/RUB снимается на момент инициализации и каждый раз заново при формировании Telegram-ответа.
2. **Binance pair set:** `BTCUSDT`, `ETHUSDT`, `TONUSDT`. RUB-пары не используем (нет ликвидности).
3. **Спред моделируется через `bookTicker`** (ask на BUY, bid на SELL). Без дополнительного slippage поверх.
4. **Только public Binance API**, без ключей и без testnet-ордеров.
5. **AI выбирает долю `buy_fraction` ∈ (0, 1]** только при BUY. SELL = вся позиция. HOLD = ничего.
6. **DeepSeek — модель по умолчанию** для всех 3 агентов (можно менять через `.env`). Embedding — `openai/text-embedding-3-small`.
7. **Ретрай LLM: 4 попытки, backoff 1/3/9/27 сек.** Затем `ERROR`, шаг монеты пропускается, в Telegram уходит уведомление.
8. **Таймфреймы**: нативно из Binance + агрегация на стороне приложения для 3h, 3M, 6M, 1Y, 3Y, 5Y. PRICE-агент получает агрегированные числа, не сырые свечи.
9. **Pipeline по монетам — последовательно**, внутри одной монеты PRICE и NEWS ветки идут параллельно через `asyncio.gather`.
10. **Один процесс** для pipeline + Telegram-бота (через `asyncio.gather` в `app.main`).
11. **Миграции alembic запускаются автоматически** при старте контейнера через `entrypoint.sh`.
12. **Логи loguru — английский**, всё прочее (docstring, комментарии, промпты, Telegram-уведомления) — русский.

---

## 18. Что НЕ зафиксировано и решается «по мере разработки»

(Это явно разрешено ответами на 8.1/8.2/8.3/8.5 — минимально из варианта `a`, при необходимости расширяем.)

- Дополнительные поля в `users` / `wallets` / `transactions` / `llm_calls` — добавляем, если в процессе становится понятно, что нужны.
- Точная подмножественная стратегия Jinja vs `str.Template` для промптов — выбор делается в фазе 6 после первой пробы.
- Размер IVFFlat-индекса (`lists`) — стартуем со `100`, тюним когда новостей станет >10k.
- Точное имя модели DeepSeek в OpenRouter — стартуем с `deepseek/deepseek-chat`, пользователь меняет через `.env` на актуальное.

---

## 19. Definition of Done для MVP

- `docker compose up` поднимает БД + app, alembic мигрирует.
- `python scripts/init_user.py` создаёт пользователя с конвертированным капиталом.
- В режиме `SCHEDULER_MODE=interval` `SCHEDULER_INTERVAL_MINUTES=1` запускается тик, в Telegram приходят 3 step-уведомления + summary.
- Все 9 групп unit-тестов из раздела 14 — зелёные.
- В БД есть записи: `decisions` (3 на тик), `transactions` (только для исполненных), `news` (за последние 24h), `llm_calls` (для каждого вызова).
- `/balance` показывает корректные числа в USDT и RUB.
- `/stats` показывает `delta_vs_hold_pct`.
