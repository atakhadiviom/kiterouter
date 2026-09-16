# KiteRouter 🪁

**Zero-build, on-the-fly AI gateway for coding CLIs.**

Connect Claude Code, Cursor, Antigravity, Cline, OpenCode, Codex, and Hermes to multi-provider AI models with automatic fallback and RTK token compression.

## Features
- **Zero Build**: Pure Python, zero webpack/npm build steps. Update directly to the latest git commit on the fly.
- **RTK Token Saver**: Intercepts `git diff`, file tree dumps, and terminal bloat, saving 20–40% tokens per turn.
- **Initial Providers**:
  - **OpenCode Free**: Public keyless provider (Claude 3.5 Sonnet, GPT-4o, DeepSeek)
  - **OpenCode Go**: Cloud subscription routing
  - **Cursor**: Local session token extraction and bridge
  - **Antigravity**: Google DeepMind coding agent integration
  - **Cline**: Anthropic `/v1/messages` format adapter
- **Dual Protocol**: Native OpenAI `/v1/chat/completions` and Anthropic `/v1/messages`.
