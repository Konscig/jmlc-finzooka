# Sequence Diagrams: Trading Agent

> **Версия:** 1.0
> **Дата:** 2026-03-14
> **Основано на:** USM v1.1, C4 v1.0, API Inventory v1.0

---

## 1. Онбординг пользователя

> US-001, US-002, US-003

```mermaid
sequenceDiagram
    actor Trader as Трейдер
    participant TG as Telegram
    participant Bot as Telegram Bot
    participant API as Backend API
    participant DB as PostgreSQL
    participant LLM as Mistral API

    Trader->>TG: /start
    TG->>Bot: Webhook: /start command
    Bot->>API: POST /users/register {telegram_id, username}
    API->>DB: INSERT INTO users
    DB-->>API: user created
    API-->>Bot: 201 Created
    Bot-->>TG: "Привет! Я AI-советник для трейдинга. Выбери тикеры:"
    TG-->>Trader: Сообщение + inline-кнопки тикеров

    Trader->>TG: Нажимает [SBER] [GAZP]
    TG->>Bot: Callback: выбор тикеров
    Bot->>API: POST /users/{id}/tickers {symbol: "SBER"}
    API->>DB: INSERT INTO user_tickers
    Bot->>API: POST /users/{id}/tickers {symbol: "GAZP"}
    API->>DB: INSERT INTO user_tickers
    Bot-->>TG: "Отлично! Опиши свою стратегию торговли:"
    TG-->>Trader: Запрос стратегии

    Trader->>TG: "Торгую скальпинг на 15м, риск 2%..."
    TG->>Bot: Текст стратегии
    Bot->>API: PUT /users/{id}/strategy {raw_description}
    API->>LLM: "Формализуй стратегию: ..."
    LLM-->>API: structured_profile JSON
    API->>DB: UPDATE user_strategies
    API-->>Bot: structured_profile
    Bot-->>TG: "Твой профиль: скальпинг, 15м, риск 2%. Верно?"
    TG-->>Trader: Профиль + кнопки [Да] [Изменить]

    Trader->>TG: [Да]
    TG->>Bot: Callback: confirm
    Bot->>API: PATCH /users/{id}/strategy/confirm
    API->>DB: UPDATE is_confirmed = true
    Bot-->>TG: "Готово! Открой мини-приложение для графиков 📊"
    TG-->>Trader: Кнопка Mini App
```

---

## 2. Получение рекомендации (по запросу)

> US-007, US-008, US-009, US-016

```mermaid
sequenceDiagram
    actor Trader as Трейдер
    participant TG as Telegram
    participant Bot as Telegram Bot
    participant API as Backend API
    participant Redis as Redis Cache
    participant ML as ML Engine
    participant DB as PostgreSQL

    Trader->>TG: "Анализ SBER"
    TG->>Bot: Текст команды
    Bot->>API: POST /tickers/SBER/signals/generate
    API->>Redis: GET prediction:SBER (кэш?)

    alt Кэш актуален (< 15 мин)
        Redis-->>API: cached signal
        API-->>Bot: signal data
    else Кэш устарел
        API->>ML: generate_signal("SBER")
        ML->>Redis: GET ohlcv:SBER:15m
        Redis-->>ML: OHLCV данные
        ML->>Redis: GET sentiment:latest:SBER
        Redis-->>ML: sentiment score
        ML->>ML: Прогноз + расчёт стоп/тейк
        ML->>DB: INSERT INTO signals
        ML->>Redis: SET prediction:SBER (TTL 15m)
        ML-->>API: signal data
        API-->>Bot: signal data
    end

    Bot-->>TG: "📊 SBER | BUY\n💰 Вход: 283.50\n🛑 Стоп: 280.00\n✅ Тейк: 290.00\n📝 Основано на: sentiment +25%, отскок от поддержки 280, дивиденды."
    TG-->>Trader: Сигнал + кнопка [Открыть график]
```

---

## 3. Просмотр графика в Mini App

> US-010, US-011, US-012, US-013

```mermaid
sequenceDiagram
    actor Trader as Трейдер
    participant MA as Mini App
    participant API as Backend API
    participant Redis as Redis Cache

    Trader->>MA: Открывает Mini App (вкладка SBER)

    par Параллельные запросы
        MA->>API: GET /tickers/SBER/ohlcv?timeframe=15m
        API->>Redis: GET ohlcv:SBER:15m
        Redis-->>API: OHLCV данные
        API-->>MA: свечи [{t, o, h, c, l, v}, ...]
    and
        MA->>API: GET /tickers/SBER/signals/latest
        API-->>MA: signal {stop_loss, take_profit, entry, ...}
    and
        MA->>API: GET /tickers/SBER/sentiment
        API->>Redis: GET sentiment:latest:SBER
        Redis-->>API: sentiment data
        API-->>MA: sentiment {score, trend, history}
    and
        MA->>API: GET /tickers/SBER/prediction
        API->>Redis: GET prediction:SBER
        Redis-->>API: prediction data
        API-->>MA: predictions [{t, price, lower, upper}, ...]
    end

    MA->>MA: Рендер графика + overlay
    Note over MA: Свечи + стоп (красная) + тейк (зелёная) + прогноз (пунктир) + sentiment-шкала
    MA-->>Trader: Интерактивный график

    Trader->>MA: Переключает на вкладку GAZP
    MA->>API: (повторяет запросы для GAZP)
```

---

## 4. Фоновый сбор данных и генерация сигналов

> SS-001, SS-002, SS-003

```mermaid
sequenceDiagram
    participant Sched as Celery Scheduler
    participant DC as Data Collector
    participant TInvest as T-Invest API
    participant News as Новостные каналы
    participant TPulse as Т-Пульс
    participant Redis as Redis Cache
    participant SP as Sentiment Pipeline
    participant LLM as Mistral API
    participant ML as ML Engine
    participant DB as PostgreSQL
    participant Bot as Telegram Bot
    participant TG as Telegram

    Note over Sched: Каждые 5 минут (торговая сессия)

    Sched->>DC: task: collect_market_data
    par Сбор данных
        DC->>TInvest: GetCandles(SBER, 15m)
        TInvest-->>DC: OHLCV свечи
        DC->>Redis: SET ohlcv:SBER:15m
    and
        DC->>News: Парсинг Headlines, каналов
        News-->>DC: новые посты
        DC->>DB: INSERT INTO news_events
    and
        DC->>TPulse: Парсинг постов по тикерам
        TPulse-->>DC: посты
        DC->>Redis: сырые тексты в очередь
    end
    DC->>DB: UPDATE pipeline_statuses (data_collector: ok)

    Sched->>SP: task: analyze_sentiment
    SP->>Redis: GET сырые тексты
    SP->>LLM: "Оцени sentiment: [тексты]"
    LLM-->>SP: sentiment scores
    SP->>DB: INSERT INTO sentiment_scores
    SP->>Redis: SET sentiment:latest:SBER
    SP->>DB: UPDATE pipeline_statuses (sentiment: ok)

    Sched->>ML: task: generate_signals
    loop Для каждого активного тикера
        ML->>Redis: GET ohlcv, sentiment
        ML->>ML: Прогноз + стоп/тейк
        ML->>DB: INSERT INTO signals
        ML->>Redis: SET prediction:{ticker}
    end
    ML->>DB: UPDATE pipeline_statuses (ml_engine: ok)

    Note over Sched: Проверка триггеров уведомлений
    Sched->>DB: SELECT users с активными триггерами
    Sched->>Redis: GET sentiment changes

    alt Sentiment резко изменился (> порога)
        Sched->>Bot: send_notification(user_id, signal)
        Bot->>TG: Push: "⚠️ GAZP: sentiment упал на 40%! Рекомендация: SELL"
        TG-->>TG: Уведомление трейдеру
    end
```

---

## 5. Push-уведомление по триггеру

> US-004, SS-003

```mermaid
sequenceDiagram
    participant Sched as Celery Scheduler
    participant API as Backend API
    participant DB as PostgreSQL
    participant Redis as Redis Cache
    participant Bot as Telegram Bot
    participant TG as Telegram
    actor Trader as Трейдер

    Note over Sched: Каждую минуту: проверка триггеров

    Sched->>Redis: GET sentiment:latest:SBER
    Redis-->>Sched: score = -0.65 (было -0.25)

    Sched->>Sched: Δsentiment = 0.40 > порог 0.30
    Sched->>DB: SELECT users WHERE ticker=SBER AND trigger=sentiment_spike
    DB-->>Sched: [user_1, user_2, user_3]

    loop Для каждого пользователя
        Sched->>Bot: send_alert(telegram_id, alert_data)
        Bot->>TG: "⚠️ SBER: sentiment резко упал (-40%)\n📊 Текущий: -0.65 (негатив)\n💡 Причина: новость о повышении ставки ЦБ\n🔗 Открыть график"
        TG-->>Trader: Push-уведомление
    end
```

---

## 6. Редирект в терминал брокера

> US-014

```mermaid
sequenceDiagram
    actor Trader as Трейдер
    participant MA as Mini App
    participant TBank as Приложение Т-Банка

    Trader->>MA: Нажимает "Открыть в терминале" (SBER)
    MA->>MA: Формирует deep link: tinkoffinvest://trade?symbol=SBER
    MA->>TBank: Deep Link redirect
    TBank-->>Trader: Открывается тикер SBER в терминале
    Note over Trader: Трейдер совершает сделку вручную
    Note over MA: Формат deep link: tinkoffinvest://trade?symbol={SYMBOL}<br/>Требует проверки актуального формата в документации Т-Банка
```

---

## 7. Админ-панель: мониторинг

> US-024, US-025, US-026, US-027

```mermaid
sequenceDiagram
    actor Admin as Администратор
    participant Panel as Админ-панель
    participant API as Backend API
    participant DB as PostgreSQL

    Admin->>Panel: Открывает дашборд

    par Параллельные запросы
        Panel->>API: GET /admin/users
        API->>DB: SELECT users + tickers
        API-->>Panel: users list + DAU stats
    and
        Panel->>API: GET /admin/metrics/overview
        API->>DB: SELECT model_metrics
        API-->>Panel: MAPE, win_rate по тикерам
    and
        Panel->>API: GET /admin/pipelines
        API->>DB: SELECT pipeline_statuses
        API-->>Panel: statuses [{name, status, last_success}]
    end

    Panel-->>Admin: Дашборд: пользователи, метрики, pipeline'ы

    Note over Admin: Замечает деградацию MAPE по GAZP

    Admin->>Panel: Drill-down: GAZP метрики
    Panel->>API: GET /admin/metrics/GAZP?period=30d
    API-->>Panel: тренд MAPE за 30 дней
    Panel-->>Admin: График: MAPE растёт последние 5 дней
```

---

## 8. Алерт администратору при сбое

> US-027

```mermaid
sequenceDiagram
    participant Sched as Celery Scheduler
    participant API as Backend API
    participant DB as PostgreSQL
    participant Bot as Telegram Bot
    participant TG as Telegram
    actor Admin as Администратор

    Note over Sched: Каждую минуту: healthcheck pipeline'ов

    Sched->>DB: SELECT pipeline_statuses WHERE updated_at < NOW() - INTERVAL '10 min'
    DB-->>Sched: stale pipelines: [data_collector]

    Sched->>DB: UPDATE pipeline_statuses SET status = 'error' WHERE name = 'data_collector'

    Sched->>API: GET /admin/metrics/overview (проверка аномалий)
    API->>DB: SELECT model_metrics WHERE period_date = TODAY
    API-->>Sched: metrics (avg_inference_ms = 45000 > threshold 30000)

    par Алерты
        Sched->>Bot: send_admin_alert("Pipeline data_collector не обновлялся 10+ мин")
        Bot->>TG: "🛑 Pipeline СБОЙ: data_collector\n⏱ Последний успех: 10:50\n❌ Ошибка: T-Invest API timeout"
        TG-->>Admin: Push-уведомление
    and
        Sched->>Bot: send_admin_alert("Inference time аномально высок")
        Bot->>TG: "⚠️ ML Engine: avg inference 45с (порог 30с)\nТикеры: SBER, GAZP"
        TG-->>Admin: Push-уведомление
    end
```

---

## Сводка потоков

| # | Поток | Тип | Участники | Триггер |
|---|-------|-----|-----------|---------|
| 1 | Онбординг | Синхронный | Trader → Bot → API → DB → LLM | /start команда |
| 2 | Получение рекомендации | Синхронный/кэш | Trader → Bot → API → ML → Redis | Команда пользователя |
| 3 | Просмотр графика | Синхронный/кэш | Trader → Mini App → API → Redis | Открытие Mini App |
| 4 | Фоновый сбор данных | Асинхронный | Scheduler → DC → SP → ML → DB | Celery beat (каждые 5 мин) |
| 5 | Push-уведомление | Асинхронный | Scheduler → Bot → Telegram | Срабатывание триггера |
| 6 | Редирект в брокер | Клиентский | Mini App → Deep Link → Т-Банк | Кнопка пользователя |
| 7 | Админ мониторинг | Синхронный | Admin → Panel → API → DB | Открытие панели |
| 8 | Алерт администратору | Асинхронный | Scheduler → Bot → Telegram → Admin | Сбой pipeline / аномалия метрик |
