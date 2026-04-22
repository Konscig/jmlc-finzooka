# Phase 0 Research: ML Forecast Service

**Feature**: 001-ml-forecast-service | **Date**: 2026-04-19

Цель этого документа — закрыть все `NEEDS CLARIFICATION` и
неочевидные технические выборы, всплывшие при заполнении
`plan.md`. Каждое решение фиксируется форматом Decision /
Rationale / Alternatives considered, чтобы Phase 1 / 2
опирались на воспроизводимую аргументацию.

---

## R1. Формат gRPC-контракта и версионирование

**Decision**: Контракт живёт в файле `contracts/ml_forecast.proto`
с пакетом `finzooka.ml.v1`. Только syntax = proto3. Сообщения
неизменные внутри minor-версии; добавление полей — только
через новые теги с сохранением совместимости. Мажорный bump
(`v2`) — только при несовместимых изменениях; старая версия
параллельно обслуживается минимум один релиз.

**Rationale**:
- `proto3` совпадает с экосистемой T-Invest (C4: Data Collector
  общается с T-Invest API по gRPC/REST — уже знакомая команде
  сущность).
- Префикс `finzooka.ml.v1` даёт пространство для будущих
  сервисов (`finzooka.sentiment.v1`, `finzooka.data.v1`) без
  коллизий типов.
- Явное версионирование пакета соответствует Principle II
  «Navigator, Not Autopilot» в расширенной интерпретации: явные,
  предсказуемые изменения контракта для потребителей.

**Alternatives considered**:
- JSON/REST (FastAPI) — добавляет накладные расходы на сериализацию
  и требует ручных schema-валидаторов; gRPC даёт строгие типы «из
  коробки».
- proto2 — избыточен для greenfield-сервиса; opt-in / opt-out
  полей не нужны.
- Apache Thrift — требует отдельного runtime, вне стандартной
  Python-экосистемы Finzooka.

---

## R2. Контракт ключей Redis (вход сервиса)

**Decision**: Договариваемся о двух пространствах ключей
(владельцы — `Data Collector` и `Sentiment Pipeline` соответственно;
этот сервис — только читатель):

```
ohlcv:<ticker>:<timeframe>:last          → JSON с последними N баров
ohlcv:<ticker>:<timeframe>:last:ts       → ISO-timestamp последнего бара
sentiment:<ticker>:agg                   → JSON агрегата
sentiment:<ticker>:agg:ts                → ISO-timestamp расчёта
```

Полезная нагрузка `ohlcv:*:last`:
```json
{
  "bars": [{"dt": "...", "o": ..., "h": ..., "l": ..., "c": ..., "v": ...}],
  "source": "tinvest",
  "schema_version": 1
}
```

Полезная нагрузка `sentiment:*:agg`:
```json
{
  "score": -0.12,
  "confidence": 0.78,
  "sources": ["tpulse", "news_headlines"],
  "window_seconds": 3600,
  "schema_version": 1
}
```

TTL выставляется владельцами ключей; ML-сервис валидирует
свежесть через суффикс `:ts` (FR-002b: OHLCV ≤ 1× длительности
таймфрейма, sentiment ≤ 60 мин).

**Rationale**:
- Отдельный ключ `:ts` избавляет от парсинга payload только для
  проверки свежести — дешёвый `GET` + сравнение.
- Поле `sources[]` в sentiment-ключе обслуживает Multi-Factor Gate:
  если длина < 2, ML-сервис считает sentiment «недоверенным» и
  включает fallback (часть Principle III).
- `schema_version` даёт явный путь миграции без поломки
  потребителей.

**Alternatives considered**:
- Redis Streams — избыточно для read-latest-value; Streams
  оправданы только если ML-сервису нужна история обновлений.
- Pub/Sub — не решает задачу «получить последний снапшот при
  запуске»; требует отдельного cold-start-механизма.
- Протобуф внутри Redis — читабельнее JSON для отладки; potential
  future optimization, не для MVP.

---

## R3. Стратегия walk-forward валидации

**Decision**: Expanding-window walk-forward с минимум 5 фолдами,
размер тестового окна = 1 календарный месяц на таймфрейме D1
и 2 торговые недели на таймфрейме M15. Обучение каждого фолда
включает полный feature-engineering с использованием ТОЛЬКО
данных до начала текущего test-окна (anti look-ahead guard
проверяется hypothesis-тестом).

**Rationale**:
- Expanding window (а не sliding) лучше отражает производственный
  сценарий: в проде мы постоянно добавляем новые данные, не
  отбрасываем старые.
- 5 фолдов — минимум, дающий осмысленные mean ± std по MAPE; для
  blue chips с ≥5 лет истории это не урезает обучающую выборку
  критично.
- Разные размеры окон по таймфреймам — D1 должен охватывать
  разные рыночные режимы (минимум месяц), M15 — реалистичное окно
  «пары недель», чтобы поймать смены режима внутри торгового дня.

**Alternatives considered**:
- Sliding (rolling) window фиксированного размера — отбрасывает
  исторические паттерны; хуже для ARMA-семейства, которое любит
  длинные серии.
- Blocked time-series CV (без перекрытия) — проще, но даёт меньше
  фолдов при той же истории; менее стабильные метрики.
- Nested CV (inner loop для hyperparam + outer для validation) —
  теоретически правильнее, но удваивает время train (нарушение
  SC-005 ≤10 мин). Заменяется более скромным: hyperparameter-поиск
  делаем на первой половине, фиксируем параметры, затем walk-forward
  на оставшейся для оценки.

---

## R4. Библиотека hyperparameter search

**Decision**: **Optuna** (TPE sampler по умолчанию, медианный pruner
для ранней остановки неудачных trial’ов). Бюджет на сессию поиска
= `n_trials=50` или 5 минут суммарно — что наступит раньше.
Конфигурация хранится в `training/hyperparam.py` и параметризуется
per-ticker × timeframe.

**Rationale**:
- TPE (Tree-structured Parzen Estimator) на практике быстрее
  grid-search в 10–30× при сопоставимой точности на ≤10 параметрах.
- Pruner важен для времени train SC-005: плохой трайл отсекается
  после первого фолда walk-forward.
- Optuna нативно интегрируется со `scikit-learn` / `statsmodels` /
  `lightgbm` — один API для всех моделей.

**Alternatives considered**:
- `GridSearchCV` / `RandomizedSearchCV` из scikit-learn — менее
  эффективны, нет pruning; приемлемо только для < 5 параметров.
- `Ray Tune` — мощнее, но тянет зависимость на Ray (≈250 МБ
  контейнер) и распределённый sheduler; избыточно для solo-dev
  на одном хосте (Principle VII).
- `hyperopt` — предшественник Optuna; сейчас мало поддерживается.

---

## R5. Набор технических индикаторов (feature set v1)

**Decision**: На MVP фиксируем **15 фич** на каждый бар, все
вычисляются с anti look-ahead guard и заворачиваются в
`features/ohlcv_features.py`:

| # | Feature | Window | Группа |
|---|---------|--------|--------|
| 1 | `return_log_1` | 1 | Returns |
| 2 | `return_log_5` | 5 | Returns |
| 3 | `return_log_20` | 20 | Returns |
| 4 | `rsi_14` | 14 | Momentum |
| 5 | `macd_line` | 12/26 | Momentum |
| 6 | `macd_signal` | 12/26/9 | Momentum |
| 7 | `bb_upper_20` | 20 | Volatility |
| 8 | `bb_lower_20` | 20 | Volatility |
| 9 | `atr_14` | 14 | Volatility |
| 10 | `volume_zscore_20` | 20 | Volume |
| 11 | `vwap_20` | 20 | Volume |
| 12 | `high_low_range_1` | 1 | Range |
| 13 | `close_to_ema_50` | 50 | Trend |
| 14 | `close_to_ema_200` | 200 | Trend |
| 15 | `sentiment_score` | live | Exogenous |

ATR-14 переиспользуется для расчёта `suggested_stop_loss` /
`suggested_take_profit` (обычно 1.5 × ATR для SL, 3 × ATR для TP —
цифры из ноутбука, параметризуемы).

**Rationale**:
- Покрываем 5 стандартных групп индикаторов, требуемых отраслевыми
  гайдами AI-трейдинга (Momentum / Volatility / Trend / Volume /
  Returns).
- 15 фич — осмысленный размер для ARMAExo (избежать переобучения
  при 500 наблюдениях минимума) и уже достаточный для LightGBM
  fallback.
- ATR-14 из ноутбука сохраняется как базовый risk-метрик.

**Alternatives considered**:
- 50+ фич «всё подряд» — нарушение Principle VII (простота) и
  риск переобучения ARMAExo; feature selection потом можно
  расширить.
- Только «признаки ноутбука» (daily/weekly/monthly growth, Sharpe)
  — повторит провал ARMAExo по R² (см. анализ в ноутбуке).
- Использовать `ta-lib` вместо `ta` (python-package) — C-библиотека
  с установочными хлопотами; решение «чистая Python» даёт меньше
  трения для Docker-образа.

---

## R6. Формат сохранения артефактов моделей

**Decision**: `joblib.dump(model, ...)` на локальную ФС по пути
`data/models/<ticker>/<timeframe>/<model_version>.joblib` с
компрессией `compress=3`. Рядом лежит `metadata.json` с
training-метриками и хешем входного CSV. Симлинк
`data/models/<ticker>/<timeframe>/production.joblib` указывает на
текущую production-версию (атомарная смена — `os.replace`).

**Rationale**:
- `joblib` — стандарт де-факто для scikit-learn / statsmodels /
  lightgbm; отлично сжимает numpy-массивы.
- Файл-based реестр дёшев, отлаживается через `ls` + `file`, не
  требует внешней БД для артефактов.
- Атомарный `os.replace` симлинка даёт нулевой downtime при
  promote.
- `metadata.json` рядом с весами повышает воспроизводимость (+ в
  PostgreSQL дублируется для SQL-запросов).

**Alternatives considered**:
- Pickle напрямую — теряем компрессию и совместимость по версиям;
  joblib = pickle + оптимизации для numpy.
- ONNX — даёт cross-runtime-инференс, но ARMAExo и кастомные
  фичи не экспортятся целиком; зря усложняет MVP.
- MLflow / Weights & Biases — overkill для solo-dev, тянут
  внешние зависимости (Principle VII).
- Хранить в PostgreSQL как `bytea` — раздувает БД и усложняет
  бэкап-политику.

---

## R7. Реализация shadow-inference без блокировки production

**Decision**: В RPC `Forecast` обработчик:
1. Синхронно выполняет production-модель → формирует ответ клиенту.
2. Перед `return` ставит задачу в Celery-очередь `shadow_predict`
   со всеми аргументами запроса; Celery-воркер исполняет все
   shadow-модели параллельно и пишет результаты в `shadow_prediction`.

Факт (фактическая цена через `horizon` баров) дописывается
периодической задачей `shadow_resolve` (каждые 15 минут) —
читает из Redis актуальные бары и проставляет `actual_value` и
`abs_error` по созревшим предсказаниям.

**Rationale**:
- Клиент не платит latency за shadow — production-модель
  возвращается сразу.
- Celery уже в стеке (Principle VII, нулевая новая зависимость).
- Отделение «сделать предсказание» от «сравнить с фактом»
  реалистично для разных таймфреймов (на D1 факт будет известен
  через 1 день).

**Alternatives considered**:
- Синхронно выполнять shadow параллельно production в пределах
  того же RPC — раздует p95 при 2+ shadow-моделях, ломает SC-004.
- Отдельный gRPC-клиент-коллектор, подписанный на production-
  ответы — усложнение API без ощутимой выгоды.
- Redis stream `shadow_predictions_queue` вместо Celery — дубль
  функциональности Celery на Redis-брокере.

---

## R8. Как `mape_at_generation` попадает в ответ

**Decision**: Для каждой production-модели поддерживается поле
`current_mape` в таблице `model_registry`, обновляемое ежедневным
Celery-таском `recompute_daily_metrics`: пересчёт MAPE по последним
30 резолвнутым inference из `inference_log` для данной пары
(ticker, timeframe). Значение отдается в поле `mape_at_generation`
любого нового `Forecast`.

**Rationale**:
- 30 последних resolved inference — достаточно для стабильной
  метрики на MVP трафике (5–25 rpm → за 30 запросов ≈1–6 часов
  данных).
- Ежедневный пересчёт совпадает с частотой агрегатов в
  admin-панели (FR-016).
- Не блокируем critical path inference вычислением метрики
  на лету.

**Alternatives considered**:
- Пересчитывать `current_mape` после каждого resolve —
  избыточная нагрузка на PostgreSQL; разница в метрике между
  двумя резолвами ниже шумового порога.
- Возвращать walk-forward MAPE обучения как `mape_at_generation`
  — неверно отражает реальную свежую точность; нарушает дух
  Principle V.

---

## R9. Celery-расписание автоматического переобучения

**Decision**: Celery Beat с cron-расписанием
`0 2 * * 0` (воскресенье 02:00 МСК) запускает задачу
`retrain_all_production_models`, которая перебирает все пары
(ticker, timeframe) в статусе `production` и ставит каждую в
очередь `train_queue` (Celery concurrency = 1 на worker, чтобы
уважать 4 CPU бюджет и не конкурировать с другими задачами
стека). Ручной запуск через admin RPC `Train(force=true)` добавляет
задачу в ту же очередь вне графика.

**Rationale**:
- Воскресенье 02:00 МСК — заведомо вне MOEX-сессии и без
  конкурирующего трафика.
- Concurrency=1 гарантирует, что 10 тренировок × 10 мин = ≤2 часа,
  укладываясь в ночное окно.
- Общая очередь train_queue для ручного и расписанного режимов
  даёт единственный путь артефакта в реестр — проще дебажить.

**Alternatives considered**:
- Одновременный запуск всех 10 тренировок параллельно — перегружает
  4 CPU (ARMAExo и LightGBM однопоточны по дефолту, но scikit-learn
  при n_jobs=-1 может).
- Расписание «каждую ночь» — превышает бюджет сервера и не даёт
  выигрыша по свежести на модели, которая на M15/D1 меняется
  медленнее.

---

## R10. Генерация `explanation` и чёрный список слов

**Decision**: Шаблонный генератор (без LLM) в
`inference/explain.py`. Шаблон: `«{direction_verb} на {pct}% в
течение {horizon} {bar_unit}. Основные факторы: {top_3_factors}.
Текущая MAPE модели {ticker}/{timeframe}: {mape}%.»` Перед
возвратом `explanation` прогоняется через фильтр по YAML-файлу
`config/forbidden_phrases.yaml` — содержит substring-паттерны
(регистронезависимо); при совпадении поднимается исключение
`ExplanationForbiddenPhrase`, inference отклоняется (безопасный
default).

**Rationale**:
- Шаблон — детерминированный, ревьюабельный, не требует Mistral
  API (нет rate-limit в critical path).
- Чёрный список в YAML легко обновляется админом без деплоя кода.
- Fail-closed поведение (блокировать ответ) сильнее fail-open
  (молча пропускать) по Principle VI.

**Alternatives considered**:
- LLM-генерация `explanation` (Mistral) — плюс читаемость,
  минусы: rate-limit и latency ~3–10с на бесплатном плане,
  плюс риск галлюцинаций.
- Только шаблон без фильтра — шаблон сам по себе может
  воспроизвести триггерное слово через данные (название тикера,
  новостной заголовок в future); фильтр — страховка.
- Генерация `explanation` на стороне backend — теряется инвариант
  «сервис гарантирует отсутствие слов из чёрного списка» (FR-019).

---

## R11. Классификатор ликвидности (US-5) — решение дисбаланса классов

**Decision**: `RandomForestClassifier(class_weight='balanced',
n_estimators=300)` + стратифицированный `train_test_split` +
опциональный `imblearn.SMOTE` на обучающей части (не в test).
Порог отсечения для класса «голубая фишка» подбирается по
максимуму F1 на cross-validation.

**Rationale**:
- `class_weight='balanced'` даёт мгновенный буст recall редкого
  класса без генерации синтетики.
- SMOTE как второй эшелон — если `class_weight` не вытаскивает
  SC-010 (recall ≥0.80).
- Стратификация устраняет проблему ноутбука «в тесте 3 blue chips
  из 50».

**Alternatives considered**:
- `GradientBoostingClassifier` с `scale_pos_weight` — сопоставимо,
  но RandomForest даёт out-of-the-box feature importance и
  проще объяснить (Principle I даже для классификатора).
- Oversampling через дублирование — хуже SMOTE по обобщению.
- Undersampling большого класса — выбрасывает ценные данные
  (247 тикеров → <30), разрушает обучающую выборку.

---

## R12. Гранулярность concurrency для Forecast RPC

**Decision**: gRPC-сервер конфигурируется `ThreadPoolExecutor(
max_workers=8)` на MVP. Inference внутри RPC — однопоточный,
parallelism идёт через несколько RPC одновременно. Пиковая
пропускная способность ~8 одновременных прогнозов × ~15 с =
~32 rpm — покрывает MVP-нагрузку 5–25 rpm с запасом.

**Rationale**:
- 8 воркеров × 1.5 ГБ на каждый ARMAExo in-memory + 4 CPU =
  укладываемся в бюджет 12 ГБ.
- Запас 8× над MVP-нагрузкой достаточен для spike’ов.
- Рост до 100 DAU (500 rpm) требует отдельной итерации —
  scale-out через дополнительные контейнеры ml-service (stateless
  для inference, state в PostgreSQL/Redis).

**Alternatives considered**:
- Single-threaded — деградирует даже на MVP при 5+ параллельных
  клиентах.
- `max_workers=32` — чрезмерно для 4 CPU, приведёт к context-
  switch-оверхеду.

---

## Summary Decisions Table

| ID | Topic | Decision |
|----|-------|----------|
| R1 | gRPC package | `finzooka.ml.v1`, proto3 |
| R2 | Redis schema | `ohlcv:*`, `sentiment:*` + `:ts` suffix, JSON payload |
| R3 | Walk-forward | Expanding window, ≥5 folds, 1 мес D1 / 2 нед M15 |
| R4 | Hyperparam search | Optuna TPE, 50 trials / 5 мин budget |
| R5 | Feature set | 15 фич (returns/momentum/volatility/volume/trend + sentiment) |
| R6 | Artifact format | joblib + symlink production.joblib + metadata.json |
| R7 | Shadow mechanism | Celery post-response, resolve task каждые 15 мин |
| R8 | `mape_at_generation` | daily recompute по last 30 resolved inferences |
| R9 | Retrain schedule | Celery Beat `0 2 * * 0`, concurrency=1 |
| R10 | Explanation | Шаблон + YAML forbidden-phrases filter, fail-closed |
| R11 | Classifier imbalance | RF `balanced` + стратификация + опц. SMOTE |
| R12 | gRPC concurrency | ThreadPoolExecutor max_workers=8 |

Все пункты закрывают соответствующие `NEEDS CLARIFICATION` из
Technical Context. Переходим к Phase 1.
