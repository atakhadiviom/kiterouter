# Playground and Live Testing

## Playground

- **Model field**: input + datalist populated from `GET /api/v1/models`, grouped by provider with a provider filter; free text still allowed so you can paste any model id.
- **Prompt** → `Send Test Request` → real completion through the gateway; streamed or non-streamed.
- Upstream errors render as errors (red) — never styled as success.

## Testing endpoints

| Button | Endpoint | What it does |
|---|---|---|
| Test provider | `POST /api/test-provider` | One completion against the provider's default model |
| Test model | `POST /api/test-model` | Tests a specific `provider/model` |
| Test all models | `POST /api/test-all-models` | Iterates every model of every enabled provider; results persisted per model; shown as pass/fail with upstream error text |

**Important:** a successful *model list* fetch means the catalog is reachable, not that the model works. Only the test endpoints (real completions) mark a model as passing.

## Anti-ban behavior

- 429/403 from an upstream stops the test instead of retry-spamming (protects accounts).
- Machine/session identity stays stable per token — never rotated per request.
