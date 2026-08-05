"""
veya/oskill/vision.py — Vision Analysis Pipeline (Layer 2).

Composite skill built on oprim image/video ops + LLM vision APIs.
Handles: image prep → vision LLM call → structured result.

Supports: OpenAI (GPT-4V/gpt-4o), Anthropic (Claude 3), DashScope (qwen-vl).
"""

from __future__ import annotations

import base64
import json
import os
from typing import Any

try:
    import httpx
    _HAS_HTTPX = True
except ImportError:
    httpx = None  # type: ignore
    _HAS_HTTPX = False

from veya.oprim.types import ImageFrame, ImageFormat, VisionResult
from veya.oprim.video import (
    detect_image_format,
    image_to_data_uri,
    parse_data_uri,
    validate_image,
)

# ---------------------------------------------------------------------------
# Provider config
# ---------------------------------------------------------------------------

_VISION_PROVIDERS: dict[str, dict[str, Any]] = {
    "openai": {
        "endpoint": "https://api.openai.com/v1/chat/completions",
        "api_key_env": "OPENAI_API_KEY",
        "default_model": "gpt-4o",
        "max_images": 20,
    },
    "anthropic": {
        "endpoint": "https://api.anthropic.com/v1/messages",
        "api_key_env": "ANTHROPIC_API_KEY",
        "default_model": "claude-sonnet-4-20250514",
        "max_images": 20,
    },
    "dashscope": {
        "endpoint": "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
        "api_key_env": "DASHSCOPE_API_KEY",
        "default_model": "qwen-vl-max",
        "max_images": 10,
    },
}


def _get_vision_config(provider: str) -> dict | None:
    return _VISION_PROVIDERS.get(provider.lower())


def _get_api_key(provider: str) -> str:
    cfg = _VISION_PROVIDERS.get(provider.lower(), {})
    env_var = cfg.get("api_key_env", f"{provider.upper()}_API_KEY")
    return os.environ.get(env_var, "")


# ---------------------------------------------------------------------------
# Core vision analysis
# ---------------------------------------------------------------------------


async def analyze_image(
    image: bytes,
    provider: str = "openai",
    *,
    prompt: str = "Describe this image in detail.",
    model: str | None = None,
    system_prompt: str | None = None,
    max_tokens: int = 1024,
    temperature: float = 0.7,
    timeout: float = 60.0,
) -> VisionResult:
    """Analyze a single image using a vision-capable LLM."""
    if not _HAS_HTTPX:
        return VisionResult(description="", metadata={"error": "httpx not installed"})
    cfg = _get_vision_config(provider)
    if cfg is None:
        return VisionResult(
            description="",
            metadata={"error": f"Unknown vision provider: {provider}"},
        )

    api_key = _get_api_key(provider)
    if not api_key:
        return VisionResult(
            description="",
            metadata={"error": f"API key not configured for {provider}"},
        )

    # Validate image
    valid, err = validate_image(image)
    if not valid:
        return VisionResult(description="", metadata={"error": err})

    resolved_model = model or cfg.get("default_model", "gpt-4o")
    endpoint = cfg["endpoint"]
    fmt = detect_image_format(image) or ImageFormat.PNG
    data_uri = image_to_data_uri(image, fmt)

    import time
    start = time.time()

    if provider == "openai" or provider == "dashscope":
        result = await _vision_openai_compat(
            data_uri, prompt, api_key, endpoint, resolved_model,
            system_prompt, max_tokens, temperature, timeout,
        )
    elif provider == "anthropic":
        result = await _vision_anthropic(
            image, fmt, prompt, api_key, endpoint, resolved_model,
            system_prompt, max_tokens, temperature, timeout,
        )
    else:
        result = VisionResult(description="", metadata={"error": f"Provider {provider} not implemented"})

    result.processing_time_ms = (time.time() - start) * 1000
    result.model = resolved_model
    return result


async def analyze_images(
    images: list[bytes],
    provider: str = "openai",
    *,
    prompt: str = "Describe these images.",
    model: str | None = None,
    system_prompt: str | None = None,
    max_tokens: int = 2048,
    timeout: float = 120.0,
) -> VisionResult:
    """Analyze multiple images together.

    Args:
        images: List of raw image bytes.
        provider: Vision provider.
        prompt: What to ask about the images.
        model: Model name.
        system_prompt: System instruction.
        max_tokens: Max response tokens.
        timeout: HTTP timeout.

    Returns:
        VisionResult.
    """
    cfg = _get_vision_config(provider)
    if cfg is None:
        return VisionResult(description="", metadata={"error": f"Unknown provider: {provider}"})

    api_key = _get_api_key(provider)
    if not api_key:
        return VisionResult(description="", metadata={"error": "No API key"})

    resolved_model = model or cfg.get("default_model", "gpt-4o")
    endpoint = cfg["endpoint"]
    max_imgs = cfg.get("max_images", 10)

    # Trim to max images
    images = images[:max_imgs]

    # Build content blocks
    content: list[dict] = [{"type": "text", "text": prompt}]
    for img in images:
        fmt = detect_image_format(img) or ImageFormat.PNG
        uri = image_to_data_uri(img, fmt)
        content.append({
            "type": "image_url",
            "image_url": {"url": uri, "detail": "auto"},
        })

    messages: list[dict] = [{"role": "user", "content": content}]
    if system_prompt:
        messages.insert(0, {"role": "system", "content": system_prompt})

    import time
    start = time.time()

    if provider in ("openai", "dashscope"):
        result = await _vision_openai_compat_multi(
            messages, api_key, endpoint, resolved_model,
            max_tokens, timeout,
        )
    else:
        result = VisionResult(description="", metadata={"error": f"Multi-image not supported for {provider}"})

    result.processing_time_ms = (time.time() - start) * 1000
    result.model = resolved_model
    return result


# ---------------------------------------------------------------------------
# Provider-specific implementations
# ---------------------------------------------------------------------------


async def _vision_openai_compat(
    data_uri: str,
    prompt: str,
    api_key: str,
    endpoint: str,
    model: str,
    system_prompt: str | None,
    max_tokens: int,
    temperature: float,
    timeout: float,
) -> VisionResult:
    """OpenAI-compatible vision API call."""
    messages: list[dict] = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": data_uri, "detail": "auto"}},
            ],
        }
    ]
    if system_prompt:
        messages.insert(0, {"role": "system", "content": system_prompt})

    body = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }

    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            resp = await client.post(
                endpoint,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json=body,
            )
            resp.raise_for_status()
            data = resp.json()

            choice = (data.get("choices") or [{}])[0]
            text = choice.get("message", {}).get("content", "")
            usage = data.get("usage", {})

            return VisionResult(
                description=text,
                metadata={
                    "provider": "openai",
                    "model": model,
                    "usage": usage,
                },
            )
        except Exception as e:
            return VisionResult(description="", metadata={"error": str(e)})


async def _vision_openai_compat_multi(
    messages: list[dict],
    api_key: str,
    endpoint: str,
    model: str,
    max_tokens: int,
    timeout: float,
) -> VisionResult:
    """OpenAI-compatible multi-image call."""
    body = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
    }

    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            resp = await client.post(
                endpoint,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json=body,
            )
            resp.raise_for_status()
            data = resp.json()
            choice = (data.get("choices") or [{}])[0]
            text = choice.get("message", {}).get("content", "")
            return VisionResult(description=text, metadata={"usage": data.get("usage", {})})
        except Exception as e:
            return VisionResult(description="", metadata={"error": str(e)})


async def _vision_anthropic(
    image: bytes,
    fmt: ImageFormat,
    prompt: str,
    api_key: str,
    endpoint: str,
    model: str,
    system_prompt: str | None,
    max_tokens: int,
    temperature: float,
    timeout: float,
) -> VisionResult:
    """Anthropic Claude vision API call."""
    b64_data = base64.b64encode(image).decode("utf-8")
    mime_type = f"image/{fmt.value}" if fmt != ImageFormat.JPG else "image/jpeg"

    body: dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": mime_type, "data": b64_data}},
                    {"type": "text", "text": prompt},
                ],
            }
        ],
    }
    if system_prompt:
        body["system"] = system_prompt

    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            resp = await client.post(
                endpoint,
                headers={
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                    "Content-Type": "application/json",
                },
                json=body,
            )
            resp.raise_for_status()
            data = resp.json()

            text = ""
            for block in data.get("content", []):
                if block.get("type") == "text":
                    text = block.get("text", "")

            return VisionResult(
                description=text,
                metadata={
                    "provider": "anthropic",
                    "model": model,
                    "usage": data.get("usage", {}),
                },
            )
        except Exception as e:
            return VisionResult(description="", metadata={"error": str(e)})


# ---------------------------------------------------------------------------
# Convenience: analyze image file
# ---------------------------------------------------------------------------


async def analyze_image_file(
    file_path: str,
    provider: str = "openai",
    *,
    prompt: str = "Describe this image in detail.",
    model: str | None = None,
) -> VisionResult:
    """Analyze an image file on disk.

    Args:
        file_path: Path to image file.
        provider: Vision provider.
        prompt: What to ask.
        model: Model name.

    Returns:
        VisionResult.
    """
    with open(file_path, "rb") as f:
        image = f.read()
    return await analyze_image(image, provider=provider, prompt=prompt, model=model)
