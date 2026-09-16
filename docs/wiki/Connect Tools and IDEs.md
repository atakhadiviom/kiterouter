# Connect Tools and IDEs

KiteRouter exposes standard OpenAI and Anthropic compatible API endpoints, allowing you to drop it directly into any AI code editor, agent CLI, or developer tool.

All clients connect to KiteRouter locally at `http://127.0.0.1:3001/v1`.

---

## Universal Configuration

| Setting | Value |
|---|---|
| **Base URL** | `http://127.0.0.1:3001/v1` |
| **API Key** | `kiterouter` (or any string) |
| **Smart Zero-Config Model** | `auto` or `default` |

---

## Tool-by-Tool Setup Guides

### 1. Cursor IDE
1. Open Cursor Settings (`Cmd + ,` or `Ctrl + ,`).
2. Navigate to **Models**.
3. Under **OpenAI API Key**, enter `kiterouter`.
4. Click **Override OpenAI Base URL** and enter:
   ```
   http://127.0.0.1:3001/v1
   ```
5. In the models list, add your desired models:
   - `auto` (Zero-config smart fallback across all active providers)
   - `combo/Own` (Or your custom combos)
   - `opencode_go/deepseek-flash`
   - `cursor/composer-2.5`

### 2. Claude Code CLI
Run Claude Code with KiteRouter's endpoint using standard environment variables:

```bash
export ANTHROPIC_BASE_URL="http://127.0.0.1:3001/v1"
export ANTHROPIC_API_KEY="kiterouter"
claude --model auto
```

### 3. Cline & Roo Code (VS Code)
1. Open the Cline or Roo Code extension settings.
2. Under **API Provider**, select **OpenAI Compatible**.
3. Set **Base URL** to `http://127.0.0.1:3001/v1`.
4. Set **API Key** to `kiterouter`.
5. Set **Model ID** to `auto` or `combo/Own`.

### 4. OpenCode CLI
Add KiteRouter as a custom provider in `~/.config/opencode/opencode.json`:

```json
{
  "model": "kiterouter/auto",
  "providers": {
    "kiterouter": {
      "baseURL": "http://127.0.0.1:3001/v1",
      "apiKey": "kiterouter"
    }
  }
}
```

### 5. Continue.dev
In `~/.continue/config.json`:

```json
{
  "models": [
    {
      "title": "KiteRouter Auto",
      "provider": "openai",
      "model": "auto",
      "apiBase": "http://127.0.0.1:3001/v1",
      "apiKey": "kiterouter"
    }
  ]
}
```

### 6. Aider CLI
Launch Aider pointing to local KiteRouter:

```bash
export OPENAI_API_BASE="http://127.0.0.1:3001/v1"
export OPENAI_API_KEY="kiterouter"
aider --model openai/auto
```

### 7. Python (OpenAI SDK)

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://127.0.0.1:3001/v1",
    api_key="kiterouter"
)

response = client.chat.completions.create(
    model="auto",
    messages=[{"role": "user", "content": "Explain async I/O in Python."}]
)

print(response.choices[0].message.content)
```

---

## Related Documentation
- [[Home]]
- [[Dashboard]]
- [[Combos and Model Routing]]
- [[API Reference]]
