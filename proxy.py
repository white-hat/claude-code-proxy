# /// script
# dependencies = ["fastapi", "httpx", "uvicorn[standard]", "pyyaml"]
# ///
"""Anthropic-compatible proxy with YAML-configured providers."""

import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import yaml
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse

CONFIG_PATH = Path(__file__).parent / "config.yaml"
_HOP_BY_HOP = {
    "host",
    "content-length",
    "transfer-encoding",
    "connection",
    "keep-alive",
}

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S"
)
log = logging.getLogger("proxy")

_http: httpx.AsyncClient
_cfg: dict
_model_map: dict  # model_name -> provider_name


def _load_config() -> dict:
    if not CONFIG_PATH.exists():
        raise FileNotFoundError(f"Config not found: {CONFIG_PATH} (copy config.yaml.sample to config.yaml)")
    with open(CONFIG_PATH) as f:
        cfg = yaml.safe_load(f)
    providers = cfg.get("providers") or {}
    if not providers:
        raise ValueError("config.yaml has no providers defined")
    model_map = {}
    for model_name, entry in cfg.get("models", {}).items():
        if isinstance(entry, str):
            provider = entry
        elif isinstance(entry, dict):
            provider = entry["provider"]
        else:
            continue
        if provider not in providers:
            raise ValueError(f"Model '{model_name}' maps to unknown provider '{provider}'")
        model_map[model_name] = provider
    cfg["_model_map"] = model_map
    cfg["_default_provider"] = next(iter(providers))
    return cfg


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _http, _cfg
    _cfg = _load_config()
    log.info(
        "loaded %d providers, %d models",
        len(_cfg.get("providers", {})),
        len(_cfg.get("_model_map", {})),
    )
    async with httpx.AsyncClient(timeout=300) as c:
        _http = c
        yield


app = FastAPI(lifespan=lifespan)


def _headers_for_provider(request: Request, provider_cfg: dict) -> dict:
    h = {k: v for k, v in request.headers.items() if k.lower() not in _HOP_BY_HOP}

    if "api_key" in provider_cfg:
        h["authorization"] = f"Bearer {provider_cfg['api_key']}"

    if provider_cfg.get("strip_oauth_beta"):
        beta = h.get("anthropic-beta", "")
        parts = [b.strip() for b in beta.split(",") if b.strip() != "oauth-2025-04-20"]
        if parts:
            h["anthropic-beta"] = ", ".join(parts)
        else:
            h.pop("anthropic-beta", None)

    return h


def _format_usage(usage: dict) -> str:
    parts = []
    if "input_tokens" in usage:
        parts.append(f"in={usage['input_tokens']}")
    if "output_tokens" in usage:
        parts.append(f"out={usage['output_tokens']}")
    if usage.get("cache_creation_input_tokens"):
        parts.append(f"cache_create={usage['cache_creation_input_tokens']}")
    if usage.get("cache_read_input_tokens"):
        parts.append(f"cache_hit={usage['cache_read_input_tokens']}")
    return " | ".join(parts) if parts else ""


async def _stream(resp: httpx.Response, model: str, provider_name: str):
    log.info(">>> %s | %s | streaming...", provider_name, model)
    usage = {}
    try:
        async for chunk in resp.aiter_raw():
            try:
                for line in chunk.decode().split("\n"):
                    if line.startswith("data: "):
                        evt = json.loads(line[6:])
                        if evt.get("type") == "message_delta" and "usage" in evt:
                            usage = evt["usage"]
            except (ValueError, UnicodeDecodeError):
                pass
            yield chunk
    except (httpx.ReadError, httpx.RemoteProtocolError):
        pass
    finally:
        await resp.aclose()
        log.info(
            "<<< %s | %s | %s",
            provider_name,
            model,
            _format_usage(usage) or "no usage data",
        )


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def proxy(request: Request, path: str):
    body = await request.body()

    model = "unknown"
    provider_name = _cfg["_default_provider"]

    if request.method == "POST" and body:
        try:
            data = json.loads(body)
            model = data.get("model", "unknown")
            if model in _cfg["_model_map"]:
                provider_name = _cfg["_model_map"][model]
        except (ValueError, AttributeError):
            pass

    provider_cfg = _cfg["providers"][provider_name]
    headers = _headers_for_provider(request, provider_cfg)
    target = f"{provider_cfg['base_url']}/{path}"

    log.info(">>> %s | %s | %s", provider_name, model, request.url.path)

    req = _http.build_request(request.method, target, headers=headers, content=body)
    resp = await _http.send(req, stream=True)
    resp_headers = {
        k: v for k, v in resp.headers.items() if k.lower() not in _HOP_BY_HOP
    }
    return StreamingResponse(
        _stream(resp, model, provider_name),
        status_code=resp.status_code,
        headers=resp_headers,
    )


if __name__ == "__main__":
    import uvicorn

    cfg = _load_config()
    port = cfg.get("port", 4000)
    uvicorn.run(app, host="0.0.0.0", port=port)
