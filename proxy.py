# /// script
# dependencies = ["fastapi", "httpx", "uvicorn[standard]", "pyyaml"]
# ///
"""Anthropic-compatible proxy with YAML-configured providers."""

import json
import logging
import re
from contextlib import asynccontextmanager
from datetime import datetime
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
    "accept-encoding",
}

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S"
)
log = logging.getLogger("proxy")

_dump_file: Path | None = None


def _dump(request: Request, body: bytes, status: int) -> None:
    assert _dump_file is not None
    with _dump_file.open("a") as f:
        f.write(f"\n{'='*60}\n")
        f.write(f"[{datetime.now().isoformat(timespec='seconds')}] {request.method} {request.url} → {status}\n")
        for k, v in request.headers.items():
            f.write(f"  {k}: {v}\n")
        if body:
            f.write("\n")
            try:
                f.write(json.dumps(json.loads(body), indent=2))
            except (ValueError, UnicodeDecodeError):
                f.write(body.decode(errors="replace"))
            f.write("\n")

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
    cfg["_aliases"] = cfg.get("aliases") or {}
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

    h["accept-encoding"] = "identity"  # prevent upstream compression; proxy parses SSE as plain text

    if provider_cfg.get("strip_oauth_beta"):
        beta = h.get("anthropic-beta", "")
        parts = [b.strip() for b in beta.split(",") if b.strip() != "oauth-2025-04-20"]
        if parts:
            h["anthropic-beta"] = ", ".join(parts)
        else:
            h.pop("anthropic-beta", None)

    return h


def _format_usage(usage: dict, meta: dict) -> str:
    parts = []
    if "input_tokens" in usage:
        parts.append(f"in={usage['input_tokens']}")
    if "output_tokens" in usage:
        parts.append(f"out={usage['output_tokens']}")
    if usage.get("cache_creation_input_tokens"):
        parts.append(f"cache_create={usage['cache_creation_input_tokens']}")
    if usage.get("cache_read_input_tokens"):
        parts.append(f"cache_hit={usage['cache_read_input_tokens']}")
    if meta.get("stop_reason"):
        parts.append(f"stop={meta['stop_reason']}")
    if meta.get("service_tier"):
        parts.append(f"tier={meta['service_tier']}")
    if meta.get("inference_geo") and meta["inference_geo"] != "not_available":
        parts.append(f"geo={meta['inference_geo']}")
    return " | ".join(parts) if parts else ""


async def _stream(resp: httpx.Response, model: str, provider_name: str):
    log.info(">>> %s | %s | streaming...", provider_name, model)
    usage: dict = {}
    meta: dict = {}
    chunks: list[bytes] = [] if _dump_file else None  # type: ignore[assignment]
    try:
        async for chunk in resp.aiter_raw():
            if chunks is not None:
                chunks.append(chunk)
            try:
                for line in chunk.decode().split("\n"):
                    if line.startswith("data: "):
                        evt = json.loads(line[6:])
                        t = evt.get("type")
                        if t == "message_start" and "message" in evt:
                            msg = evt["message"]
                            usage.update(msg.get("usage", {}))
                            for k in ("service_tier", "inference_geo"):
                                if k in msg:
                                    meta[k] = msg[k]
                        elif t == "message_delta":
                            if "usage" in evt:
                                usage.update(evt["usage"])
                            if "delta" in evt and "stop_reason" in evt["delta"]:
                                meta["stop_reason"] = evt["delta"]["stop_reason"]
            except (ValueError, UnicodeDecodeError):
                pass
            yield chunk
    except (httpx.ReadError, httpx.RemoteProtocolError):
        pass
    finally:
        await resp.aclose()
        if _dump_file and chunks:
            with _dump_file.open("a") as f:
                f.write(f"  --- response {resp.status_code} ---\n")
                f.write(b"".join(chunks).decode(errors="replace"))
                f.write("\n")
        log.info(
            "<<< %s | %s | %s",
            provider_name,
            model,
            _format_usage(usage, meta) or "no usage data",
        )


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD"])
async def proxy(request: Request, path: str):
    body = await request.body()

    model = "unknown"
    provider_name = _cfg["_default_provider"]

    if request.method == "POST" and body:
        try:
            data = json.loads(body)
            model = data.get("model", "unknown")
            model = re.sub(r"\[1m\]$", "", model, flags=re.IGNORECASE)
            data["model"] = model
            resolved = _cfg["_aliases"].get(model, model)
            if resolved != model:
                data["model"] = resolved
                body = json.dumps(data).encode()
                model = resolved
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
    if _dump_file:
        _dump(request, body, resp.status_code)
    resp_headers = {
        k: v for k, v in resp.headers.items() if k.lower() not in _HOP_BY_HOP
    }
    return StreamingResponse(
        _stream(resp, model, provider_name),
        status_code=resp.status_code,
        headers=resp_headers,
    )


if __name__ == "__main__":
    import argparse
    import uvicorn

    parser = argparse.ArgumentParser()
    parser.add_argument("--dump", metavar="FILE", nargs="?", const="dump.log",
                        help="append all requests/responses to FILE (default: dump.log)")
    args = parser.parse_args()

    if args.dump:
        _dump_file = Path(args.dump)
        log.info("dumping requests to %s", _dump_file)

    cfg = _load_config()
    port = cfg.get("port", 4000)
    uvicorn.run(app, host="0.0.0.0", port=port)
