<!--
## Sync Impact Report
- **Version change**: 0.0.0 → 1.0.0
- **Bump rationale**: MAJOR — initial constitution creation (first ratification)
- **Modified principles**: N/A (initial creation)
- **Added sections**:
  - Core Principles (7 principles)
  - Technical Constraints
  - Development Workflow
  - Governance
- **Removed sections**: N/A
- **Templates requiring updates**:
  - `.specify/templates/plan-template.md` — ✅ no updates needed (Constitution Check section is generic)
  - `.specify/templates/spec-template.md` — ✅ no updates needed (template is generic)
  - `.specify/templates/tasks-template.md` — ✅ no updates needed (template is generic)
- **Follow-up TODOs**: none
-->

# Finzooka Constitution

## Core Principles

### I. Explainability First (NON-NEGOTIABLE)

Every recommendation, signal, and prediction MUST include a human-readable
explanation of the factors that led to the output. A signal without a
"why" is equivalent to no signal.

- Each signal MUST contain a textual explanation (2-3 sentences)
  describing which factors contributed: sentiment, technical analysis,
  news events
- The `factors` field MUST enumerate concrete data points (sentiment
  score, RSI value, specific news headline)
- No "black box" outputs — if the model cannot explain a recommendation,
  it MUST NOT be surfaced to the user
- Rationale: custdev insight from Dmitry — "explainability is more
  important than accuracy." Industry standard confirmed by Danelfin
  and Kavout track records

### II. Navigator, Not Autopilot

The system is an advisory tool. The trader MUST always retain full
control over trading decisions.

- All outputs MUST be framed as "recommendations" or "suggestions,"
  never as "instructions" or "strategies"
- The system MUST NOT execute trades autonomously without explicit
  per-action user confirmation (even in v2.0 auto-stop feature)
- Every signal MUST include stop-loss and take-profit levels to
  support informed decision-making
- Deep links to the broker terminal MUST open the ticker view only —
  no pre-filled order forms unless explicitly confirmed by the user
- Rationale: this positioning addresses the primary trust barrier
  identified in custdev and differentiates from competitor bots

### III. Multi-Factor Analysis

Recommendations MUST be based on at least two independent data sources.
Single-factor signals are prohibited.

- Every signal generation MUST incorporate: (a) historical OHLCV +
  technical indicators, and (b) at least one exogenous factor
  (sentiment or news)
- Sentiment scoring MUST aggregate data from multiple sources
  (T-Pulse, news channels) — a single source is insufficient
- When a data source is unavailable, the system MUST degrade
  gracefully: fall back to available sources and clearly indicate
  reduced confidence in the explanation
- The `factors` JSON in each signal MUST list all contributing
  data sources with their individual scores
- Rationale: multi-factor approach is the industry standard
  (Danelfin uses 900+ indicators); single-factor bots consistently
  fail per market research findings

### IV. Telegram-Native UX

The user interface MUST live entirely within the Telegram ecosystem.
No separate apps, no browser-only experiences.

- All user interactions MUST be accessible through Telegram Bot
  commands and/or the Telegram Mini App
- Two complementary formats MUST be maintained: "numbers in chat"
  (quick text-based analytics) and "visuals in Mini App"
  (interactive charts with overlay)
- Notifications MUST be configurable by the user — no unsolicited
  spam. Users select triggers (sentiment spike, level approach,
  important news) and frequency
- Onboarding MUST complete in under 2 minutes via the chat interface
- Rationale: target audience already lives in Telegram; installing
  a separate app is a conversion killer per custdev feedback

### V. Model Integrity & Observability

The ML model MUST be continuously monitored, and degradation MUST
be detected and acted upon.

- MAPE and win rate MUST be tracked daily per ticker and exposed
  in the admin panel
- Inference latency MUST be monitored; alerts MUST fire when
  average inference exceeds the defined threshold (30s)
- Pipeline health (data collector, sentiment, ML engine) MUST
  be checked at least every minute, with admin alerts on failure
- Every signal MUST record `mape_at_generation` — the model's
  current accuracy at the time the signal was created
- Model retraining MUST be available on-demand and MUST include
  before/after metric comparison
- Rationale: "model degradation without monitoring" is the #1
  failure pattern identified in market research of failed trading
  bots

### VI. Regulatory Compliance

The system MUST operate within Russian legal requirements for
financial information services.

- Every user-facing signal MUST be accompanied by the disclaimer:
  "Не является инвестиционной рекомендацией" (Not investment advice)
- User data handling MUST comply with 152-FZ (Russian personal
  data law): store only Telegram ID, provide deletion on request
  with cascading removal of all associated data
- T-Invest API tokens MUST be stored securely: environment
  variables (MVP), encrypted storage (production)
- The system MUST NOT use language that implies guaranteed returns
  or promises specific financial outcomes
- Rationale: operating without a Central Bank license requires
  careful framing to avoid regulatory action; 152-FZ compliance
  is legally mandatory

### VII. Simplicity & MVP Discipline

The system is built by a solo developer. Every architectural and
feature decision MUST favor simplicity over completeness.

- YAGNI: do not implement features beyond the current release scope
  (MVP → v1.1 → v2.0 as defined in the User Story Map)
- Start with vertical scaling on the home server (4 CPU, 12GB RAM,
  GTX 1650); horizontal scaling is deferred until >100 users
- Prefer existing infrastructure: PostgreSQL + Redis (no additional
  databases), Celery with Redis broker (already in stack)
- Docker Compose for all services — no Kubernetes or cloud
  orchestration until proven necessary
- Blue chips only for MVP; second-tier tickers are deferred to v2.0
- Rationale: solo developer with a pet-project budget; over-
  engineering is the primary risk to project completion

## Technical Constraints

- **Language**: Python (backend, ML, bots), JS/HTML (Mini App frontend)
- **Primary stack**: FastAPI, aiogram/python-telegram-bot, scikit-learn,
  statsmodels, PostgreSQL, Redis, Celery
- **LLM**: Mistral API (free tier) for NLP tasks; local fallback for
  basic sentiment when rate-limited
- **Broker integration**: T-Invest API (gRPC/REST) — single read-only
  token for MVP, per-user tokens deferred to v2.0
- **Deployment**: Docker Compose on home server; maintenance window
  outside MOEX trading session (before 10:00 or after 18:50 MSK)
- **MVP SLA**: 95% availability; critical hours are MOEX trading
  session (10:00-18:50 MSK, Mon-Fri)
- **Performance targets**: signal generation <15s (target), <60s
  (acceptable MVP); Mini App load <3s; push notifications <60s
- **Data retention**: OHLCV unlimited; raw sentiment 30 days;
  signal journal 1 year; backups daily (RPO <24h)

## Development Workflow

- **Solo developer** model — no formal code review process required,
  but all changes MUST be committed with descriptive messages
- **Release cadence**: feature-driven, irregular; deploy during
  maintenance windows only
- **Testing discipline**: backtest model on 6+ months of historical
  data before any public release; walk-forward validation mandatory
  to prevent overfitting
- **Rollback**: manual rollback acceptable for MVP; previous Docker
  images MUST be retained for quick recovery (RTO <2h MVP, <30min
  target)
- **Branching**: feature branches merged to main; no direct pushes
  to main for non-trivial changes

## Governance

This constitution is the authoritative source of project principles
and constraints. All design decisions, feature specifications, and
implementation plans MUST be consistent with these principles.

- **Amendments**: any principle change MUST be documented with
  rationale, reflected in this file, and propagated to dependent
  templates (plan, spec, tasks)
- **Versioning**: MAJOR.MINOR.PATCH — MAJOR for principle removals
  or incompatible redefinitions, MINOR for new principles or
  material expansions, PATCH for clarifications and wording fixes
- **Compliance review**: before each release, verify that all user-
  facing outputs include required disclaimers (Principle VI) and
  explanations (Principle I)
- **Conflict resolution**: when principles conflict, priority order
  is: VI (Regulatory) > I (Explainability) > II (Navigator) >
  V (Observability) > III (Multi-Factor) > IV (UX) > VII (Simplicity)

**Version**: 1.0.0 | **Ratified**: 2026-03-14 | **Last Amended**: 2026-03-14
