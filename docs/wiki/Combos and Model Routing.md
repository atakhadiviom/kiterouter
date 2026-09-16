# Combos and Model Routing

KiteRouter Combos allow grouping multiple AI models into a single virtual endpoint with configurable fallback and routing policies.

Combos are exposed through standard OpenAI-compatible endpoints (`/v1/chat/completions`, `/v1/models`) and can be consumed by external tools (Cursor, Cline, Continue, OpenCode) just like any standalone model.

## Supported Routing Strategies

1. **Priority Fallback (`fallback`)**:
   - Evaluates models sequentially in the order defined (`Model 1 → Model 2 → Model 3`).
   - If a provider is unavailable, encounters an HTTP error (401, 403, 429, 500, 502, 503), or exhausts quota, KiteRouter immediately transfers the prompt to the next model in the chain without returning an error to the user.
2. **Round-Robin Load Balancing (`round-robin`)**:
   - Cycles through constituent models sequentially across requests.
   - If a selected model encounters an error, the request automatically falls back to remaining models in the chain.
3. **Random Pool (`random`)**:
   - Randomizes the candidate order for each incoming prompt, providing equal statistical distribution with automatic failover.

## Naming & Invocations

Combos can be invoked using either the `combo/` namespace or direct identifier:

- `combo/Own`
- `Own`
- `combo/coding-power`
- `coding-power`

Example OpenAI completion request:

```bash
curl http://127.0.0.1:3001/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "combo/Own",
    "messages": [{"role": "user", "content": "Hello!"}],
    "stream": true
  }'
```

## Management via Dashboard

The KiteRouter Dashboard provides a dedicated **Combos** tab:
- **Visual Chains**: See the ordered pipeline of models with provider badges.
- **Interactive Reordering**: Easily reorder models in sequence using drag-and-drop or priority arrows.
- **Live Testing**: Test combo execution with a single click to verify failover and measure latency.
- **Import from 9Router**: One-click extraction and translation of existing combos from `~/.9router/db/data.sqlite`.

## Management via REST API

- `GET /api/combos` — List all defined combos
- `POST /api/combos` — Create a new combo
- `PUT /api/combos/{name}` — Update combo configuration
- `DELETE /api/combos/{name}` — Delete combo
- `POST /api/combos/import-9router` — Import combos from 9Router SQLite
- `POST /api/test-combo` — Run live validation test against a combo
