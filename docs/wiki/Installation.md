# Installation

## Requirements

- Python 3.12+ with [uv](https://github.com/astral-sh/uv) (recommended) — dependencies are handled by `pyproject.toml` / `uv.lock`.
- macOS (tested) — Linux/Windows should work for most providers.
- Optional local apps whose credentials can be auto-discovered: Cursor IDE, GitHub CLI (`gh`), Claude Code, OmniRoute, 9Router, gcloud.

## Clone and run

```bash
git clone https://github.com/atakhadiviom/kiterouter.git
cd kiterouter
uv run uvicorn kiterouter.server:app --host 127.0.0.1 --port 3001
```

Verify:

```bash
curl http://127.0.0.1:3001/health
# then open http://127.0.0.1:3001/dashboard
```

## Tests

```bash
uv run pytest tests/ -q
```

## Run as a service

KiteRouter is a plain FastAPI/uvicorn app; any process manager works, e.g.:

```bash
nohup uv run uvicorn kiterouter.server:app --host 127.0.0.1 --port 3001 &
```

Keep `--host 127.0.0.1` — the gateway is meant for local coding CLIs, not the LAN.

## Updating

```bash
git pull          # fast: the dashboard is a single static file, no build step
```
