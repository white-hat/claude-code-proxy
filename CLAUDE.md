# claude-code-proxy

Minimal Anthropic-compatible proxy for routing Claude Code requests to multiple providers.

## What it does

Receives Anthropic-format requests from Claude Code, routes them by model name:

- **Anthropic models** (`claude-*`): forwarded as-is to api.anthropic.com (OAuth token passthrough)
- **Other providers** (e.g. Xiaomi Mimo): re-routed with provider's own API key, OAuth beta header stripped

## Architecture

- `proxy.py` — FastAPI + httpx single-file proxy (~120 lines)
- `config.yaml` — provider and model routing config
- `Dockerfile` + `docker-compose.yml` — containerized deployment
- `Makefile` — shortcuts (`make up`, `make down`, `make lint`, etc.)

## Running

```bash
cd ~/code/github.com/white-hat/claude-code-proxy
uv run proxy.py        # local
make up                # Docker
make down              # stop Docker
```

Claude Code points to `http://localhost:4000`.

## Adding a provider

In `config.yaml`, add a provider entry and map models to it:

```yaml
providers:
  my-provider:
    base_url: https://api.example.com/anthropic
    api_key: sk-...
    strip_oauth_beta: true  # strip oauth-2025-04-20 from anthropic-beta

models:
  my-model: my-provider
  # or with context_window override (for providers that don't implement /v1/models):
  my-model:
    provider: my-provider
    context_window: 1000000
```

`strip_oauth_beta: true` is needed when the upstream rejects Claude Code's OAuth token.

## Key decisions

- Anthropic-compatible endpoints only (not OpenAI `/v1/chat/completions`)
- Streaming only — Claude Code streams all responses
- Usage stats (tokens, cache, stop reason, tier, geo) parsed from `message_start`/`message_delta` SSE events and logged at stream end
- `accept-encoding: identity` forced upstream to prevent compression and allow SSE parsing

