# claude-code-proxy

Minimal Anthropic-compatible proxy for routing Claude Code to multiple providers.

## Why

Claude Code only talks to one API endpoint. If you want to use third-party
Anthropic-compatible models (e.g. Xiaomi Mimo) alongside native Claude models,
you'd normally have to switch `ANTHROPIC_BASE_URL` and restart — losing access
to one side or the other.

This proxy sits in front and routes by model name: Anthropic models go straight
to `api.anthropic.com` (OAuth passthrough), third-party models get rerouted with
their own API key and headers fixed up. Claude Code sees a single endpoint; the
proxy handles the rest.

## Quick Start

### With uv

```bash
uv run proxy.py
```

### With Docker

```bash
docker compose up -d
```

### Point Claude Code

```bash
export ANTHROPIC_BASE_URL=http://localhost:4000
```

## Configuration

Edit `config.yaml`:

```yaml
port: 4000

providers:
  anthropic:
    base_url: https://api.anthropic.com
    # OAuth token forwarded from Claude Code as-is

  xiaomi:
    base_url: https://token-plan-sgp.xiaomimimo.com/anthropic
    api_key: YOUR_API_KEY_HERE
    strip_oauth_beta: true

aliases:
  mimo-pro: mimo-v2.5-pro

models:
  claude-opus-4-7:           anthropic
  claude-sonnet-4-6:         anthropic
  claude-haiku-4-5-20251001: anthropic
  mimo-v2.5-pro:             xiaomi
  mimo-v2.5:                 xiaomi
```

### Provider options

| Key                | Required | Description                                              |
|--------------------|----------|----------------------------------------------------------|
| `base_url`         | yes      | Upstream API base URL (Anthropic-messages format)        |
| `api_key`          | no       | Bearer token; if omitted, client auth is passed through  |
| `strip_oauth_beta` | no       | Remove `oauth-2025-04-20` from `anthropic-beta` header   |

### Models

Map model names to provider keys. Unknown models fall through to the first provider.

### Aliases

Short names that resolve to real model IDs before routing. Useful in Claude Code's `/model` picker so you don't have to type full model names.

```yaml
aliases:
  mimo-pro: mimo-v2.5-pro
  opus: claude-opus-4-7
```

Now you can use `/model mimo-pro` in Claude Code — the proxy rewrites it to `mimo-v2.5-pro` before sending upstream.

## Switching models in Claude Code

Claude Code lets you pick the model with `/model`. The name you type must match exactly what's in `config.yaml` under `models:`.

```
/model mimo-v2.5-pro
```

To use a third-party model as the **default** (so you don't have to switch every session), put it first in the `models:` block — the proxy falls through to the first listed provider for any model it doesn't recognise.

### 1M context window

Claude Code hardcodes context window sizes by model name — only Claude models get 1M. Third-party models default to 200k regardless of their actual capacity.

Use the `[1m]` suffix to tell Claude Code to use a 1M window. The proxy strips it before forwarding upstream:

```
/model mimo[1m]
/model mimo-pro[1m]
```

Claude Code reads `[1m]` in the model name and sets the context window to 1M tokens. The proxy removes the suffix so the upstream API sees the real model name. Models without the suffix keep their default window (e.g. Haiku stays at 200k).

Alternatively, override the model at startup:

```bash
ANTHROPIC_MODEL=mimo-v2.5-pro claude
```

## Logging

Each request logs a summary line on completion:

```
13:05:14 >>> anthropic | claude-sonnet-4-6 | streaming...
13:05:14 <<< anthropic | claude-sonnet-4-6 | in=3 | out=5 | cache_create=15 | cache_hit=111006 | stop=end_turn | tier=standard
```

Fields: `in`, `out` (tokens), `cache_create`, `cache_hit`, `stop` (stop reason: `end_turn`, `tool_use`, `max_tokens`), `tier` (service tier), `geo` (inference region, if available).

### Request/response dump

Pass `--dump` to log raw requests and SSE responses to a file (default: `dump.log`):

```bash
uv run proxy.py --dump
uv run proxy.py --dump my.log
```

## Limitations

- Anthropic-messages format only (not OpenAI-compatible endpoints)
- Streaming only (non-streaming responses not yet handled)
- No request/response body transformation
