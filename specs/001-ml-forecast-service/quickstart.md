# Quickstart: ML Forecast Service

**Feature**: 001-ml-forecast-service | **Audience**: solo-dev (владелец
сервиса) и будущие интеграторы со стороны `backend/`.

Цель документа — довести локальную машину (или домашний сервер Finzooka)
от «только что склонированный репозиторий» до успешного
`Forecast`-запроса по SBER через gRPC. Ожидаемое время прохождения: ~20 минут.

---

## 0. Предусловия

| Компонент | Версия | Назначение |
|-----------|--------|------------|
| Docker | ≥ 24.0 | контейнер сервиса + зависимости |
| Docker Compose | v2 | оркестрация |
| Python | 3.11 | если будете запускать без Docker |
| PostgreSQL | 15+ | shared БД Finzooka (реестр моделей) |
| Redis | 7+ | вход live-данных |
| grpcurl | любая | отладка RPC без Python-клиента |

Все БД-инстансы уже описаны в общем `docker-compose.yaml` Finzooka;
для локального smoke-теста достаточно поднять `postgres`, `redis`,
`ml` и `ml-worker`.

---

## 1. Клонирование и структура

```bash
git checkout 001-ml-forecast-service
ls ml_forecast/   # ожидается после реализации Phase 2
```

Если каталог `ml_forecast/` ещё пустой — значит Phase 2 не выполнена.
Этот quickstart написан «на будущее» как acceptance-doc: он
выполним, когда `/speckit.tasks` + `/speckit.implement` отработают.

---

## 2. Подготовка окружения

### 2.1 `.env` для сервиса

Создайте `ml_forecast/.env.local` (в `.gitignore`):

```dotenv
ML_DATABASE_URL=postgresql://finzooka:finzooka@postgres:5432/finzooka
ML_REDIS_URL=redis://redis:6379/0
ML_ARTIFACT_DIR=/data/models
ML_ARCHIVE_DIR=/data/archive
ML_LOG_LEVEL=INFO
ML_GRPC_PORT=50051
ML_FORBIDDEN_PHRASES_PATH=/app/config/forbidden_phrases.yaml

# Data freshness thresholds (see spec FR-002b)
ML_OHLCV_MAX_AGE_MULTIPLIER=1
ML_SENTIMENT_MAX_AGE_SECONDS=3600

# Training
ML_TRAIN_MAX_DURATION_SECONDS=600
ML_OPTUNA_TRIALS=50
ML_WALKFORWARD_FOLDS=5
```

### 2.2 Миграции PostgreSQL

```bash
cd ml_forecast
alembic upgrade head
```

Поднимутся таблицы `ml.model_registry`, `ml.training_run`,
`ml.inference_log`, `ml.shadow_prediction`, `ml.backtest_report`,
`ml.classification_run`, плюс partial-UK «ровно одна production-модель».

### 2.3 Сборка gRPC-стабов

```bash
python -m grpc_tools.protoc \
  -I ../specs/001-ml-forecast-service/contracts \
  --python_out=src/ml_forecast/grpc_gen \
  --grpc_python_out=src/ml_forecast/grpc_gen \
  ../specs/001-ml-forecast-service/contracts/ml_forecast.proto
```

(В production это шаг внутри `Dockerfile`.)

---

## 3. Запуск Docker Compose

Из корня репозитория:

```bash
docker compose up -d postgres redis ml ml-worker
docker compose logs -f ml
```

Ожидаемый лог:

```
INFO  ml_forecast.main: gRPC server listening on :50051
INFO  ml_forecast.storage.postgres: connected to finzooka
INFO  ml_forecast.storage.redis_client: connected to redis://redis:6379
INFO  ml_forecast.observability.metrics: Prometheus exporter at :9100
```

Health-check:

```bash
grpcurl -plaintext localhost:50051 grpc.health.v1.Health/Check
# ожидается: { "status": "SERVING" }
```

---

## 4. Загрузка обучающих данных

Архив CSV монтируется как read-only volume. Сервис не мутирует его.

```yaml
# фрагмент общего docker-compose.yaml
services:
  ml:
    volumes:
      - ./archive:/data/archive:ro
      - ml_models:/data/models
```

Проверить внутри контейнера:

```bash
docker compose exec ml ls /data/archive/M15 | head -5
# ABIO_M15.csv  ABRD_M15.csv  ACKO_M15.csv  AFKS_M15.csv  AFLT_M15.csv
```

---

## 5. Первое обучение: SBER × D1

```bash
grpcurl -plaintext \
  -d '{"ticker":"SBER","timeframe":"TIMEFRAME_D1","trigger":"TRAIN_TRIGGER_MANUAL"}' \
  localhost:50051 finzooka.ml.v1.MlForecast/Train
# {"training_run_id": 1, "enqueued_at": "...", "queue": "train_queue"}
```

Следим за прогрессом:

```bash
watch -n 5 'grpcurl -plaintext \
  -d "{\"training_run_id\":1}" \
  localhost:50051 finzooka.ml.v1.MlForecast/GetTrainingRun'
```

Ожидается последовательность:
1. `status: running` (пока Celery-воркер крутит walk-forward)
2. `status: succeeded`, `promote_decision: auto_shadow` —
   модель в `shadow`; production пустая → `awaiting_admin` если
   это первая версия, иначе начинается 5-сессионный shadow-пробег.
3. При отсутствии предыдущей production модели промоутится
   автоматически после минимального периода оценки walk-forward
   метрик (feature flag, default off на MVP).

Для smoke-теста быстро продвигаем вручную:

```bash
grpcurl -plaintext \
  -d '{"ticker":"SBER","timeframe":"TIMEFRAME_D1","model_version":"<v1-from-getrun>"}' \
  localhost:50051 finzooka.ml.v1.MlForecast/Promote
```

---

## 6. Первый Forecast

Поднимите фикстуру Redis (только для quickstart; в проде Data
Collector и Sentiment Pipeline это делают автоматически):

```bash
python scripts/quickstart_seed_redis.py SBER D1
# Пишет ohlcv:SBER:D1:last + :ts (из последних строк CSV), а также
# sentiment:SBER:agg со score=0.0 для проверки multi-factor пути.
```

Запрос прогноза:

```bash
grpcurl -plaintext \
  -d '{"ticker":"SBER","timeframe":"TIMEFRAME_D1","horizon":1}' \
  localhost:50051 finzooka.ml.v1.MlForecast/Forecast
```

Ожидаемый ответ (сокращённо):

```json
{
  "request_id": "9b8e…",
  "status": "FORECAST_STATUS_OK",
  "predicted_path": [ { "t": "...", "mean": 310.12, "lo": 305.4, "hi": 314.7 } ],
  "suggested_stop_loss": 302.1,
  "suggested_take_profit": 320.5,
  "factors": [
    { "name": "rsi_14", "value": 52.4, "has_value": true, "contribution": 0.18, "source": "ohlcv", "status": "FACTOR_STATUS_OK" },
    { "name": "atr_14", "value": 4.35, "has_value": true, "contribution": 0.14, "source": "ohlcv", "status": "FACTOR_STATUS_OK" },
    { "name": "sentiment_score", "value": 0.0, "has_value": true, "contribution": 0.07, "source": "sentiment", "status": "FACTOR_STATUS_OK" }
  ],
  "explanation": "Рост на 0.3% в течение 1 D1-бара. Основные факторы: rsi_14, atr_14, sentiment_score. Текущая MAPE модели SBER/D1: 2.1%.",
  "mape_at_generation": 0.021,
  "model_version": "armaexo-2026-04-19-a1b2c3",
  "source_availability": { "ohlcv": "SOURCE_FRESHNESS_FRESH", "sentiment": "SOURCE_FRESHNESS_FRESH" }
}
```

---

## 7. Acceptance-cheсks для Phase 2

Эти проверки — прямая проекция user stories из [spec.md](spec.md):

| Проверка | Команда | Связано |
|----------|---------|---------|
| US-1 AC1 — `factors[]` ≥ 3, `explanation` непустое | `grpcurl Forecast` + `jq '.factors | length >= 3'` | SC-006 |
| US-1 AC2 — отказ `insufficient_history` | запросить `Forecast` по тикеру без обучения | FR-002 |
| US-1 AC3 — отказ `horizon_out_of_range` | `horizon=100` на D1 | US-1 |
| US-1 AC4 — DEGRADED при пропавшем sentiment | удалить `sentiment:SBER:agg` и повторить запрос | FR-014 |
| US-2 AC1 — ≥5 фолдов per-fold metrics | `GetTrainingRun` | FR-007 |
| US-2 AC2 — `shadow` после успешного train | `ListModels state=SHADOW` | FR-026 |
| US-2 AC4 — `do_not_promote` при регрессии | ручной test: обучить на повреждённом CSV | FR-009 |
| US-3 — `Backtest` возвращает baselines | `grpcurl Backtest` | FR-011 |
| US-4 — метрика `ml_forecast_latency_ms_p95` в Prometheus | `curl :9100/metrics` | FR-005, FR-016 |
| US-5 — `ClassifyLiquidity` recall ≥ 0.80 | `grpcurl ClassifyLiquidity` | SC-010 |
| Anti look-ahead property test | `pytest tests/unit/features/test_antilookahead.py -q` | FR-021 |
| Forbidden-phrases filter | `pytest tests/unit/inference/test_explain_forbidden.py` | FR-019 |

---

## 8. Smoke-скрипт «всё сразу»

```bash
make smoke   # реализуется в Phase 2 Makefile
# 1) alembic upgrade head
# 2) proto codegen
# 3) docker compose up -d ml ml-worker postgres redis
# 4) train SBER D1
# 5) promote
# 6) seed redis
# 7) forecast SBER D1 horizon=1
# 8) assert предыдущие поля присутствуют и статус == OK
```

Если `make smoke` зелёный — сервис пригоден для использования backend’ом
и для передачи в `/speckit.tasks` → `/speckit.implement` как «готовый к
расширению».

---

## 9. Траблшутинг

| Симптом | Причина | Что делать |
|---------|---------|------------|
| `grpc status UNAVAILABLE` на Forecast | Redis недоступен | `docker compose ps redis`; проверить `ML_REDIS_URL` |
| `FAILED_PRECONDITION stale_ohlcv` | последний бар старше порога | удостовериться, что Data Collector пишет ключи; для quickstart — перезапустить `quickstart_seed_redis.py` |
| `FAILED_PRECONDITION unexplainable` | ARMAExo выдал < 3 факторов (все contribution ~0) | переобучить модель; возможно, датасет слишком короткий |
| Train run завис на `running` > 10 мин | SC-005 нарушен | посмотреть `ml_forecast.training_run.error`, `docker compose logs ml-worker` |
| `explanation` отвергается forbidden-фильтром | шаблон или факторы воспроизвели триггер-слово | обновить шаблон в `inference/explain.py`; добавить юнит-тест |
