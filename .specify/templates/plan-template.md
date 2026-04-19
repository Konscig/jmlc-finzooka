# Implementation Plan: [FEATURE]

**Branch**: `[###-feature-name]` | **Date**: [DATE] | **Spec**: [link]
**Input**: Feature specification from `/specs/[###-feature-name]/spec.md`

**Note**: This template is filled in by the `/speckit.plan` command. See `.specify/templates/plan-template.md` for the execution workflow.

## Summary

[Extract from feature spec: primary requirement + technical approach from research]

## Technical Context

<!--
  ACTION REQUIRED: Replace the content in this section with the technical details
  for the project. The structure here is presented in advisory capacity to guide
  the iteration process.
-->

**Language/Version**: [e.g., Python 3.11, Swift 5.9, Rust 1.75 or NEEDS CLARIFICATION]  
**Primary Dependencies**: [e.g., FastAPI, UIKit, LLVM or NEEDS CLARIFICATION]  
**Storage**: [if applicable, e.g., PostgreSQL, CoreData, files or N/A]  
**Testing**: [e.g., pytest, XCTest, cargo test or NEEDS CLARIFICATION]  
**Target Platform**: [e.g., Linux server, iOS 15+, WASM or NEEDS CLARIFICATION]
**Project Type**: [e.g., library/cli/web-service/mobile-app/compiler/desktop-app or NEEDS CLARIFICATION]  
**Performance Goals**: [domain-specific, e.g., 1000 req/s, 10k lines/sec, 60 fps or NEEDS CLARIFICATION]  
**Constraints**: [domain-specific, e.g., <200ms p95, <100MB memory, offline-capable or NEEDS CLARIFICATION]  
**Scale/Scope**: [domain-specific, e.g., 10k users, 1M LOC, 50 screens or NEEDS CLARIFICATION]

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

Mark each gate ✅ Pass / ⚠️ Deviation (log in Complexity Tracking) / N/A.
Gates derive from `.specify/memory/constitution.md` v1.0.0 — see that file for
full rationale. Conflict priority: VI > I > II > V > III > IV > VII.

### I. Explainability Gate (NON-NEGOTIABLE)

- [ ] Every new signal, recommendation, or prediction surfaces a human-readable
      `explanation` (2–3 sentences) AND a `factors` array listing concrete
      data points (sentiment score, RSI, news headline, etc.)
- [ ] No output path produces a "black box" value that reaches the user without
      an explanation — if the model cannot explain it, it is suppressed

### II. Navigator Gate

- [ ] All user-facing copy frames output as "recommendation" / "suggestion",
      never "instruction" / "strategy" / guaranteed return
- [ ] No code path executes a trade autonomously without explicit per-action
      user confirmation
- [ ] Every actionable signal includes stop-loss and take-profit levels
- [ ] Broker deep-links open the ticker view only (no pre-filled order forms
      unless user explicitly confirmed)

### III. Multi-Factor Gate

- [ ] Signal generation combines ≥2 independent sources: (a) OHLCV +
      technical indicators, plus (b) sentiment and/or news
- [ ] Sentiment aggregation uses >1 source (T-Pulse + news channels)
- [ ] Graceful degradation defined: when a source is unavailable, fallback is
      specified AND reduced confidence is surfaced in the explanation
- [ ] `factors` JSON enumerates every contributing source with its score

### IV. Telegram-Native UX Gate

- [ ] Feature is reachable entirely within Telegram (Bot commands and/or
      Mini App) — no separate app or browser-only flow
- [ ] Both formats supported where relevant: "numbers in chat" (quick text)
      AND "visuals in Mini App" (interactive charts with overlay)
- [ ] Notifications are user-configurable (trigger type + frequency); no
      unsolicited pushes
- [ ] Any onboarding path completes in ≤2 minutes via chat

### V. Model Integrity & Observability Gate

- [ ] New ML artifacts expose MAPE and win rate daily per ticker in the
      admin panel
- [ ] Inference latency is instrumented; alerts fire when avg >30s
- [ ] Every new signal records `mape_at_generation`
- [ ] Pipeline health-checks (data collector / sentiment / ML engine) run
      ≥ every minute with admin alerts on failure
- [ ] Retraining path exposes before/after metric comparison

### VI. Regulatory Compliance Gate (HIGHEST PRIORITY)

- [ ] Every user-facing signal carries the disclaimer
      «Не является инвестиционной рекомендацией»
- [ ] No copy implies guaranteed returns or specific financial outcomes
- [ ] Personal data scope stays within 152-ФЗ (Telegram ID only); deletion
      request cascades to all associated data
- [ ] T-Invest API tokens stored in env vars (MVP) or encrypted storage
      (production) — never in code, logs, or plaintext DB columns

### VII. Simplicity & MVP Discipline Gate

- [ ] Scope matches the current release tier (MVP / v1.1 / v2.0 per USM);
      no features pulled forward without explicit rescoping
- [ ] No new database/broker/queue introduced — reuses PostgreSQL, Redis,
      Celery already in stack
- [ ] Deployable via existing Docker Compose (no Kubernetes/cloud
      orchestration)
- [ ] Runs within home-server budget (4 CPU, 12GB RAM, GTX 1650); any
      horizontal-scaling assumption is justified by projected DAU >100
- [ ] Ticker universe stays on blue chips unless release tier permits
      second-tier

**Deviations**: any ⚠️ above MUST be logged in Complexity Tracking with
justification and a rejected simpler alternative.

## Project Structure

### Documentation (this feature)

```text
specs/[###-feature]/
├── plan.md              # This file (/speckit.plan command output)
├── research.md          # Phase 0 output (/speckit.plan command)
├── data-model.md        # Phase 1 output (/speckit.plan command)
├── quickstart.md        # Phase 1 output (/speckit.plan command)
├── contracts/           # Phase 1 output (/speckit.plan command)
└── tasks.md             # Phase 2 output (/speckit.tasks command - NOT created by /speckit.plan)
```

### Source Code (repository root)
<!--
  ACTION REQUIRED: Replace the placeholder tree below with the concrete layout
  for this feature. Delete unused options and expand the chosen structure with
  real paths (e.g., apps/admin, packages/something). The delivered plan must
  not include Option labels.
-->

```text
# [REMOVE IF UNUSED] Option 1: Single project (DEFAULT)
src/
├── models/
├── services/
├── cli/
└── lib/

tests/
├── contract/
├── integration/
└── unit/

# [REMOVE IF UNUSED] Option 2: Web application (when "frontend" + "backend" detected)
backend/
├── src/
│   ├── models/
│   ├── services/
│   └── api/
└── tests/

frontend/
├── src/
│   ├── components/
│   ├── pages/
│   └── services/
└── tests/

# [REMOVE IF UNUSED] Option 3: Mobile + API (when "iOS/Android" detected)
api/
└── [same as backend above]

ios/ or android/
└── [platform-specific structure: feature modules, UI flows, platform tests]
```

**Structure Decision**: [Document the selected structure and reference the real
directories captured above]

## Complexity Tracking

> **Fill ONLY if Constitution Check has violations that must be justified**

| Violation | Why Needed | Simpler Alternative Rejected Because |
|-----------|------------|-------------------------------------|
| [e.g., 4th project] | [current need] | [why 3 projects insufficient] |
| [e.g., Repository pattern] | [specific problem] | [why direct DB access insufficient] |
