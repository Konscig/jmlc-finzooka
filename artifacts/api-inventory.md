# API Inventory: Trading Agent

> **Версия:** 1.0
> **Дата:** 2026-03-14
> **Основано на:** USM v1.1, C4 v1.0, ER v1.0
> **Base URL:** `https://{server}/api/v1`

---

## Обзор

**Всего эндпоинтов:** 23
**Формат:** JSON
**Версионирование:** `/api/v1/`

### Аутентификация

| Потребитель | Механизм | Описание |
|-------------|----------|----------|
| Mini App | Telegram WebApp InitData | Валидация подписи `initData` через HMAC-SHA256 с использованием токена бота. Middleware проверяет `hash` параметр и извлекает `telegram_id` из данных. [Документация](https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app) |
| Bot | Telegram Bot API | Запросы от бота идут через внутреннюю сеть (Docker network). Авторизация по `X-Bot-Token` header |
| Админ-панель | API Key + IP Whitelist | Статический API-ключ в `Authorization: Bearer {key}`, доступ ограничен по IP (localhost / VPN). Ключ хранится в env vars |

### Группы
| Группа | Количество | Потребитель |
|--------|-----------|-------------|
| Users | 5 | Bot, Mini App |
| Tickers | 4 | Mini App, Bot |
| Signals | 3 | Mini App, Bot |
| Sentiment | 2 | Mini App |
| News | 1 | Mini App |
| Charts | 2 | Mini App |
| Admin | 6 | Админ-панель |

---

## 1. Users — Пользователи и настройки

### `POST /users/register`
> US-001: Онбординг через /start

| Параметр | Тип | Описание |
|----------|-----|----------|
| telegram_id | int | Telegram user ID |
| username | string? | Telegram username |

**Response 201:**
```json
{
  "id": 1,
  "telegram_id": 123456789,
  "username": "trader_ivan",
  "created_at": "2026-03-14T10:00:00Z"
}
```

---

### `GET /users/{telegram_id}`
> Получение профиля пользователя

**Response 200:**
```json
{
  "id": 1,
  "telegram_id": 123456789,
  "username": "trader_ivan",
  "notification_settings": {"triggers": ["sentiment_spike", "level_approach"], "interval": "on_event"},
  "tickers": ["SBER", "GAZP"],
  "strategy": {"is_confirmed": true, "profile": {...}},
  "created_at": "2026-03-14T10:00:00Z"
}
```

---

### `PUT /users/{telegram_id}/notifications`
> US-004: Настройка уведомлений

| Параметр | Тип | Описание |
|----------|-----|----------|
| triggers | string[] | Типы триггеров: `sentiment_spike`, `level_approach`, `important_news` |
| interval | string | `on_event` / `hourly` / `manual` |

**Response 200:** обновлённые настройки

---

### `PUT /users/{telegram_id}/strategy`
> US-003: Описание стратегии

| Параметр | Тип | Описание |
|----------|-----|----------|
| raw_description | string | Текстовое описание стратегии от пользователя |

**Response 200:**
```json
{
  "raw_description": "Торгую скальпинг на 15м, максимальный риск 2% на сделку...",
  "structured_profile": {
    "timeframe": "15m",
    "risk_per_trade": 0.02,
    "style": "scalping",
    "preferred_indicators": ["RSI", "MACD"]
  },
  "is_confirmed": false
}
```

---

### `PATCH /users/{telegram_id}/strategy/confirm`
> US-003: Подтверждение профиля стратегии

**Response 200:**
```json
{
  "is_confirmed": true,
  "structured_profile": {...}
}
```

---

### `DELETE /users/{telegram_id}`
> 152-ФЗ: Право пользователя на удаление своих данных

**Response 204:** пользователь и все связанные данные удалены

**Каскад:** удаляются `user_strategies`, `user_tickers` (согласно ER-модели)

**Note:** Доступен через бота (команда /delete) и Mini App (настройки). Требует подтверждения от пользователя перед выполнением.

---

## 2. Tickers — Тикеры

### `GET /tickers`
> US-002: Список доступных тикеров

| Query | Тип | Описание |
|-------|-----|----------|
| blue_chip_only | bool? | Только голубые фишки (default: true для MVP) |

**Response 200:**
```json
[
  {"id": 1, "symbol": "SBER", "name": "Сбербанк", "sector": "Финансы", "is_blue_chip": true},
  {"id": 2, "symbol": "GAZP", "name": "Газпром", "sector": "Нефтегаз", "is_blue_chip": true}
]
```

---

### `GET /tickers/{symbol}/card`
> US-005: Карточка (досье) тикера

**Response 200:**
```json
{
  "symbol": "SBER",
  "name": "Сбербанк",
  "sector": "Финансы",
  "is_blue_chip": true,

  "price": {
    "current": 285.50,
    "open": 286.20,
    "high": 287.10,
    "low": 284.30,
    "close_prev": 288.90
  },

  "growth": {
    "daily_percent": -1.18,
    "weekly_percent": -3.45,
    "monthly_percent": 2.10
  },

  "technical": {
    "volatility": 0.0284,
    "stability": 20.46,
    "sharpe_ratio": 0.049,
    "atr_14": 5.92,
    "true_range": 2.80,
    "high_low_spread": 2.80
  },

  "volume": {
    "current": 8500000,
    "avg_volume": 8098579
  },

  "sentiment": {
    "score": -0.15,
    "trend": "declining",
    "source_count": 42,
    "positive": 12,
    "negative": 22,
    "neutral": 8
  },

  "prediction": {
    "direction": "SELL",
    "target_price": 282.0,
    "confidence": 0.72,
    "mape": 3.45
  },

  "key_levels": {
    "support": 280.0,
    "resistance": 290.0,
    "stop_loss": 289.0,
    "take_profit": 278.0
  },

  "correlation": {
    "moex_spearman": 0.87
  },

  "latest_news": [
    {"title": "ЦБ повысил ставку...", "sentiment_impact": -0.3, "published_at": "..."}
  ],

  "last_signal": {
    "direction": "SELL",
    "entry": 286.0,
    "stop": 289.0,
    "take": 278.0,
    "generated_at": "2026-03-14T11:15:00Z"
  }
}
```
**Cache:** Redis, TTL 5 мин

**Маппинг на модель ARMAExo:**
| Поле в API | Переменная в модели | Описание |
|-----------|-------------------|----------|
| price.open/high/low | open, high, low | Цены текущей свечи |
| growth.daily_percent | daily_growth | Дневной рост % |
| growth.weekly_percent | weekly_growth | Недельный рост % |
| growth.monthly_percent | monthly_growth | Месячный рост % |
| technical.volatility | volatility (daily_growth_std) | Волатильность |
| technical.stability | stability (std/mean) | Стабильность |
| technical.sharpe_ratio | sharpe_ratio (mean/std) | Коэффициент Шарпа |
| technical.atr_14 | atr_14 | Average True Range (14 дней) |
| technical.true_range | true_range | True Range текущий |
| technical.high_low_spread | high_low | Спред high-low |
| volume.current | volume | Объём текущий |
| volume.avg_volume | avg_volume | Средний объём торгов |
| correlation.moex_spearman | spearman(close, moex_close) | Корреляция с индексом MOEX |

---

### `POST /users/{telegram_id}/tickers`
> US-002: Добавление тикера в избранное

| Параметр | Тип | Описание |
|----------|-----|----------|
| symbol | string | Тикер (SBER, GAZP...) |

**Response 201:** добавленный тикер
**Error 400:** превышен лимит (5 тикеров MVP)

---

### `DELETE /users/{telegram_id}/tickers/{symbol}`
> Удаление тикера из избранного

**Response 204**

---

## 3. Signals — Сигналы и рекомендации

### `GET /tickers/{symbol}/signals/latest`
> US-007, US-008, US-009: Последний сигнал по тикеру

**Response 200:**
```json
{
  "id": 42,
  "ticker": "SBER",
  "direction": "BUY",
  "entry_price": 283.50,
  "stop_loss": 280.00,
  "take_profit": 290.00,
  "confidence": 0.68,
  "explanation": "Рекомендация основана на: рост sentiment на 25% за последний час, отскок от уровня поддержки 280, позитивная новость о дивидендах.",
  "factors": {
    "sentiment_score": 0.35,
    "ta_rsi": 32,
    "ta_macd": "bullish_crossover",
    "news": "Дивиденды утверждены выше ожиданий"
  },
  "mape_at_generation": 3.45,
  "generated_at": "2026-03-14T11:15:00Z"
}
```

---

### `GET /tickers/{symbol}/signals`
> US-019 (v1.1): История сигналов

| Query | Тип | Описание |
|-------|-----|----------|
| limit | int? | Количество (default: 20) |
| offset | int? | Смещение |

**Response 200:**
```json
{
  "signals": [...],
  "stats": {"total": 150, "profitable": 87, "win_rate": 0.58}
}
```

---

### `POST /tickers/{symbol}/signals/generate`
> US-016: Запрос анализа по тикеру (по кнопке)

**Response 202:** `{"task_id": "abc123", "status": "processing"}`

**Response (async callback или polling):**
```json
{"task_id": "abc123", "status": "completed", "signal": {...}}
```
**Note:** Асинхронный — через Celery task, результат через polling или WebSocket

---

## 4. Sentiment

### `GET /tickers/{symbol}/sentiment`
> US-006, US-012: Текущий sentiment

| Query | Тип | Описание |
|-------|-----|----------|
| period | string? | `1h` / `4h` / `1d` (default: `1h`) |

**Response 200:**
```json
{
  "ticker": "GAZP",
  "current_score": -0.42,
  "trend": "declining",
  "history": [
    {"score": -0.15, "measured_at": "2026-03-14T10:00:00Z"},
    {"score": -0.42, "measured_at": "2026-03-14T11:00:00Z"}
  ],
  "sources": {
    "tpulse": {"score": -0.55, "count": 28},
    "news": {"score": -0.30, "count": 5},
    "telegram": {"score": -0.40, "count": 12}
  },
  "top_posts": [
    {"text": "Газпром отменяет дивиденды...", "source": "tpulse", "sentiment": -0.9}
  ]
}
```

---

### `GET /tickers/{symbol}/sentiment/history`
> Данные для графика sentiment

| Query | Тип | Описание |
|-------|-----|----------|
| from | datetime | Начало периода |
| to | datetime | Конец периода |
| granularity | string? | `15m` / `1h` / `1d` |

**Response 200:** массив `{score, measured_at}` для отрисовки на графике

---

## 5. News

### `GET /tickers/{symbol}/news`
> US-017 (v1.1): Новости по тикеру

| Query | Тип | Описание |
|-------|-----|----------|
| limit | int? | Количество (default: 10) |

**Response 200:**
```json
[
  {
    "title": "ЦБ повысил ключевую ставку до 22%",
    "summary": "Банк России повысил ставку...",
    "source_url": "https://...",
    "sentiment_impact": -0.3,
    "published_at": "2026-03-14T09:00:00Z"
  }
]
```

---

## 6. Charts — Данные для графиков

### `GET /tickers/{symbol}/ohlcv`
> US-010, US-013: Данные свечей для графика

| Query | Тип | Описание |
|-------|-----|----------|
| timeframe | string | `15m` / `30m` / `1h` |
| from | datetime | Начало периода |
| to | datetime? | Конец (default: now) |

**Response 200:**
```json
[
  {"t": "2026-03-14T10:00:00Z", "o": 283.0, "h": 285.5, "c": 284.2, "l": 282.5, "v": 150000},
  {"t": "2026-03-14T10:15:00Z", "o": 284.2, "h": 286.0, "c": 285.8, "l": 284.0, "v": 120000}
]
```
**Cache:** Redis, TTL зависит от timeframe

---

### `GET /tickers/{symbol}/prediction`
> US-013: Прогноз модели для overlay на графике

**Response 200:**
```json
{
  "ticker": "SBER",
  "timeframe": "15m",
  "predictions": [
    {"t": "2026-03-14T11:30:00Z", "price": 285.0, "lower": 283.0, "upper": 287.0},
    {"t": "2026-03-14T11:45:00Z", "price": 285.5, "lower": 282.5, "upper": 288.5}
  ],
  "model_mape": 3.45,
  "generated_at": "2026-03-14T11:15:00Z"
}
```
**Cache:** Redis, TTL 15 мин

---

## 7. Admin — Админ-панель

### `GET /admin/users`
> US-024: Список пользователей

| Query | Тип | Описание |
|-------|-----|----------|
| sort_by | string? | `last_active` / `created_at` |
| active_since | datetime? | Фильтр по активности |

**Response 200:**
```json
{
  "total": 47,
  "dau": 12,
  "active_monitorings": 35,
  "users": [
    {"telegram_id": 123, "username": "trader1", "tickers": ["SBER", "GAZP"], "last_active_at": "..."}
  ]
}
```

---

### `GET /admin/metrics/{symbol}`
> US-025: Метрики модели по тикеру

| Query | Тип | Описание |
|-------|-----|----------|
| period | string? | `7d` / `30d` / `90d` |

**Response 200:**
```json
{
  "ticker": "SBER",
  "current": {"mape": 3.45, "win_rate": 0.58, "avg_inference_ms": 8500},
  "trend": [
    {"date": "2026-03-13", "mape": 3.50, "win_rate": 0.55},
    {"date": "2026-03-14", "mape": 3.45, "win_rate": 0.58}
  ],
  "total_signals": 150,
  "profitable_signals": 87
}
```

---

### `GET /admin/metrics/overview`
> US-025: Сводка метрик по всем тикерам

**Response 200:** массив метрик по каждому активному тикеру

---

### `GET /admin/pipelines`
> US-026: Статус pipeline'ов

**Response 200:**
```json
[
  {"name": "data_collector", "status": "ok", "last_success_at": "2026-03-14T11:00:00Z", "last_error": null},
  {"name": "sentiment", "status": "warning", "last_success_at": "2026-03-14T10:30:00Z", "last_error": "Mistral API rate limit"},
  {"name": "ml_engine", "status": "ok", "last_success_at": "2026-03-14T11:15:00Z", "last_error": null}
]
```

---

### `GET /admin/signals/results`
> US-028 (v1.1): Результативность рекомендаций

| Query | Тип | Описание |
|-------|-----|----------|
| period | string? | `7d` / `30d` |
| ticker | string? | Фильтр по тикеру |

**Response 200:**
```json
{
  "period": "30d",
  "total_signals": 450,
  "profitable": 261,
  "win_rate": 0.58,
  "avg_pnl_percent": 1.2,
  "by_ticker": [
    {"symbol": "SBER", "signals": 120, "win_rate": 0.62},
    {"symbol": "GAZP", "signals": 95, "win_rate": 0.53}
  ]
}
```

---

### `POST /admin/model/retrain/{symbol}`
> US-029 (v1.1): Запуск переобучения

**Response 202:**
```json
{"task_id": "retrain_sber_001", "status": "queued", "ticker": "SBER"}
```

---

## Error Responses

Все эндпоинты возвращают ошибки в едином формате:

```json
{
  "error": {
    "code": "TICKER_LIMIT_EXCEEDED",
    "message": "Максимум 5 тикеров на пользователя (MVP)",
    "details": {}
  }
}
```

### Коды ошибок

| HTTP | Код | Описание | Когда |
|------|-----|----------|-------|
| 400 | BAD_REQUEST | Невалидные параметры запроса | Неверный формат данных, отсутствие обязательных полей |
| 400 | TICKER_LIMIT_EXCEEDED | Превышен лимит тикеров | `POST /users/{id}/tickers` при 5 тикерах |
| 400 | TICKER_NOT_AVAILABLE | Тикер недоступен | Попытка добавить тикер с `is_active = false` |
| 401 | UNAUTHORIZED | Не пройдена аутентификация | Невалидный InitData / API Key / Bot Token |
| 403 | FORBIDDEN | Нет доступа | Попытка доступа к `/admin/*` без прав |
| 404 | NOT_FOUND | Ресурс не найден | Несуществующий telegram_id, symbol |
| 409 | CONFLICT | Конфликт | Повторная регистрация существующего telegram_id |
| 429 | RATE_LIMITED | Превышен лимит запросов | Слишком частые вызовы `POST /signals/generate` |
| 500 | INTERNAL_ERROR | Внутренняя ошибка сервера | Сбой БД, Redis, внешнего API |
| 502 | UPSTREAM_ERROR | Ошибка внешнего сервиса | Недоступен T-Invest API, Mistral API |
| 503 | SERVICE_UNAVAILABLE | Сервис недоступен | ML Engine перегружен, Celery worker недоступен |

---

## Сводка: Маппинг USM → API

| User Story | Эндпоинт(ы) |
|-----------|-------------|
| US-001 Онбординг | `POST /users/register` |
| US-002 Выбор тикеров | `GET /tickers`, `POST /users/{id}/tickers`, `DELETE /users/{id}/tickers/{symbol}` |
| US-003 Стратегия | `PUT /users/{id}/strategy`, `PATCH /users/{id}/strategy/confirm` |
| US-003 Подтверждение | `PATCH /users/{id}/strategy/confirm` |
| US-004 Уведомления | `PUT /users/{id}/notifications` |
| US-005 Карточка тикера | `GET /tickers/{symbol}/card` |
| US-006 Sentiment | `GET /tickers/{symbol}/sentiment` |
| US-007/008 Сигналы | `GET /tickers/{symbol}/signals/latest` |
| US-009 Объяснение | Поле `explanation` в сигнале |
| US-010 График | `GET /tickers/{symbol}/ohlcv` |
| US-012 Sentiment-индикатор | `GET /tickers/{symbol}/sentiment/history` |
| US-013 Прогноз | `GET /tickers/{symbol}/prediction` |
| US-015/016 Аналитика в чате | `GET /tickers/{symbol}/card` (через Bot) |
| US-017 Новости | `GET /tickers/{symbol}/news` |
| US-019 История сигналов | `GET /tickers/{symbol}/signals` |
| US-024 Пользователи (админ) | `GET /admin/users` |
| US-025 Метрики (админ) | `GET /admin/metrics/{symbol}`, `GET /admin/metrics/overview` |
| US-026 Pipeline (админ) | `GET /admin/pipelines` |
| US-028 Результаты (админ) | `GET /admin/signals/results` |
| US-029 Переобучение (админ) | `POST /admin/model/retrain/{symbol}` |
| 152-ФЗ Удаление данных | `DELETE /users/{id}` |
| US-011 Стоп/тейк на графике | Поля `stop_loss`, `take_profit` в `GET /signals/latest` |
| US-014 Редирект в терминал | Клиентская логика (deep link `tinkoffinvest://trade?symbol={SYMBOL}`) |
