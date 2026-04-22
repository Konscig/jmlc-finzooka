# Implementation Plan: ML Forecast Service

**Branch**: `001-ml-forecast-service` | **Date**: 2026-04-19 | **Spec**: [spec.md](spec.md)
**Input**: Feature specification from `specs/001-ml-forecast-service/spec.md`

## Summary

Вынести прогнозную математическую модель из исследовательского ноутбука
`ML_ТМБ.ipynb` в полноценный gRPC-микросервис `ml-forecast` (контейнер
`ML Engine` из [c4-architecture.md](../../artifacts/c4-architecture.md)).
Сервис предоставляет три операции: `Forecast` (синхронный запрос-ответ для
backend’а), `Train` (обучение по паре ticker × timeframe с walk-forward
валидацией) и `Backtest` (исторический отчёт за период).

Технический подход:

- **Primary candidate** модели — ARMAExo из ноутбука с доработками
  (anti look-ahead, walk-forward на ≥5 фолдах, hyperparameter search,
  честное сравнение с baseline’ами). **Fallback** — LightGBM при провале
  целевых метрик (SC-001).
- **Интеграционный контур** — gRPC (`finzooka.ml.v1`) внутри общего
  Docker Compose с backend’ом. Backend вызывает синхронно; долгие
  операции (training) — через Celery-воркер.
- **Жизненный цикл модели** — состояния `training → shadow → production →
  archived`. Shadow-версия крутится параллельно минимум 5 торговых
  сессий, затем админ продвигает вручную.
- **Входы live inference** читаются из общего Redis (контракт с
  `Data Collector` и `Sentiment Pipeline`); архив `archive/` — только
  для обучения и бэктеста.
- **Наблюдаемость** — каждый inference пишется в журнал PostgreSQL,
  ежедневные агрегаты (MAPE, win rate, latency) публикуются в
  admin-панель; health-check раз в минуту (конституция, принцип V).

## Technical Context

**Language/Version**: Python 3.11 (единый стек Finzooka — backend / ML / bots)
**Primary Dependencies**:
- gRPC: `grpcio`, `grpcio-tools`, `grpcio-health-checking`, `protobuf`
- ML: `scikit-learn` (RandomForest для классификатора, walk-forward
  валидация), `statsmodels` (ARMAExo primary), `lightgbm` (fallback),
  `optuna` (hyperparameter search)
- Данные / фичи: `pandas`, `numpy`, `ta` (библиотека технических
  индикаторов)
- Интеграция: `redis-py` (чтение live OHLCV/sentiment),
  `sqlalchemy` + `psycopg2-binary` (model registry, training runs,
  inference log), `celery` (train-worker + cron-расписание),
  `pydantic-settings` (конфиг)
- Тесты: `pytest`, `pytest-asyncio`, `grpcio-testing`, `hypothesis`
  (свойства walk-forward валидатора)

**Storage**:
- **PostgreSQL** (уже в стеке): таблицы `model_registry`, `training_run`,
  `inference_log`, `shadow_prediction`, `backtest_report`, `classification_run`.
- **Redis** (уже в стеке): (а) вход — OHLCV-бары и sentiment-агрегаты от
  других сервисов; (б) выход — недолговечный кэш последних прогнозов
  (TTL = длительность бара).
- **Локальная ФС** — артефакты моделей (`.joblib`/`.pkl`) в
  `data/models/<ticker>/<timeframe>/<model_version>.joblib`; RPO 24 ч
  через ежедневный бэкап каталога (NFR 2.2).

**Testing**:
- unit (pytest) — feature engineering, walk-forward валидатор, конвертеры
- contract (grpcio-testing) — каждая RPC в `ml_forecast.proto`
- integration — полный цикл `train → shadow → promote → forecast →
  backtest` на фиксированном CSV из `archive/`
- property-based (hypothesis) — инвариант anti look-ahead (ни одна
  фича в момент `t` не зависит от данных `>t`)

**Target Platform**: Linux container (Docker), домашний сервер Finzooka
(4 CPU / 12 ГБ ОЗУ / GTX 1650). Сервис не требует GPU на MVP (ARMAExo
и LightGBM — CPU-bound); GPU зарезервирован за Sentiment Pipeline.

**Project Type**: gRPC microservice (single-repo monorepo; эта фича —
один каталог верхнего уровня рядом с будущими `backend/`, `bot/`,
`sentiment/`, `data_collector/`).

**Performance Goals** (из спеки SC-004 / SC-005):
- `Forecast` p95 ≤ 30 с, среднее ≤ 15 с, timeout 60 с
- `Train` одного тикера × таймфрейма ≤ 10 мин на референс-железе
- Пропускная способность MVP: 5–25 rpm; целевая (100 DAU × 5 тикеров):
  500 rpm на пике торговой сессии

**Constraints**:
- MAPE ≤ 5% (M15 / горизонт 4) и ≤ 3% (D1 / горизонт 1) на 5 голубых
  фишках (SBER, GAZP, LKOH, GMKN, ROSN) — SC-001
- Directional accuracy ≥ 55% — SC-002
- Свежесть входов: OHLCV ≤ 1× длительности таймфрейма; sentiment
  ≤ 60 мин (иначе fallback или отказ)
- Availability ≥ 95% в MOEX-часы (10:00–18:50 МСК пн–пт); вне сессии
  простой не считается
- Данные — локально в РФ (152-ФЗ); артефакты и логи не покидают
  домашний сервер
- `explanation` не содержит слов из чёрного списка
  («гарантировано», «точно вырастет», «100%»)

**Scale/Scope**:
- MVP: 5 голубых фишек × 2 таймфрейма (M15, D1) = 10 production-моделей;
  возможно 10 shadow-моделей параллельно
- Исторические данные для обучения/бэктеста: `archive/` ≈ 3 ГБ, 249
  CSV × 9 таймфреймов; для MVP используется только D1 и M15 для
  5 тикеров (≈50 МБ)
- Инференс в день: 5 тикеров × ~100 запросов = ~500 inference/сутки
  на MVP
- Переобучение: 10 моделей × 1 раз/неделю = 10 тренировок по ≤10 мин
  = ≤2 ч в окно обслуживания

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

Gates derive from `.specify/memory/constitution.md` v1.0.0.
Conflict priority: VI > I > II > V > III > IV > VII.

### I. Explainability Gate (NON-NEGOTIABLE)

- [x] Каждый прогноз возвращает `explanation` (2–3 предложения) и
      массив `factors[]` с контрибуциями (FR-004, SC-006). Для ARMAExo
      контрибуции берутся из коэффициентов AR/MA + экзогенных; для
      LightGBM — из `TreeExplainer` (SHAP).
- [x] «Чёрный ящик» не допускается: если модель не может назвать
      ≥3 фактора, inference отклоняется с кодом `unexplainable`
      (дополнительный edge-case, добавлен в Phase 1 data-model).

### II. Navigator Gate

- [x] Все ответы обёрнуты как `Forecast`, а не `Instruction`; контракт
      `.proto` не содержит полей типа `order_side` или `quantity`.
- [x] Сервис не исполняет сделок (out of scope спеки).
- [x] Каждый `Forecast` содержит `suggested_stop_loss` и
      `suggested_take_profit` (вычисляются по ATR-14, как в ноутбуке).
- [x] Deep-link — ответственность backend/Mini App; этот сервис их не
      формирует.

### III. Multi-Factor Gate

- [x] Обязательны минимум два источника входов: OHLCV-технические
      индикаторы + sentiment (из Redis, владелец — `Sentiment Pipeline`).
      FR-003 + FR-014 + FR-002a.
- [x] Sentiment-агрегат из Redis сам по себе агрегирует ≥2 источника
      (Т-Пульс + новостные каналы) — ответственность
      `Sentiment Pipeline`, но контракт ключа включает поле `sources[]`,
      которое этот сервис проверяет (≥2 — иначе sentiment-вход
      игнорируется).
- [x] Fallback без sentiment описан в FR-002b; снижение уверенности
      попадает в `explanation` и в `factors[]` как явная запись
      `{name: "sentiment", value: null, status: "unavailable"}`.
- [x] `factors[]` перечисляет каждый источник и его вклад.

### IV. Telegram-Native UX Gate

- [N/A] Этот сервис — backend-only (gRPC для других контейнеров).
  UX-слой (Bot, Mini App) — другие фичи; принцип соблюдается на
  уровне композиции.

### V. Model Integrity & Observability Gate

- [x] MAPE и win rate агрегируются ежедневно в таблице
      `model_daily_metrics` и отдаются админ-панели (FR-016).
- [x] Inference latency публикуется в Prometheus-exporter, алерт на
      `avg_5m > 30s` (FR-005 / FR-017).
- [x] Каждый `Forecast` пишет `mape_at_generation` в `inference_log`.
- [x] Health-check каждую минуту: `/health` gRPC + проверки
      (а) Redis reachable, (б) PostgreSQL reachable, (в) последний
      успешный train run не старше 14 дней по всем production-моделям.
- [x] Training pipeline формирует `Training Run` с before/after
      сравнением (FR-009).

### VI. Regulatory Compliance Gate (HIGHEST PRIORITY)

- [x] Сервис не генерирует user-facing copy напрямую; backend обёртывает
      ответ дисклеймером. Сервис валидирует `explanation` по чёрному
      списку слов перед возвратом.
- [x] `explanation` проходит через фильтр запрещённых формулировок;
      список слов — в `config/forbidden_phrases.yaml`.
- [x] PII отсутствует (FR-018): на вход — только тикер и таймфрейм.
- [x] Токен T-Invest API в этом сервисе не используется (владелец —
      `Data Collector`). Сервис читает только обезличенные OHLCV из
      Redis. N/A для данного контура.

### VII. Simplicity & MVP Discipline Gate

- [x] Scope MVP = 5 голубых фишек × 2 таймфрейма (M15 + D1), остальные
      отложены до v1.1 (spec FR-012).
- [x] Новых СУБД/брокеров/очередей не добавляется — PostgreSQL, Redis,
      Celery уже в стеке.
- [x] Деплой — тот же Docker Compose; добавляется один сервис `ml`
      (контейнер) + один worker-контейнер `ml-worker` (train/backtest).
- [x] Ресурсы: ARMAExo и LightGBM на blue chips × D1/M15 укладываются
      в 4 CPU / 12 ГБ ОЗУ; GPU не требуется.
- [x] Universe строго blue chips; любое расширение — через пересмотр
      спеки.

**Deviations**: нет. Все gate пройдены без отклонений → проходим в Phase 0.

**Post-impl re-verify (2026-04-21)**:
- I Explainability — `factor_contributions` реализовано в
  `models/armaexo.py` (через SARIMAX coefficients) и
  `models/lightgbm_model.py` (через SHAP TreeExplainer);
  `inference/explain.py::render` отклоняет запрос если значимых
  факторов (`|contribution| > EXPLAIN_MIN_CONTRIBUTION=0.01` и
  `status != UNAVAILABLE`) меньше трёх. ✓
- III Multi-Factor — `features/sentiment_features.py` содержит
  multi-source guard (`len(sources) < 2 → UNAVAILABLE`), закрывая
  remediation C3. Fallback-режим реализован через поле status
  `SourceAvailability.sentiment` и прокидывается в
  `ForecastResponse.source_availability.sentiment`. ✓
- V Model Integrity — метрики публикуются через
  `observability/metrics.py` (`ml_forecast_latency_seconds`,
  `ml_forecast_current_mape`, `ml_forecast_stale_rate`); ежедневный
  пересчёт current_mape живёт в `observability/jobs.py`; 6 alert-
  правил в `config/alerts/prometheus_rules.yaml`. ✓
- VI Regulatory — fail-closed forbidden-phrase фильтр
  (`inference/explain.py::check_forbidden`) прогоняется на каждом
  `render()`; тест прибивает каждую фразу из
  `config/forbidden_phrases.yaml`. ✓
- VII Simplicity — ни одна новая БД/очередь не добавлена; Celery
  на Redis-брокере, всё в docker-compose.yaml. ✓

## Project Structure

### Documentation (this feature)

```text
specs/001-ml-forecast-service/
├── plan.md              # this file
├── spec.md
├── research.md          # Phase 0 — выбор библиотек, схема Redis, walk-forward
├── data-model.md        # Phase 1 — ER сервиса (PostgreSQL + in-memory)
├── quickstart.md        # Phase 1 — локальный запуск, smoke-test gRPC
├── contracts/
│   └── ml_forecast.proto   # Phase 1 — gRPC-контракт finzooka.ml.v1
├── checklists/
│   └── requirements.md
└── tasks.md             # Phase 2 — /speckit.tasks (не создаётся этой командой)
```

### Source Code (repository root)

Монорепо с одним микросервисом на фичу. Для `001-ml-forecast-service`
на корне появляется каталог `ml_forecast/`:

```text
ml_forecast/
├── pyproject.toml
├── Dockerfile
├── docker-compose.snippet.yaml       # фрагмент для общего compose-стека
├── README.md
├── alembic.ini
├── migrations/                        # миграции PostgreSQL для таблиц сервиса
│   └── versions/
├── proto/
│   └── finzooka/ml/v1/ml_forecast.proto   # копия из specs/.../contracts/
├── src/
│   └── ml_forecast/
│       ├── __init__.py
│       ├── config.py                  # pydantic-settings
│       ├── main.py                    # gRPC server bootstrap
│       ├── grpc_gen/                  # сгенерированные стабы (gitignored)
│       ├── api/
│       │   ├── forecast_service.py    # RPC Forecast
│       │   ├── train_service.py       # RPC Train (enqueue Celery)
│       │   ├── backtest_service.py    # RPC Backtest
│       │   ├── admin_service.py       # RPC Promote/Archive/ShadowReport
│       │   └── health.py
│       ├── domain/
│       │   ├── ticker.py
│       │   ├── timeframe.py
│       │   ├── forecast.py            # dataclass результата
│       │   ├── model_state.py         # enum Training/Shadow/Production/Archived
│       │   └── factor.py
│       ├── models/
│       │   ├── base.py                # интерфейс BaseForecaster
│       │   ├── armaexo.py             # портированная реализация ноутбука
│       │   ├── lightgbm.py            # fallback
│       │   ├── baselines.py           # naive + ohlcv_only
│       │   └── classifier.py          # liquidity classifier (US-5)
│       ├── features/
│       │   ├── ohlcv_features.py      # RSI, MACD, ATR, Bollinger, VWAP
│       │   ├── sentiment_features.py
│       │   └── validators.py          # anti look-ahead property tests
│       ├── training/
│       │   ├── walkforward.py
│       │   ├── hyperparam.py          # optuna-обёртка
│       │   ├── pipeline.py            # train → validate → register → shadow
│       │   └── scheduler.py           # Celery beat (воскресенье ночью)
│       ├── inference/
│       │   ├── registry.py            # SQLAlchemy-модели + операции
│       │   ├── shadow.py              # параллельный запуск shadow-моделей
│       │   ├── explain.py             # formatter `explanation` + `factors[]`
│       │   └── freshness.py           # проверка stale_ohlcv / stale_sentiment
│       ├── storage/
│       │   ├── redis_client.py        # чтение OHLCV и sentiment из общих ключей
│       │   ├── postgres.py
│       │   └── artifact_store.py      # joblib на диск
│       ├── backtest/
│       │   └── runner.py
│       └── observability/
│           ├── metrics.py             # Prometheus exporter
│           └── logging.py
└── tests/
    ├── unit/
    ├── contract/
    └── integration/

archive/                                # уже существует; входной датасет
└── <timeframe>/<ticker>_<timeframe>.csv
```

**Structure Decision**: Single-project layout внутри каталога
`ml_forecast/` на корне репозитория. Причины:

- Python-пакет + Docker + миграции собираются и деплоятся как единое
  целое (один образ, один контейнер).
- Будущие микросервисы (`backend/`, `bot/`, `sentiment/`,
  `data_collector/`) добавляются как соседние каталоги, общий
  `docker-compose.yaml` на корне объединяет их.
- `archive/` остаётся в корне как shared-том: сервис `ml_forecast`
  монтирует его как read-only volume для train/backtest; live-inference
  его не читает (читает Redis).

## Complexity Tracking

> **Fill ONLY if Constitution Check has violations that must be justified**

Нет нарушений.
