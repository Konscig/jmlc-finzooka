# Phase 1 Data Model: ML Forecast Service

**Feature**: 001-ml-forecast-service | **Date**: 2026-04-19

Границы ответственности:

- Shared-таблицы (`tickers`, `signals`, `signal_results`,
  `model_metrics`, `pipeline_statuses`) **уже описаны** в
  [er-diagram.md](../../artifacts/er-diagram.md) и принадлежат
  backend’у. Этот сервис **читает** `tickers` и **записывает** в
  `model_metrics`, `pipeline_statuses`; таблицы `signals` /
  `signal_results` сервиса не касаются.
- Ниже описываются таблицы, принадлежащие только
  `ml-forecast`-сервису (живут в той же PostgreSQL-инстанции,
  схема `ml`), плюс in-memory объекты API.

---

## 1. Owned tables (schema `ml`)

### 1.1 `ml.model_registry`

**Назначение**: единый реестр артефактов моделей. Источник истины
для состояния `training / shadow / production / archived /
do_not_promote`. На пару (ticker, timeframe) — ровно одна
production-запись в любой момент времени.

| Поле | Тип | Null | Default | Описание |
|------|-----|------|---------|----------|
| id | BIGINT | NO | GENERATED | PK |
| ticker_id | BIGINT | NO | — | FK → `tickers.id` |
| timeframe | VARCHAR(4) | NO | — | `MN1`/`W1`/`D1`/`H4`/`H1`/`M30`/`M15`/`M10`/`M5` |
| model_family | VARCHAR(32) | NO | — | `armaexo` / `lightgbm` / `baseline_naive` / `baseline_ohlcv_only` |
| model_version | VARCHAR(64) | NO | — | UK вместе с (ticker_id, timeframe) |
| state | VARCHAR(16) | NO | `training` | state-machine (см. ниже) |
| artifact_path | TEXT | YES | — | путь к `.joblib` на ФС |
| artifact_sha256 | CHAR(64) | YES | — | целостность |
| dataset_sha256 | CHAR(64) | NO | — | хеш CSV, на котором обучались |
| feature_set_version | VARCHAR(16) | NO | — | напр. `v1` (см. R5) |
| current_mape | DECIMAL(6,4) | YES | — | посл. ежедневный пересчёт |
| current_win_rate | DECIMAL(6,4) | YES | — | то же |
| promoted_at | TIMESTAMP | YES | — | когда переведена в production |
| archived_at | TIMESTAMP | YES | — | — |
| created_at | TIMESTAMP | NO | NOW() | — |
| updated_at | TIMESTAMP | NO | NOW() | — |

**Индексы**:
- UK `(ticker_id, timeframe, model_version)`
- Partial UK `(ticker_id, timeframe) WHERE state = 'production'`
  — физически гарантирует «ровно одна production-модель»
- IDX `(state)` для админ-фильтров

**State transitions**:

```
training ──success──▶ shadow ──promote──▶ production
    │                   │                      │
    │                   └──archive─────────────┤
    └──failure──▶ do_not_promote               │
                                  production ──▶ archived
```

Правила:
1. Только admin RPC `Promote` переводит `shadow → production` —
   предыдущая production → `archived` в той же транзакции.
2. `training → do_not_promote`: walk-forward валидация не прошла
   минимальные пороги (регрессия против действующей production).
3. Переходы `do_not_promote → *` возможны только вручную
   (admin решает «отбросить» или «принудительно отправить в shadow»).
4. `archived` — терминальное состояние; модель остаётся для
   истории/отката (FR-010, минимум 3 версии).

---

### 1.2 `ml.training_run`

**Назначение**: журнал попыток обучения (FR-008, FR-009). Одна
строка = один запуск train, вне зависимости от итога.

| Поле | Тип | Null | Default | Описание |
|------|-----|------|---------|----------|
| id | BIGINT | NO | GENERATED | PK |
| model_id | BIGINT | YES | — | FK → `ml.model_registry.id`; NULL если тренировка провалилась до сохранения артефакта |
| ticker_id | BIGINT | NO | — | FK → `tickers.id` |
| timeframe | VARCHAR(4) | NO | — | — |
| model_family | VARCHAR(32) | NO | — | — |
| started_at | TIMESTAMP | NO | NOW() | — |
| finished_at | TIMESTAMP | YES | — | NULL = ещё выполняется |
| status | VARCHAR(16) | NO | `running` | `running` / `succeeded` / `failed` / `cancelled` |
| trigger | VARCHAR(16) | NO | — | `scheduled` / `manual` / `family_switch`. Значения строго соответствуют `TrainTrigger` enum из [ml_forecast.proto](contracts/ml_forecast.proto) — используется одно имя на обе роли (event в journal + enum в RPC). |
| hyperparams | JSONB | NO | `{}` | выбранные Optuna параметры |
| metrics_fold | JSONB | YES | — | массив per-fold MAPE/RMSE/MAE/R²/dir_acc |
| metrics_aggregate | JSONB | YES | — | усреднённые метрики |
| comparison_to_prev | JSONB | YES | — | delta против предыдущей production |
| promote_decision | VARCHAR(24) | YES | — | `auto_shadow` / `do_not_promote` / `awaiting_admin` |
| error | TEXT | YES | — | stack-trace если `failed` |
| duration_seconds | INT | YES | — | производное |

**Индексы**:
- IDX `(ticker_id, timeframe, started_at DESC)`
- IDX `(status, started_at DESC)`

---

### 1.3 `ml.inference_log`

**Назначение**: запись каждого RPC `Forecast` (FR-015). Используется
для наблюдаемости и ежедневного пересчёта `current_mape` (R8).

| Поле | Тип | Null | Default | Описание |
|------|-----|------|---------|----------|
| id | BIGINT | NO | GENERATED | PK |
| request_id | UUID | NO | GENERATED | UK, возвращается клиенту |
| ticker_id | BIGINT | NO | — | FK → `tickers.id` |
| timeframe | VARCHAR(4) | NO | — | — |
| horizon | SMALLINT | NO | — | запрошенный горизонт |
| model_id | BIGINT | YES | — | FK → `ml.model_registry.id`; NULL при raject |
| status | VARCHAR(24) | NO | — | `ok` / `degraded` / `stale_ohlcv` / `stale_sentiment_fallback` / `insufficient_history` / `horizon_out_of_range` / `unexplainable` / `model_not_found` / `error` |
| latency_ms | INT | NO | — | — |
| mape_at_generation | DECIMAL(6,4) | YES | — | снапшот `current_mape` на момент ответа |
| source_availability | JSONB | NO | `{}` | `{"ohlcv": "fresh", "sentiment": "stale"}` и т.д. |
| predicted_path | JSONB | YES | — | массив `{t, mean, lo, hi}` длины `horizon` |
| factors | JSONB | YES | — | массив контрибуций |
| explanation | TEXT | YES | — | финальный текст (после фильтра) |
| error | TEXT | YES | — | — |
| generated_at | TIMESTAMP | NO | NOW() | — |
| resolved_at | TIMESTAMP | YES | — | когда стал известен факт для метрик |
| actual_path | JSONB | YES | — | фактические close по этому горизонту |
| abs_error_pct | DECIMAL(6,4) | YES | — | средний \|err\|/\|actual\| по горизонту |

**Индексы**:
- UK `request_id`
- IDX `(ticker_id, timeframe, generated_at DESC)`
- IDX `(resolved_at)` WHERE `resolved_at IS NULL` (для `shadow_resolve`-таска)
- IDX `(status)` для мониторинга

**Retention**: 1 год (NFR 8.1); партицирование по месяцам в v1.1.

---

### 1.4 `ml.shadow_prediction`

**Назначение**: предсказания shadow-моделей, выполненные параллельно
production-ответу (FR-026, R7). Структура похожа на `inference_log`,
но привязана к shadow-модели.

| Поле | Тип | Null | Default | Описание |
|------|-----|------|---------|----------|
| id | BIGINT | NO | GENERATED | PK |
| request_id | UUID | NO | — | FK-like → `ml.inference_log.request_id`; тот же request, что и production |
| shadow_model_id | BIGINT | NO | — | FK → `ml.model_registry.id` (state=`shadow`) |
| predicted_path | JSONB | NO | — | — |
| factors | JSONB | YES | — | — |
| explanation | TEXT | YES | — | не возвращается клиенту |
| generated_at | TIMESTAMP | NO | NOW() | — |
| resolved_at | TIMESTAMP | YES | — | — |
| actual_path | JSONB | YES | — | — |
| abs_error_pct | DECIMAL(6,4) | YES | — | — |

**Индексы**:
- IDX `(shadow_model_id, generated_at DESC)`
- IDX `(request_id)`

**Business rules**:
1. INSERT идёт после того, как production-ответ вернулся клиенту
   (Celery-таск).
2. Для одного `request_id` может быть 0..N строк (по одной на
   каждую активную shadow-модель этой пары ticker/timeframe).
3. Запись — append-only; `resolved_at`/`actual_path` UPDATE’ятся
   периодическим `shadow_resolve`.

---

### 1.5 `ml.backtest_report`

**Назначение**: результат RPC `Backtest` (FR-011). Хранится для
воспроизводимости и публикации в admin-панель.

| Поле | Тип | Null | Default | Описание |
|------|-----|------|---------|----------|
| id | BIGINT | NO | GENERATED | PK |
| model_id | BIGINT | NO | — | FK → `ml.model_registry.id` |
| period_start | DATE | NO | — | — |
| period_end | DATE | NO | — | — |
| executed_at | TIMESTAMP | NO | NOW() | — |
| metrics_model | JSONB | NO | — | MAPE/RMSE/win_rate/dir_acc |
| metrics_naive | JSONB | NO | — | baseline `naive` |
| metrics_ohlcv_only | JSONB | NO | — | baseline `ohlcv_only` |
| signals_vs_actuals | JSONB | NO | — | массив (предсказ, факт, timestamp) |

**Индексы**:
- IDX `(model_id, executed_at DESC)`

---

### 1.6 `ml.classification_run`

**Назначение**: запуски классификатора ликвидности тикеров (US-5).

| Поле | Тип | Null | Default | Описание |
|------|-----|------|---------|----------|
| id | BIGINT | NO | GENERATED | PK |
| executed_at | TIMESTAMP | NO | NOW() | — |
| feature_set_version | VARCHAR(16) | NO | — | — |
| total_tickers | INT | NO | — | — |
| blue_chip_recall | DECIMAL(6,4) | NO | — | SC-010 ≥ 0.80 |
| other_recall | DECIMAL(6,4) | NO | — | ≥ 0.90 |
| confusion_matrix | JSONB | NO | — | — |
| label_predictions | JSONB | NO | — | `[{ticker_id, predicted, prob}]` |

---

## 2. Shared tables touched (read/write)

### 2.1 `tickers` (read-only, updated by ops)

Сервис читает `symbol`, `is_blue_chip`, `is_active`. На MVP только
`is_blue_chip = true`. Не пишет.

### 2.2 `model_metrics` (write)

Из [er-diagram.md:120](../../artifacts/er-diagram.md#L120) уже
определена. Ежедневный Celery-таск `recompute_daily_metrics`
INSERT’ит по одной строке на каждый (ticker_id, `period_date=today`),
поля: `mape`, `win_rate`, `total_signals`, `profitable_signals`,
`avg_inference_ms`. Это публичная поверхность для admin-панели.

### 2.3 `pipeline_statuses` (write)

Health-task раз в минуту (FR-017) обновляет строку
`pipeline_name='ml_forecast'` полями `status`, `last_success_at`,
`last_failure_at`, `last_error`.

---

## 3. Redis contract (read-only, ключи владеют другие сервисы)

Закреплено в [research.md#R2](research.md). Сводка:

| Key | Owner | Type | Refresh |
|-----|-------|------|---------|
| `ohlcv:<ticker>:<timeframe>:last` | Data Collector | JSON blob | каждый закрытый бар |
| `ohlcv:<ticker>:<timeframe>:last:ts` | Data Collector | string (ISO-8601) | синхронно с выше |
| `sentiment:<ticker>:agg` | Sentiment Pipeline | JSON blob | раз в 15 мин (план смежного сервиса) |
| `sentiment:<ticker>:agg:ts` | Sentiment Pipeline | string (ISO-8601) | синхронно |

ML-сервис только `GET`’ает; TTL — ответственность владельца.

---

## 4. Local filesystem layout

```
data/models/
  <ticker>/
    <timeframe>/
      production.joblib         # symlink на текущую production-версию
      <model_version>.joblib
      <model_version>.metadata.json
      archive/
        <old_model_version>.joblib
        <old_model_version>.metadata.json
```

Права: owner-only read/write; SELinux/Docker-volume mount с
политикой доступа only от сервиса `ml_forecast` (Principle VI).

---

## 5. In-memory API objects (domain layer)

Не persisted, используются между gRPC boundary и доменом.

### `Forecast` (domain dataclass)

```python
@dataclass(frozen=True)
class Forecast:
    request_id: UUID
    ticker: str
    timeframe: Timeframe  # Enum
    horizon: int
    predicted_path: list[PricePoint]   # length == horizon
    suggested_stop_loss: Decimal
    suggested_take_profit: Decimal
    factors: list[FactorContribution]
    explanation: str
    mape_at_generation: Decimal | None
    model_version: str
    generated_at: datetime
    source_availability: SourceAvailability
    status: ForecastStatus  # OK / DEGRADED
```

### `PricePoint`

```python
@dataclass(frozen=True)
class PricePoint:
    t: datetime           # середина целевого бара
    mean: Decimal
    lo: Decimal           # нижняя граница доверительного интервала
    hi: Decimal           # верхняя
```

### `FactorContribution`

```python
@dataclass(frozen=True)
class FactorContribution:
    name: str             # 'rsi_14', 'sentiment_score', ...
    value: Decimal | None # фактическое значение; None если источник недоступен
    contribution: Decimal # нормированный вклад, sum |contribution| ≈ 1
    source: str           # 'ohlcv' / 'sentiment' / 'technical'
    status: FactorStatus  # OK / STALE / UNAVAILABLE
```

### `SourceAvailability`

```python
@dataclass(frozen=True)
class SourceAvailability:
    ohlcv: Literal['fresh', 'stale']
    sentiment: Literal['fresh', 'stale', 'unavailable']
```

### `Timeframe` (enum)

```python
class Timeframe(str, Enum):
    MN1 = "MN1"; W1 = "W1"; D1 = "D1"
    H4 = "H4"; H1 = "H1"
    M30 = "M30"; M15 = "M15"; M10 = "M10"; M5 = "M5"

    @property
    def seconds(self) -> int: ...
```

### `ModelState` (enum)

```python
class ModelState(str, Enum):
    TRAINING = "training"
    SHADOW = "shadow"
    PRODUCTION = "production"
    ARCHIVED = "archived"
    DO_NOT_PROMOTE = "do_not_promote"
```

---

## 6. Validation rules summary

| Правило | Источник | Место контроля |
|---------|----------|----------------|
| 500+ баров OHLCV для обучения | FR-002 | `TrainPipeline.validate_dataset` |
| Монотонный datetime, нет отриц. цен/volume | FR-013 | `features.validators.validate_ohlcv_csv` |
| `horizon ≤ max_trained_horizon` | US-1 AC3 | `ForecastService.__call__` |
| OHLCV возраст ≤ `timeframe.seconds` | FR-002b | `inference.freshness.check_ohlcv` |
| Sentiment возраст ≤ 3600 s | FR-002b | `inference.freshness.check_sentiment` |
| `explanation` не содержит forbidden phrase | FR-019 | `inference.explain.render` |
| `factors[]` длина ≥ 3 | SC-006 | `inference.explain.render` |
| Anti look-ahead: фичи бара t не зависят от данных >t | FR-021 | hypothesis-тест `features/validators.py` |
| На (ticker, timeframe) ровно одна production-модель | §1.1 | Partial UK + сервисный инвариант в `Promote` RPC |
