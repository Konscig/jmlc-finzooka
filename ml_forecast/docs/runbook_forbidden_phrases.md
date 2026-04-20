# Runbook — `config/forbidden_phrases.yaml`

**Owner**: ML Forecast Service maintainer.
**Principle**: VI Regulatory Compliance (fail-closed; see
[spec.md FR-019](../../specs/001-ml-forecast-service/spec.md)).

The YAML file at [config/forbidden_phrases.yaml](../config/forbidden_phrases.yaml)
is a substring blocklist. Any rendered `explanation` or factor name that
matches a pattern here causes `ExplanationForbiddenPhrase` to fire, the
Forecast RPC responds with `INTERNAL/forbidden_phrase`, and the
inference log captures the rejection for audit.

## Adding a phrase

1. **Pick the canonical form** — lowercase, no punctuation, no trailing
   spaces. Matching is substring + case-insensitive, so variants like
   `Гарантировано!` / `ГАРАНТИРОВАНО` are covered automatically.
2. Append one line to the `patterns:` list in the YAML. Keep the list
   alphabetically grouped by language block; don't reorder existing
   lines (they are version-controlled and anchored by tests).
3. Run the filter test suite:

   ```bash
   docker compose run --rm --no-deps -T ml pytest -k forbidden -q
   ```

   The test is parametrized over every pattern in the YAML, so new
   entries automatically produce a new test case.

## Removing a phrase

1. Check in the last 30 days of `ml.inference_log` that the phrase has
   **never** actually rejected a request. If it has — removal can only
   proceed with explicit legal sign-off, not just a compliance call.
2. Delete the line from the YAML and commit.
3. Re-run the filter suite to confirm no regression.

## Reloading at runtime (no restart)

The filter lru-caches the YAML on first use. To pick up edits without
bouncing the container:

```bash
docker compose kill -s HUP ml
```

The main loop listens for SIGHUP and calls
`ml_forecast.inference.explain.reload_patterns()`; the next RPC reloads
the file. **Signal handler for SIGHUP is a small addition pending on
the ops pass** — until it lands, restart the ml container instead:

```bash
docker compose restart ml
```

## Dangerous anti-patterns

- **Do not** add regex-lookalikes (`.*`, `\b`) — the matcher is plain
  substring; such entries would be dead lines.
- **Do not** add a pattern that is a sub-string of a legitimate ticker
  name or common indicator name (e.g. `rsi`) — it would block every
  explanation that lists that feature.
- **Do not** edit the file live without running the test suite. Skipping
  the suite risks an ImportError at next render and an outage for every
  forecast call.

## Escalation

If a pattern was added under time pressure and later proves to block a
legitimate explanation, the short-term fix is to remove the pattern
and restart; the follow-up is an incident doc describing why the
original addition was justified and what replacement (if any) keeps
the compliance spirit.
