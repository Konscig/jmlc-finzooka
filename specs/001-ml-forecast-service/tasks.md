---

description: "Task list for ML Forecast Service feature implementation"
---

# Tasks: ML Forecast Service

**Input**: Design documents from `specs/001-ml-forecast-service/`
**Prerequisites**: plan.md, spec.md, research.md, data-model.md, contracts/ml_forecast.proto, quickstart.md

**Tests**: Included. Justification — FR-021 (anti look-ahead property test),
FR-019 (forbidden-phrases filter tests), quickstart acceptance matrix, and
the financial domain make skipping tests unacceptable.

**Organization**: grouped by user story (US1..US5) per spec.md priorities.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies on
  incomplete tasks)
- **[Story]**: US1..US5 for user-story tasks; Setup/Foundational/Polish
  phases carry no story label

## Path Conventions

Feature code lives under `ml_forecast/` at repo root (per plan
"Project Structure"). Tests under `ml_forecast/tests/`. Migrations under
`ml_forecast/migrations/`. Proto contract under
`ml_forecast/proto/finzooka/ml/v1/ml_forecast.proto` (kept in sync with
[contracts/ml_forecast.proto](contracts/ml_forecast.proto)).

---

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: init Python package, gRPC toolchain, Docker, config.

- [X] T001 Create directory skeleton `ml_forecast/src/ml_forecast/{api,domain,models,features,training,inference,storage,backtest,observability,grpc_gen}` and `ml_forecast/tests/{unit,contract,integration}` per plan.md Project Structure
- [X] T002 Create `ml_forecast/pyproject.toml` with Python 3.11 target, dependencies `grpcio`, `grpcio-tools`, `grpcio-health-checking`, `protobuf`, `scikit-learn`, `statsmodels`, `lightgbm`, `optuna`, `pandas`, `numpy`, `ta`, `redis`, `sqlalchemy`, `psycopg2-binary`, `celery[redis]`, `pydantic-settings`, `pytest`, `pytest-asyncio`, `grpcio-testing`, `hypothesis`, `ruff`, `mypy`
- [X] T003 [P] Create `ml_forecast/Dockerfile` (multistage: builder compiles proto stubs, runtime image based on `python:3.11-slim`; non-root user; read-only archive volume mount point)
- [X] T004 [P] Create `ml_forecast/docker-compose.snippet.yaml` — services `ml` (gRPC :50051) and `ml-worker` (Celery), volumes `./archive:/data/archive:ro` and `ml_models:/data/models`, env from `.env.local`
- [X] T005 [P] Configure Ruff + MyPy in `ml_forecast/pyproject.toml` (line-length 100, target-version py311, mypy strict)
- [X] T006 [P] Create `ml_forecast/config/forbidden_phrases.yaml` with initial list from spec Assumptions («гарантировано», «точно вырастет», «100%», «гарантированный доход», «without risk», «обязательно принесёт»)
- [X] T007 Copy proto contract from `specs/001-ml-forecast-service/contracts/ml_forecast.proto` to `ml_forecast/proto/finzooka/ml/v1/ml_forecast.proto` and add `make proto` target in `ml_forecast/Makefile` that regenerates `src/ml_forecast/grpc_gen/` via `python -m grpc_tools.protoc` (gitignore generated stubs)
- [X] T008 [P] Create `ml_forecast/.env.example` mirroring quickstart.md §2.1 (placeholders for DATABASE_URL / REDIS_URL / ARTIFACT_DIR / ARCHIVE_DIR / GRPC_PORT / FORBIDDEN_PHRASES_PATH / thresholds)
- [X] T009 [P] Create `ml_forecast/alembic.ini` and `ml_forecast/migrations/env.py` wired to SQLAlchemy metadata from `src/ml_forecast/storage/postgres.py`

**Checkpoint**: `make proto && pip install -e .` works; `ruff check` and `mypy` run clean on an empty package.

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: shared domain, persistence, feature-engineering, gRPC server skeleton, baselines. All US depend on this.

**⚠️ CRITICAL**: No user-story work may start before this phase is done.

### Config & domain core

- [ ] T010 Implement `ml_forecast/src/ml_forecast/config.py` (pydantic-settings `Settings` class: db_url, redis_url, artifact_dir, archive_dir, grpc_port, ohlcv_max_age_multiplier=1, sentiment_max_age_seconds=3600, optuna_trials=50, walkforward_folds=5, forbidden_phrases_path)
- [ ] T011 [P] Implement `ml_forecast/src/ml_forecast/domain/timeframe.py` — `Timeframe` enum (9 values) with `.seconds` property and mapping from gRPC `finzooka.ml.v1.Timeframe`
- [ ] T012 [P] Implement `ml_forecast/src/ml_forecast/domain/model_state.py` — `ModelState` enum with allowed-transition table (dict mapping src→{dst set}); unit test in `tests/unit/domain/test_model_state.py` verifying transitions match data-model §1.1
- [ ] T013 [P] Implement `ml_forecast/src/ml_forecast/domain/factor.py` — `FactorContribution`, `SourceAvailability`, `FactorStatus`, `SourceFreshness` (frozen dataclasses matching data-model §5)
- [ ] T014 [P] Implement `ml_forecast/src/ml_forecast/domain/forecast.py` — `Forecast`, `PricePoint`, `ForecastStatus` frozen dataclasses matching data-model §5

### Persistence layer

- [ ] T015 [P] Implement `ml_forecast/src/ml_forecast/storage/postgres.py` — SQLAlchemy `Base`, `create_engine` from settings, session factory
- [ ] T016 Create Alembic migration `ml_forecast/migrations/versions/001_schema_ml.py` — schema `ml`, tables `model_registry`, `training_run`, `inference_log`, `shadow_prediction`, `backtest_report`, `classification_run` with all columns, types, FKs, and indexes per data-model.md §1; **partial UK** `(ticker_id, timeframe) WHERE state='production'` on `model_registry`
- [ ] T017 [P] Implement ORM models in `ml_forecast/src/ml_forecast/storage/orm.py` (one class per table, matches T016)
- [ ] T018 [P] Implement `ml_forecast/src/ml_forecast/storage/redis_client.py` — wrapper with `get_ohlcv_last(ticker, timeframe)` / `get_sentiment_agg(ticker)` returning parsed payload + timestamp; expects keys per research.md R2
- [ ] T019 [P] Implement `ml_forecast/src/ml_forecast/storage/artifact_store.py` — `save(model, ticker, timeframe, version) -> Path`, `load(path)`, `set_production_symlink(ticker, timeframe, version)` using atomic `os.replace`, SHA256 computation

### CSV validation & feature engineering

- [ ] T020 [P] Implement `ml_forecast/src/ml_forecast/features/validators.py`: (a) `validate_ohlcv_csv(path) -> DataFrame` — schema check (datetime/open/high/low/close/volume), monotonic datetime, no negative price/volume, raises `InvalidOhlcvCsv` с detail; (b) `detect_anomalous_bar(df, t) -> bool` — возвращает True если бар `t` даёт |close change| > 3×rolling-std или |volume/avg - 1| > 10× (признак сплита/корп-события, spec Edge Case I3); используется T045 для установки `anomalous_last_bar` в ответе
- [ ] T021 [P] Unit test `ml_forecast/tests/unit/features/test_validators.py` — corrupted-CSV matrix from spec Edge Cases (empty, missing column, non-monotonic, negative close) — each triggers `InvalidOhlcvCsv`
- [ ] T022 Implement `ml_forecast/src/ml_forecast/features/ohlcv_features.py` — 14 OHLCV-derived features (items 1–14 из research.md R5: returns 1/5/20, rsi_14, macd_line/signal, bb_upper/lower_20, atr_14, volume_zscore_20, vwap_20, high_low_range_1, close_to_ema_50, close_to_ema_200). Every function takes full DataFrame + target index `t`, returns scalar; raises `InsufficientHistory` if any feature would need data beyond `t`. Фича #15 `sentiment_score` из R5 живёт отдельно в T023 — здесь не реализуется
- [ ] T023 Implement `ml_forecast/src/ml_forecast/features/sentiment_features.py` — `sentiment_score(ticker, at_time)` reads Redis key; returns `(value, status)`. Multi-source guard (FR-014, research R2, Principle III): при `len(payload.sources) < 2` возвращать `(None, FactorStatus.UNAVAILABLE)` даже если ключ свежий. Добавить unit-тест `tests/unit/features/test_sentiment_monosource.py` с моком Redis payload `{sources: ["tpulse"]}` — ожидание UNAVAILABLE
- [ ] T024 [P] **Anti look-ahead property test** `ml_forecast/tests/unit/features/test_antilookahead.py` — hypothesis strategy generates random OHLCV DataFrames and indices; for every feature and every `t`, mutating bars at indices `>t` MUST NOT change the computed feature value at `t` (FR-021, SC-007)

### Baseline models (used by US1 explanation, US3 backtest, US4 metrics)

- [ ] T025 [P] Implement `ml_forecast/src/ml_forecast/models/base.py` — abstract `BaseForecaster` with `fit(df) -> self`, `predict(df, horizon) -> list[PricePoint]`, `factor_contributions(df) -> list[FactorContribution]`, `family: ModelFamily`
- [ ] T026 [P] Implement `ml_forecast/src/ml_forecast/models/baselines.py::NaiveBaseline` (family=BASELINE_NAIVE, predict = last close repeated) and `OhlcvOnlyBaseline` (family=BASELINE_OHLCV_ONLY, OHLCV-features only, simple linear fit on returns)

### Explanation formatter + forbidden-phrases filter

- [ ] T027 Implement `ml_forecast/src/ml_forecast/inference/explain.py::render(forecast, metrics) -> str` — template from research.md R10. «Значимый фактор» = `abs(contribution) > EXPLAIN_MIN_CONTRIBUTION` (константа, default 0.01). Условие SC-006: ≥3 значимых фактора с `status != UNAVAILABLE`; иначе `raises Unexplainable`. В тексте explanation перечисляются top-3 по убыванию `|contribution|`
- [ ] T028 Implement `ml_forecast/src/ml_forecast/inference/explain.py::check_forbidden(text, loader)` — loads YAML once, substring-scan case-insensitive, raises `ExplanationForbiddenPhrase` on hit (FR-019, fail-closed per R10)
- [ ] T029 [P] Unit test `ml_forecast/tests/unit/inference/test_explain_forbidden.py` — for every phrase in `config/forbidden_phrases.yaml` + each uppercase/lowercase/mixed permutation, `check_forbidden` raises; clean sentences pass

### Freshness guards (FR-002b, used by US1)

- [ ] T030 [P] Implement `ml_forecast/src/ml_forecast/inference/freshness.py` — `check_ohlcv(ticker, timeframe, now) -> SourceFreshness`, `check_sentiment(ticker, now) -> SourceFreshness`; uses Redis `:ts` suffix (R2), thresholds from `Settings`

### gRPC server skeleton

- [ ] T031 Implement `ml_forecast/src/ml_forecast/api/health.py` — `HealthServicer` (grpc.health.v1) reporting SERVING when Redis.ping() and DB.execute("SELECT 1") both succeed
- [ ] T032 Implement `ml_forecast/src/ml_forecast/main.py` — gRPC server bootstrap: `ThreadPoolExecutor(max_workers=8)` (R12), registers Health + placeholder `MlForecastServicer`, graceful shutdown, listens on `settings.grpc_port`
- [ ] T033 [P] Contract test `ml_forecast/tests/contract/test_health.py` — spin server, `grpc.health.v1.Health/Check` returns SERVING; stop Redis stub → returns NOT_SERVING

**Checkpoint**: `docker compose up ml` ⇒ healthy; `pytest tests/unit tests/contract/test_health.py` green; migration applies cleanly; property test for anti look-ahead passes.

---

## Phase 3: User Story 1 — Forecast RPC (Priority: P1) 🎯 MVP

**Goal**: backend может получить прогноз по тикеру с факторами, объяснением, SL/TP и MAPE.

**Independent Test**: поднять сервис с одной предобученной моделью SBER/D1 в артефактном каталоге → `grpcurl Forecast SBER D1 horizon=1` → ответ содержит `predicted_path`, `factors[]≥3`, `explanation` без запрещённых слов, `mape_at_generation`, `source_availability` (quickstart §6).

### Tests for US1

- [ ] T034 [P] [US1] Contract test `ml_forecast/tests/contract/test_forecast_ok.py` — с замоканными Redis (свежие OHLCV + sentiment) и stub `ModelRegistry` вернуть production-модель; assert US-1 AC1 (все обязательные поля; len(factors)≥3 с `|contribution| > 0`; status=OK; advisory-флаги `model_stale=false`, `outside_trading_hours=false` в MOEX-часах, `anomalous_last_bar=false` для нормального бара)
- [ ] T035 [P] [US1] Contract test `ml_forecast/tests/contract/test_forecast_insufficient_history.py` — US-1 AC2 (<500 баров → gRPC FAILED_PRECONDITION, detail='insufficient_history')
- [ ] T036 [P] [US1] Contract test `ml_forecast/tests/contract/test_forecast_horizon_out_of_range.py` — US-1 AC3 (horizon > max_trained → FAILED_PRECONDITION, detail='horizon_out_of_range', указан допустимый max)
- [ ] T037 [P] [US1] Contract test `ml_forecast/tests/contract/test_forecast_degraded_sentiment.py` — US-1 AC4 (Redis sentiment ключ удалён → status=DEGRADED, `sentiment` in factors has status=UNAVAILABLE, explanation содержит маркер сниженной уверенности)
- [ ] T038 [P] [US1] Contract test `ml_forecast/tests/contract/test_forecast_stale_ohlcv.py` — OHLCV `:ts` > 1 бар назад → FAILED_PRECONDITION, detail='stale_ohlcv'
- [ ] T039 [P] [US1] Integration test `ml_forecast/tests/integration/test_forecast_end_to_end.py` — реальный Postgres + Redis контейнер, предобученная ARMAExo на `archive/D1/SBER_D1.csv`, запрос прогноза, проверка schema и не-пустого `suggested_stop_loss`/`suggested_take_profit`

### Implementation for US1

- [ ] T040 [US1] Implement `ml_forecast/src/ml_forecast/models/armaexo.py` — порт из ноутбука `ML_ТМБ.ipynb`: AR порядок + MA + экзогенные признаки из R5; исправлен anti look-ahead (фичи считаются до `t`, не после); публикует `factor_contributions` через коэффициенты модели нормированные к sum(|.|)≈1
- [ ] T041 [US1] Implement `ml_forecast/src/ml_forecast/models/lightgbm_model.py` — fallback из R4; feature importance через SHAP `TreeExplainer`, маппится в `FactorContribution`
- [ ] T042 [P] [US1] Implement `ml_forecast/src/ml_forecast/training/walkforward.py::WalkForwardValidator` (expanding window, ≥5 фолдов, 1 мес на D1, 2 нед на M15 — R3); возвращает `metrics_per_fold`
- [ ] T043 [US1] Implement `ml_forecast/src/ml_forecast/training/hyperparam.py::tune(model_family, X, y, n_trials, time_budget_s)` — Optuna TPE + median pruner (R4); используется `WalkForwardValidator` как целевая функция
- [ ] T044 [US1] Implement `ml_forecast/src/ml_forecast/inference/registry.py::ModelRegistry` — `get_production(ticker, timeframe) -> ModelHandle`, `get_shadow_list(...)`, `load_artifact(handle)`, `register_new(...)`, `promote(...)` (atomic transaction + symlink swap per R6); использует ORM из T017
- [ ] T045 [US1] Implement `ml_forecast/src/ml_forecast/api/forecast_service.py::ForecastServicer.Forecast` — последовательность: (1) validate args (включая ticker + 500-bar порог FR-002); (2) freshness guards (T030); (3) load production model via registry; (4) collect features (OHLCV+sentiment); (5) `model.predict(horizon)`; (6) `factor_contributions`; (7) compute SL/TP = 1.5×ATR/3×ATR; (8) explanation renderer (T027) → forbidden filter (T028); (9) compute advisory flags для ForecastResponse (см. proto `ForecastResponse` fields 12–14): `model_stale` (production-модель старше 14 торговых дней — спека Edge Case), `outside_trading_hours` (текущее время вне 10:00–18:50 МСК пн–пт с поправкой на `config/moex_holidays_2026.yaml`), `anomalous_last_bar` (из T020 `detect_anomalous_bar`); (10) write `inference_log` row (T017 ORM) с advisory-флагами в `source_availability` JSONB для пост-агрегации; (11) return ForecastResponse
- [ ] T046 [US1] Wire `ForecastServicer` into `main.py` (T032); replace placeholder. Add `mape_at_generation` lookup from `model_registry.current_mape` (R8)
- [ ] T047 [US1] Implement status-code mapping in `forecast_service.py`: `InsufficientHistory`→FAILED_PRECONDITION/insufficient_history, `HorizonOutOfRange`→FAILED_PRECONDITION/horizon_out_of_range, `StaleOhlcv`→FAILED_PRECONDITION/stale_ohlcv, `Unexplainable`→FAILED_PRECONDITION/unexplainable, `ModelNotFound`→NOT_FOUND, Redis/DB down→UNAVAILABLE, timeout 60s→DEADLINE_EXCEEDED (per proto comments)

**Checkpoint**: все contract-тесты US1 зелёные; integration test проходит; `grpcurl Forecast` возвращает валидную структуру на seed-данных.

---

## Phase 4: User Story 2 — Train / Retrain + Shadow + Promote (Priority: P1)

**Goal**: админ запускает train, получает shadow-версию, сравнивает метрики, продвигает вручную. Авто-расписание воскресенье ночью.

**Independent Test**: `grpcurl Train SBER D1` → `GetTrainingRun` показывает status=succeeded + per-fold metrics + `promote_decision`; новая запись в `model_registry.state='shadow'`; после `Promote` — предыдущая production → archived, новая → production (US-2 AC1–3, FR-026–FR-030).

### Tests for US2

- [ ] T048 [P] [US2] Contract test `ml_forecast/tests/contract/test_train_enqueue.py` — `Train(SBER,D1)` возвращает `training_run_id>0`, строка в `training_run` status='running', Celery задача в очереди `train_queue`
- [ ] T049 [P] [US2] Contract test `ml_forecast/tests/contract/test_get_training_run.py` — опрос running/succeeded; succeeded даёт `metrics_aggregate`, `fold_metrics[≥5]`, `promote_decision`
- [ ] T050 [P] [US2] Contract test `ml_forecast/tests/contract/test_train_regression_do_not_promote.py` — если metrics новой версии хуже текущей production на порог → `promote_decision='do_not_promote'`, модель НЕ попадает в shadow, НЕ влияет на production (US-2 AC4, FR-009)
- [ ] T051 [P] [US2] Contract test `ml_forecast/tests/contract/test_promote_archive.py` — `Promote(ticker,tf,version)` только для state=shadow; переводит текущую production → archived в одной транзакции; повторный `Promote` того же version → FAILED_PRECONDITION
- [ ] T052 [P] [US2] Contract test `ml_forecast/tests/contract/test_shadow_report.py` — `GetShadowReport` возвращает production_metrics, shadow_metrics, `trading_sessions_elapsed` корректно отсчитанное (pre-condition: shadow_prediction заполнен mock-данными)
- [ ] T053 [P] [US2] Integration test `ml_forecast/tests/integration/test_train_shadow_promote_cycle.py` — полный цикл: Train → ждать succeeded → state=shadow → фейк-Forecast вызовы 5 раз (shadow_prediction пополняется) → resolve фактами → Promote → следующий Forecast использует новую production

### Implementation for US2

- [ ] T054 [US2] Implement `ml_forecast/src/ml_forecast/training/pipeline.py::TrainPipeline.run(ticker, timeframe, trigger)` — загрузить CSV из `archive/`, `validate_ohlcv_csv`, feature-extract, walk-forward (T042), hyperparam search (T043), persist artifact + metadata.json (T019), register row in `model_registry`. **Promotion gating (FR-022, Principle V)**: одновременно обучать NaiveBaseline на том же walk-forward; если `new_r2 < naive_r2` или `new_r2 < 0` → `state='do_not_promote'`, запись в `training_run.comparison_to_prev.vs_naive` фиксируется явно; иначе сравнение с предыдущей production → `shadow` или `do_not_promote` по регрессии. **Family-switch (FR-025)**: если `model_family == ARMAEXO` и aggregate MAPE по walk-forward не укладывается в SC-001 (≤5% M15 / ≤3% D1 для blue-chip тикеров из SC-001 списка), запустить второй прогон с `LIGHTGBM`; лучший по MAPE продвигается в shadow; в `training_run` пишется `trigger=FAMILY_SWITCH` с обоснованием (`{"from":"armaexo","to":"lightgbm","delta_mape":..}`)
- [ ] T055 [US2] Implement `ml_forecast/src/ml_forecast/training/scheduler.py` — Celery app + task `train_pair(ticker, timeframe, trigger)` (вызывает T054), Celery Beat schedule `0 2 * * 0` → `retrain_all_production_models` (R9)
- [ ] T056 [US2] Implement `ml_forecast/src/ml_forecast/api/train_service.py::TrainServicer.Train` — pre-checks: (a) **blue-chip guard** (FR-012): если `tickers.is_blue_chip=false` и тикер не в whitelist v1.1 → `FAILED_PRECONDITION`/`non_blue_chip_mvp`; (b) **concurrent-train guard** (spec Edge Case): взять `pg_advisory_xact_lock(hashtext(ticker||timeframe))` в той же транзакции enqueue — второй параллельный Train при том же ключе получит `FAILED_PRECONDITION`/`already_running`, если не передан `force=true`. Далее — enqueue через Celery, возвращает `training_run_id`. Contract-тесты T048/T049 дополнить кейсом concurrent-train (второй вызов без `force` падает, с `force=true` проходит)
- [ ] T057 [US2] Implement `ml_forecast/src/ml_forecast/api/train_service.py::TrainServicer.GetTrainingRun` — читает из `training_run` ORM, маппит в proto
- [ ] T058 [US2] Implement `ml_forecast/src/ml_forecast/inference/shadow.py::enqueue_shadow_predictions(request_id, ticker, timeframe, horizon, features_snapshot)` — после `Forecast` (T045) публикует Celery-таск `shadow_predict` на каждую активную shadow-модель (R7); worker пишет в `shadow_prediction`
- [ ] T059 [US2] Implement Celery task `shadow_resolve` (каждые 15 мин) — находит `shadow_prediction` со `resolved_at IS NULL` и `t+horizon ≤ now`, читает фактические цены из Redis/archive, заполняет `actual_path` + `abs_error_pct`
- [ ] T060 [US2] Wire `enqueue_shadow_predictions` в `forecast_service.py` (T045) **после** возврата основного ответа клиенту (fire-and-forget Celery publish; не блокирует critical path per R7)
- [ ] T061 [P] [US2] Implement `ml_forecast/src/ml_forecast/api/admin_service.py::AdminServicer.Promote` — transactional: load shadow model, swap symlink (T019), archive previous production, INSERT `promoted_at` — использует `SELECT ... FOR UPDATE` + partial UK из T016 как safety net
- [ ] T062 [P] [US2] Implement `AdminServicer.Archive` — state→archived, nullify symlink if was production; в этом случае требуется promote чего-то нового перед следующим Forecast (иначе NOT_FOUND)
- [ ] T063 [P] [US2] Implement `AdminServicer.GetShadowReport` — агрегация shadow_prediction vs inference_log за период shadow-модели; `trading_sessions_elapsed` считается по календарю MOEX (пн–пт без праздников; статический список праздников МОЕХ в `config/moex_holidays_2026.yaml`)
- [ ] T064 [P] [US2] Implement `AdminServicer.ListModels` — фильтры по ticker / timeframe / state
- [ ] T065 [US2] Wire TrainServicer + AdminServicer в `main.py`

**Checkpoint**: весь US2 contract-матрица зелёная; `test_train_shadow_promote_cycle` проходит на фикстурных CSV; Celery Beat schedule видно в `celery -A ml_forecast.training.scheduler beat`.

---

## Phase 5: User Story 3 — Backtest (Priority: P2)

**Goal**: админ запускает бэктест production-модели на 6+ мес и получает отчёт с baselines.

**Independent Test**: `grpcurl Backtest SBER D1 period_start=2024-02-01 period_end=2024-08-01` → ответ содержит `model_metrics`, `naive_baseline_metrics`, `ohlcv_only_baseline_metrics`; agg MAPE модели ниже обоих baselines на ≥10% относительных (SC-009).

### Tests for US3

- [ ] T066 [P] [US3] Contract test `ml_forecast/tests/contract/test_backtest_basic.py` — валидный период, 3 набора метрик присутствуют, `signal_count` > 0
- [ ] T067 [P] [US3] Integration test `ml_forecast/tests/integration/test_backtest_beats_baselines.py` — на `archive/D1/SBER_D1.csv`, период последние 6 мес; assert model_metrics.mape < naive.mape * 0.9 AND < ohlcv_only.mape * 0.9 (SC-009)

### Implementation for US3

- [ ] T068 [US3] Implement `ml_forecast/src/ml_forecast/backtest/runner.py::run(model_handle, period_start, period_end)` — sliding eval: для каждого бара в периоде воспроизводит `ForecastServicer` логику на историческом срезе (anti look-ahead!), параллельно NaiveBaseline (T026) и OhlcvOnlyBaseline (T026); вычисляет win rate через фиксированные правила SL/TP; возвращает все 3 MetricsBundle + массив (предсказ, факт, t)
- [ ] T069 [US3] Implement `ml_forecast/src/ml_forecast/api/backtest_service.py::BacktestServicer.Backtest` — вызывает T068, persist в `backtest_report` (T017), возвращает proto
- [ ] T070 [US3] Wire BacktestServicer в `main.py`

**Checkpoint**: бэктест даёт стабильное превосходство над baselines на SBER/D1; отчёт читается из БД.

---

## Phase 6: User Story 4 — Observability (Priority: P2)

**Goal**: inference-журнал полный, метрики в Prometheus, алерты на деградацию, health-check раз в минуту обновляет `pipeline_statuses`.

**Independent Test**: 100 последовательных `Forecast`-вызовов → 100 строк в `inference_log`; `curl :9100/metrics` содержит `ml_forecast_latency_seconds_bucket`, `ml_forecast_forecast_total{status=...}`; при искусственном slowdown >30с avg за 5м — алерт-правило срабатывает (US-4 AC1).

### Tests for US4

- [ ] T071 [P] [US4] Integration test `ml_forecast/tests/integration/test_inference_log_written.py` — N forecast calls → N rows in `inference_log` с корректными status/latency/model_id/mape_at_generation/source_availability
- [ ] T072 [P] [US4] Integration test `ml_forecast/tests/integration/test_prometheus_exporter.py` — после forecast’ов `/metrics` содержит ожидаемые метрики; счётчики монотонно растут
- [ ] T073 [P] [US4] Integration test `ml_forecast/tests/integration/test_healthcheck_pipeline_status.py` — запуск health-task → строка `pipeline_statuses.pipeline_name='ml_forecast'` обновлена; stop Redis → status=unhealthy

### Implementation for US4

- [ ] T074 [P] [US4] Implement `ml_forecast/src/ml_forecast/observability/metrics.py` — `prometheus_client` экспортер на `:9100`: `ml_forecast_latency_seconds` (histogram), `ml_forecast_forecast_total{status}` (counter), `ml_forecast_train_duration_seconds` (histogram), `ml_forecast_stale_rate{source}` (gauge), `ml_forecast_current_mape{ticker,timeframe}` (gauge)
- [ ] T075 [US4] Hook metrics в `forecast_service.py` (T045): measure latency, increment status counter, update stale gauges
- [ ] T076 [P] [US4] Celery task `recompute_daily_metrics` (кожен ранок MSK): по каждой production-модели — последние 30 resolved `inference_log` → MAPE, win rate; UPDATE `model_registry.current_mape` + INSERT row in shared `model_metrics` table (R8, FR-016)
- [ ] T077 [P] [US4] Celery periodic task `health_check` (каждую минуту, FR-017): проверяет (а) Redis reachable, (б) Postgres reachable, (в) нет production-модели старше 14 дней без trained; UPDATE `pipeline_statuses.ml_forecast` и отсылает алерт через `log.error` (перехват логгером → Telegram-бот admin — вне scope этой фичи)
- [ ] T078 [P] [US4] Create `ml_forecast/config/alerts/prometheus_rules.yaml` — 3 алерта: `MlForecastLatencyHigh` (avg_5m > 30s), `MlForecastStaleRateHigh` (> 20% за 15 мин), `MlForecastHealthDown` (health-check failure) — US-4 AC1/AC2
- [ ] T079 [US4] Wire `recompute_daily_metrics` и `health_check` в Celery Beat schedule (T055)

**Checkpoint**: dashboards read metrics; `inference_log` fills; alerts fire under simulated slow inference.

---

## Phase 7: User Story 5 — Liquidity Classifier (Priority: P3)

**Goal**: классификатор «голубая фишка / прочая» с recall ≥0.80 по редкому классу, устраняя 0%-recall провал ноутбука (SC-010).

**Independent Test**: `grpcurl ClassifyLiquidity` на 249 тикерах D1 → `blue_chip_recall ≥ 0.80` и `other_recall ≥ 0.90` на стратифицированном holdout.

### Tests for US5

- [ ] T080 [P] [US5] Contract test `ml_forecast/tests/contract/test_classify_liquidity.py` — проверяет возврат `blue_chip_recall`, `other_recall`, `total_tickers=249`; assert `classification_run` row persisted
- [ ] T081 [P] [US5] Integration test `ml_forecast/tests/integration/test_classifier_meets_recall.py` — на полном наборе `archive/D1/*_D1.csv`, assert `blue_chip_recall ≥ 0.80`, `other_recall ≥ 0.90` (SC-010)

### Implementation for US5

- [ ] T082 [US5] Implement `ml_forecast/src/ml_forecast/models/classifier.py::LiquidityClassifier.fit(df, labels)` — RandomForest(class_weight='balanced', n_estimators=300) + stratified split; опциональный SMOTE через `imblearn` если recall < 0.80 на первом проходе (R11)
- [ ] T083 [US5] Implement `ml_forecast/src/ml_forecast/features/ticker_features.py::extract_from_archive()` — для каждого тикера в `archive/D1/`: средний объём, волатильность, рост/день, общий возраст истории → DataFrame; label `is_blue_chip` из seed-списка из 13 уникальных тикеров: {SBER, GAZP, LKOH, GMKN, ROSN, NVTK, TATN, MGNT, YNDX, MTSS, VTBR, ALRS, PLZL}
- [ ] T084 [US5] Implement `ml_forecast/src/ml_forecast/api/classify_service.py::ClassifyServicer.ClassifyLiquidity` — extract features (T083), fit (T082), compute per-class recall, persist `classification_run` (T017), return proto
- [ ] T085 [US5] Wire ClassifyServicer в `main.py`

**Checkpoint**: SC-010 достигнут; row in `classification_run` для каждого запуска.

---

## Phase 8: Polish & Cross-Cutting Concerns

**Purpose**: приведение в запускаемое состояние; smoke-тест quickstart.

- [ ] T086 [P] Implement `ml_forecast/scripts/quickstart_seed_redis.py` из quickstart §6 — читает последние N баров CSV и пишет в Redis ключи (OHLCV + stub sentiment), используется smoke-test’ом и в ручной отладке
- [ ] T087 [P] Implement `ml_forecast/Makefile` target `smoke` из quickstart §8 (alembic → proto → compose up → train → promote → seed redis → forecast → assertion via grpcurl+jq)
- [ ] T088 [P] Add `ml_forecast/README.md` — ссылки на [spec.md](../specs/001-ml-forecast-service/spec.md), [plan.md](../specs/001-ml-forecast-service/plan.md), [quickstart.md](../specs/001-ml-forecast-service/quickstart.md); how to run, how to add new ticker
- [ ] T089 Append `ml_forecast` фрагмент в корневой `docker-compose.yaml` репозитория (единый стек с будущими backend / sentiment / data_collector)
- [ ] T090 Run full `pytest ml_forecast/tests` → all green; fix flakes if any
- [ ] T091 Run `make smoke` end-to-end against local compose stack; capture log; зафиксировать failures as follow-up issues
- [ ] T092 [P] Add GitHub Actions workflow `.github/workflows/ml_forecast.yml` — run `ruff`, `mypy`, `pytest -q`, `grpcio-tools` proto-lint on push/PR to paths `ml_forecast/**` or `specs/001-ml-forecast-service/contracts/**`
- [ ] T093 Verify constitution re-check against final codebase (manual pass through `plan.md` Constitution Check) — update plan.md Constitution Check post-impl column if anything drifted
- [ ] T094 [P] Implement Celery periodic task `artifact_rotation` в `ml_forecast/src/ml_forecast/storage/artifact_store.py` (раз в сутки в окне обслуживания): для каждой пары (ticker, timeframe) оставлять последний `production` + **минимум 3 последних `archived`** артефакта; более старые `archived` физически удалять (и файл, и row `model_registry.state=archived`) — закрывает FR-010 retention policy
- [ ] T095 [P] Contract test для advisory-флагов `ml_forecast/tests/contract/test_forecast_advisory_flags.py` — 3 кейса: (а) `model_stale=true` если модель обучена >14 торговых дней назад; (б) `outside_trading_hours=true` при искусственном patching `datetime.now()` на 22:00 МСК; (в) `anomalous_last_bar=true` при подсовывании бара с 10× средним объёмом. Закрывает I1/I2/I3
- [ ] T096 Integration regression-gate test `ml_forecast/tests/integration/test_blue_chips_sc_targets.py` — обучает 5 production-моделей на {SBER, GAZP, LKOH, GMKN, ROSN} × {D1, M15} из `archive/`, assert: aggregate MAPE ≤ 5% (M15, horizon=4) и ≤ 3% (D1, horizon=1), directional accuracy ≥ 55%, win rate ≥ 52% в встроенном бэктесте на последних 6 мес. Закрывает SC-001/002/003 как regression-gate. Отмечен xfail на раннем этапе, unblock после US2
- [ ] T097 [P] Create `ml_forecast/docs/runbook_forbidden_phrases.md` — процедура добавления/удаления фразы из `config/forbidden_phrases.yaml` (ответственный, список тестов для регрессии T029, как деплоится без рестарта сервиса через SIGHUP или периодический reload). Закрывает A2
- [ ] T098 Update plan.md Constitution Check Principle III: уточнить, что multi-source guard (FR-014) реализуется в T023 (проверка `sources[] ≥ 2` на стороне ML-сервиса); плюс сверка advisory-флагов с Edge Cases в spec.md — галочку возле строки «факторы, деградации источников» подтвердить пост-impl

---

## Dependencies & Execution Order

### Phase dependencies

- **Phase 1 Setup**: no blocker, start immediately
- **Phase 2 Foundational**: depends on Setup done; blocks ALL user stories
- **Phase 3 US1 (P1)**: depends on Phase 2
- **Phase 4 US2 (P1)**: depends on Phase 2 AND Phase 3 (US2 shadow mechanism T060 hooks into Forecast RPC from T045; if you skip US1, US2 still needs Forecast flow for shadow to observe — treat US1 as strict prerequisite for US2)
- **Phase 5 US3 (P2)**: depends on Phase 2 only; independent of US1/US2 (backtest reads archive directly, uses same baselines)
- **Phase 6 US4 (P2)**: depends on Phase 3 (US1 forecast path must exist to instrument) + Phase 4 (train duration metric needs train pipeline)
- **Phase 7 US5 (P3)**: depends on Phase 2 only; fully independent
- **Phase 8 Polish**: depends on all targeted user stories

### Intra-story dependencies

Within each user story:
- Tests (contract + integration) are written first and expected to FAIL
- Models → services → RPC servicer → wiring into `main.py`
- Each `Checkpoint` = a demoable increment

### Critical path

Setup → Foundational → US1 → US2 (shadow on top) → US4 (observability) = полноценный production-ready MVP.
US3 и US5 — параллельные ветки поверх Foundational.

---

## Parallel Execution Examples

### After Phase 1 Setup: foundational tasks with [P]

```text
Parallel batch 1 (no inter-dep, different files):
- T011 domain/timeframe.py
- T012 domain/model_state.py
- T013 domain/factor.py
- T014 domain/forecast.py
- T015 storage/postgres.py
- T017 storage/orm.py
- T018 storage/redis_client.py
- T019 storage/artifact_store.py
- T020 features/validators.py
- T021 tests/unit/features/test_validators.py
- T024 tests/unit/features/test_antilookahead.py
- T025 models/base.py
- T026 models/baselines.py
- T029 tests/unit/inference/test_explain_forbidden.py
- T030 inference/freshness.py
- T033 tests/contract/test_health.py
```

### Within US1: all contract tests first in parallel

```text
Parallel batch 2:
- T034 test_forecast_ok
- T035 test_forecast_insufficient_history
- T036 test_forecast_horizon_out_of_range
- T037 test_forecast_degraded_sentiment
- T038 test_forecast_stale_ohlcv
```

### Admin RPC handlers in parallel (US2)

```text
Parallel batch 3 (after T054 pipeline exists):
- T061 admin_service.Promote
- T062 admin_service.Archive
- T063 admin_service.GetShadowReport
- T064 admin_service.ListModels
```

---

## Implementation Strategy

### MVP First (US1 only)

1. Phase 1 Setup (T001–T009)
2. Phase 2 Foundational (T010–T033) — наиболее объёмный блок
3. Phase 3 US1 (T034–T047)
4. **STOP + VALIDATE**: `grpcurl Forecast` на предобученной модели (мокаем train через ручной `scripts/preload_model.py`)
5. Demo: живой прогноз по SBER/D1 в чате

### Incremental Delivery

1. Setup + Foundational → инфраструктура готова
2. + US1 → Demo-able MVP (прогнозы по одной предобученной модели)
3. + US2 → Train/Shadow/Promote, можно переобучать живьём (production-ready)
4. + US4 → Observability, можно выкатить под нагрузку
5. + US3 → Backtest, админ может валидировать перед релизом
6. + US5 → Классификатор ликвидности, готов расширять универс

### Solo-dev strategy

Параллелится через последовательные короткие сессии: в один день — все `[P]` из одной фазы (они не конфликтуют по файлам).

---

## Notes

- `[P]` = разные файлы, никаких inter-task deps в текущей фазе
- `[USn]` связывает задачу с user story (для traceability/rollback)
- Каждый US завершается Checkpoint’ом — stopping point для ревью
- Тесты в каждом US пишутся первыми и должны падать до реализации
- Коммит после каждой задачи или логической группы
- Избегать: размытых формулировок, конфликтов по одному файлу, кросс-story зависимостей, ломающих независимость US3/US5
