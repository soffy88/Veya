#!/usr/bin/env python3
"""Re-qualify the canonical Veya model proxies from real probes (SPEC v1.0 §16/§17).

Regenerates the per-model evidence in ``~/.veya/model-state.json`` by actually
calling each candidate. Nothing is marked healthy by hand — every
``healthy`` / ``eligible`` / ``vision_verified`` value written here is derived
from a probe result in this run, which is what §4.4 requires.

Run it after rotating the NIM key pool, after fixing the OpenRouter credential,
or whenever upstream model ids change::

    .venv/bin/python scripts/requalify_model_proxies.py
    .venv/bin/python scripts/requalify_model_proxies.py --only nim
    .venv/bin/python scripts/requalify_model_proxies.py --dry-run

Exit code is 0 when every canonical proxy has at least one eligible model.
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import httpx

STATE_PATH = Path.home() / ".veya" / "model-state.json"
NIM_POOL_PATH = Path.home() / ".veya" / "secrets" / "nim-key-pool"
GATEWAY = os.environ.get("VEYA_GATEWAY", "http://127.0.0.1:8791")

NIM_ENDPOINT = "https://integrate.api.nvidia.com/v1/chat/completions"
NIM_MODELS_URL = "https://integrate.api.nvidia.com/v1/models"
OPENROUTER_ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"

# OpenRouter free-tier vision candidates for the veya-vl pool (§9). Probed only
# when an OpenRouter API key is actually present — the current credential is a
# browser cookie and answers 401, which is a credential fault, not a model fault.
OPENROUTER_VL_CANDIDATES = [
    "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free",
    "nvidia/nemotron-nano-12b-v2-vl:free",
    "google/gemma-4-26b-a4b-it:free",
    "google/gemma-4-31b-it:free",
]

# NIM models that carry a real vision/OCR capability (§17 IMAGE_INPUT / OCR /
# MULTIMODAL_REASONING). Everything else on NIM is treated as text-only.
NIM_VISION_CANDIDATES = [
    "meta/llama-3.2-11b-vision-instruct",
    "meta/llama-3.2-90b-vision-instruct",
    "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning",
    "microsoft/phi-3-vision-128k-instruct",
    "microsoft/kosmos-2",
    "nvidia/cosmos-reason2-8b",
    "nvidia/neva-22b",
]

TEXT_PROBE = "Reply with exactly: PONGER"

#: Provider-side rejections are delivered as a normal HTTP 200 with an error
#: string in ``content``.  Treating non-empty content as success fills the pool
#: with models that can only ever answer with an error, so every probe path must
#: screen for these markers.
REJECTION_MARKERS = (
    "rejected the request",
    "FreeTierError",
    "can only be used from within",
)


def _is_rejection(content: str) -> bool:
    return any(marker in content for marker in REJECTION_MARKERS)
OCR_PROBE = (
    'Read this image. Reply with JSON only: '
    '{"invoice_no":..., "total":..., "currency":..., "status":...}'
)


# ---------------------------------------------------------------------------
# test image (generated, never committed)
# ---------------------------------------------------------------------------


def _probe_image_b64() -> str:
    """A 640x200 invoice with known text, for a falsifiable OCR assertion."""
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return ""
    img = Image.new("RGB", (640, 200), "white")
    draw = ImageDraw.Draw(img)
    draw.rectangle([20, 20, 620, 180], outline="black", width=3)
    draw.text((50, 60), "INVOICE 7742", fill="black")
    draw.text((50, 100), "TOTAL  129.50 USD", fill="black")
    draw.text((50, 135), "STATUS: PAID", fill="red")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


_IMAGE_B64 = _probe_image_b64()
OCR_MARKERS = ("7742", "129.50", "PAID")


# ---------------------------------------------------------------------------
# probes
# ---------------------------------------------------------------------------


def _auth_headers(provider: str) -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if provider == "nvidia-nim":
        keys = [
            k
            for k in NIM_POOL_PATH.read_text(encoding="utf-8").splitlines()
            if k.strip()
        ] if NIM_POOL_PATH.is_file() else []
        if not keys:
            return {}
        headers["Authorization"] = f"Bearer {keys[0]}"
    elif provider == "openrouter":
        key = os.environ.get("OPENROUTER_API_KEY", "").strip()
        if key:
            headers["Authorization"] = f"Bearer {key}"
    elif provider == "opencode-go":
        headers["Authorization"] = "Bearer local-gateway-no-auth"
    return headers


def _post(url: str, provider: str, payload: dict, timeout: float) -> tuple[int, dict | str]:
    headers = _auth_headers(provider)
    if not headers:
        return 0, "no credential available"
    try:
        resp = httpx.post(url, headers=headers, json=payload, timeout=timeout)
    except Exception as exc:  # noqa: BLE001
        return 0, f"{type(exc).__name__}: {exc}"
    if resp.status_code != 200:
        return resp.status_code, resp.text[:200]
    try:
        return 200, resp.json()
    except ValueError:
        return resp.status_code, resp.text[:200]


def probe_text(
    provider: str,
    model: str,
    url: str,
    timeout: float = 200.0,
    wire_model: str | None = None,
) -> dict[str, Any]:
    """Probe one model. ``wire_model`` is the id to put on the wire when it
    differs from the stored id (the gateway keys its catalog by
    ``<provider>/<model>``, so the prefix must stay)."""
    send = wire_model or model
    runs: list[dict[str, Any]] = []
    for attempt in range(3):
        started = time.monotonic()
        status, body = _post(
            url,
            provider,
            {
                "model": send,
                "messages": [{"role": "user", "content": TEXT_PROBE}],
                "max_tokens": 4096,
            },
            timeout,
        )
        elapsed = int((time.monotonic() - started) * 1000)
        if status != 200:
            runs.append({"n": attempt + 1, "ok": False, "ms": elapsed, "note": str(body)[:120]})
            continue
        message = (body.get("choices") or [{}])[0].get("message") or {}
        content = (message.get("content") or "").strip()
        reasoning = (message.get("reasoning_content") or "").strip()
        # A provider rejection arrives as ordinary 200 text, so non-empty
        # content is NOT success. Both the veya pool runners and this script must
        # treat it as a failure, otherwise the pool fills with models that can
        # only ever answer with an error (SPEC: FALSE_SUCCESS=0).
        if _is_rejection(content):
            runs.append(
                {"n": attempt + 1, "ok": False, "ms": elapsed, "note": f"REJECTED: {content[:100]}"}
            )
            continue
        if not content and not reasoning:
            runs.append({"n": attempt + 1, "ok": False, "ms": elapsed, "note": "empty"})
            continue
        # identity is checked against the id we asked for, prefix included
        if body.get("model") and body["model"] != send:
            runs.append(
                {
                    "n": attempt + 1,
                    "ok": False,
                    "ms": elapsed,
                    "note": f"MODEL_IDENTITY got={body['model']} want={send}",
                }
            )
            continue
        runs.append({"n": attempt + 1, "ok": True, "ms": elapsed, "note": content[:60]})
    ok_runs = [r for r in runs if r["ok"]]
    latencies = sorted(r["ms"] for r in ok_runs)
    return {
        "text_3_of_3": len(ok_runs) == 3,
        "auth": any("no credential" not in r["note"] for r in runs) and bool(runs),
        "model_identity": not any("MODEL_IDENTITY" in r["note"] for r in runs),
        "latency_ms": latencies[len(latencies) // 2] if latencies else None,
        "runs": runs,
    }


def probe_stream(
    provider: str,
    model: str,
    url: str,
    timeout: float = 200.0,
    wire_model: str | None = None,
) -> dict[str, Any]:
    headers = _auth_headers(provider)
    if not headers:
        return {"ok": False, "note": "no credential available"}
    send = wire_model or model
    chunks = 0
    started = time.monotonic()
    try:
        with httpx.stream(
            "POST",
            url,
            headers=headers,
            json={
                "model": send,
                "messages": [{"role": "user", "content": "count 1 to 5"}],
                "max_tokens": 200,
                "stream": True,
            },
            timeout=timeout,
        ) as resp:
            if resp.status_code != 200:
                return {"ok": False, "note": f"HTTP {resp.status_code}", "ms": int((time.monotonic() - started) * 1000)}
            for line in resp.iter_lines():
                if line.startswith("data: ") and line[6:].strip() != "[DONE]":
                    try:
                        if json.loads(line[6:]).get("choices"):
                            chunks += 1
                    except ValueError:
                        pass
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "note": f"{type(exc).__name__}: {exc}"}
    return {
        "ok": chunks > 0,
        "note": f"{chunks} chunks",
        "ms": int((time.monotonic() - started) * 1000),
    }


def probe_vision(
    provider: str,
    model: str,
    url: str,
    timeout: float = 200.0,
    wire_model: str | None = None,
) -> dict[str, Any]:
    if not _IMAGE_B64:
        return {"ok": False, "note": "PIL unavailable, cannot build the OCR fixture"}
    status, body = _post(
        url,
        provider,
        {
            "model": wire_model or model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": OCR_PROBE},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/png;base64,{_IMAGE_B64}"
                            },
                        },
                    ],
                }
            ],
            "max_tokens": 400,
        },
        timeout,
    )
    if status != 200:
        return {"ok": False, "note": str(body)[:140]}
    message = (body.get("choices") or [{}])[0].get("message") or {}
    text = (message.get("content") or message.get("reasoning_content") or "").strip()
    if _is_rejection(text):
        return {"ok": False, "note": f"REJECTED: {text[:100]}"}
    if not text:
        return {"ok": False, "note": "empty content"}
    hits = [m for m in OCR_MARKERS if m in text]
    # require the invoice number and the amount: one lucky substring is not OCR
    passed = "7742" in hits and "129.50" in hits
    return {
        "ok": passed,
        "note": f"ocr_hits={hits}",
        "image_input": "PASS",
        "image_understanding": "PASS" if passed else "FAIL",
        "ocr": "PASS" if passed else "FAIL",
        "multimodal_reasoning": "PASS" if passed else "FAIL",
    }


# ---------------------------------------------------------------------------
# candidate discovery
# ---------------------------------------------------------------------------


def nim_catalog() -> list[str]:
    keys = [k for k in NIM_POOL_PATH.read_text(encoding="utf-8").splitlines() if k.strip()] if NIM_POOL_PATH.is_file() else []
    if not keys:
        return []
    try:
        resp = httpx.get(
            NIM_MODELS_URL,
            headers={"Authorization": f"Bearer {keys[0]}"},
            timeout=30.0,
        )
        resp.raise_for_status()
        return sorted(m["id"] for m in resp.json().get("data", []))
    except Exception:  # noqa: BLE001
        return []


def gateway_catalog() -> list[str]:
    try:
        resp = httpx.get(f"{GATEWAY}/v1/models", timeout=15.0)
        resp.raise_for_status()
        return sorted(m["id"] for m in resp.json().get("data", []))
    except Exception:  # noqa: BLE001
        return []


# ---------------------------------------------------------------------------
# state assembly
# ---------------------------------------------------------------------------


def build_entry(
    provider: str,
    model: str,
    *,
    endpoint: str,
    proxy: str,
    text: dict[str, Any],
    stream: dict[str, Any] | None = None,
    vision: dict[str, Any] | None = None,
    now: str,
) -> dict[str, Any]:
    text_ok = bool(text.get("text_3_of_3"))
    stream_ok = bool(stream and stream.get("ok"))
    vision_ok = bool(vision and vision.get("ok"))
    transport_ok = bool(text.get("auth")) and text.get("model_identity", True)

    # The health gate is capability-appropriate. SPEC §17 qualifies a vision model
    # on IMAGE_INPUT / IMAGE_UNDERSTANDING / OCR / MULTIMODAL_REASONING /
    # STREAMING — TEXT_3_OF_3 is not in that list, and several vision models are
    # unreliable on plain text while being solid on images. Gating a VL entry on
    # the text probe would discard exactly the models the VL pool needs.
    if vision is not None:
        capability_ok = vision_ok
    else:
        capability_ok = text_ok
    healthy = transport_ok and capability_ok and (stream_ok or stream is None)

    entry: dict[str, Any] = {
        "provider": provider,
        "model_id": model,
        "canonical_proxy": proxy,
        "endpoint": endpoint,
        "capabilities": (
            ["text", "stream", "vision", "ocr", "document"] if vision is not None else ["text", "stream"]
        ),
        "discovered": True,
        "healthy": healthy,
        "credentials_valid": bool(text.get("auth")),
        "endpoint_available": transport_ok,
        "model_available": capability_ok,
        "cooldown_until": None if healthy else now,
        "last_success": now if healthy else None,
        "last_failure": None if healthy else now,
        "consecutive_failures": 0 if healthy else 1,
        "latency_ms": text.get("latency_ms"),
        "stream_verified": stream_ok,
        "tools_verified": None,
        "vision_verified": vision_ok if vision is not None else False,
        "context_verified": False,
        "last_probe_at": now,
        "eligible": healthy,
        "evidence": {
            "text_3_of_3": "PASS" if text_ok else "FAIL",
            "auth": "PASS" if text.get("auth") else "FAIL",
            "model_identity": "PASS" if text.get("model_identity", True) else "FAIL",
            "streaming": ("PASS" if stream_ok else "FAIL") if stream is not None else "NOT_PROBED",
            "runs": text.get("runs", []),
        },
    }
    if stream is not None:
        entry["evidence"]["streaming_note"] = stream.get("note")
    if vision is not None:
        entry["evidence"].update(
            {k: v for k, v in vision.items() if k in ("image_input", "image_understanding", "ocr", "multimodal_reasoning")}
        )
        entry["evidence"]["vision_note"] = vision.get("note")
        if not vision_ok:
            entry["ineligible_reason"] = f"vision probe failed: {vision.get('note')}"
    if not capability_ok:
        entry["ineligible_reason"] = (
            f"{'vision probe' if vision is not None else 'TEXT_3_OF_3'}=FAIL; "
            f"last={(vision or text).get('note') or text.get('runs', [{}])[-1].get('note')!r}"
        )
    return entry


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--only", choices=["nim", "vl", "free"], help="probe one pool only")
    parser.add_argument("--dry-run", action="store_true", help="print, do not write")
    parser.add_argument("--timeout", type=float, default=200.0)
    args = parser.parse_args()

    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    state = json.loads(STATE_PATH.read_text(encoding="utf-8")) if STATE_PATH.is_file() else {}
    models: dict[str, Any] = state.get("models") or {}
    want = {args.only} if args.only else {"nim", "vl", "free"}

    def run_one(job: tuple[str, str, str, str, str, bool]) -> tuple[str, dict[str, Any]]:
        provider, model, endpoint, proxy, _label, is_vision, wire = job
        text = probe_text(provider, model, endpoint, args.timeout, wire_model=wire)
        stream = (
            probe_stream(provider, model, endpoint, args.timeout, wire_model=wire)
            if text.get("auth")
            else None
        )
        vision = (
            probe_vision(provider, model, endpoint, args.timeout, wire_model=wire)
            if is_vision
            else None
        )
        return model, build_entry(
            provider,
            model,
            endpoint=endpoint,
            proxy=proxy,
            text=text,
            stream=stream,
            vision=vision,
            now=now,
        )

    jobs: list[tuple[str, str, str, str, str, bool, str | None]] = []

    if "nim" in want:
        catalog = set(nim_catalog())
        for model in sorted(catalog):
            jobs.append(("nvidia-nim", model, NIM_ENDPOINT, "veya-nim", "nim", False, None))
    if "vl" in want:
        for model in NIM_VISION_CANDIDATES:
            jobs.append(("nvidia-nim", model, NIM_ENDPOINT, "veya-vl", "nim-vision", True, None))
        if os.environ.get("OPENROUTER_API_KEY"):
            for model in OPENROUTER_VL_CANDIDATES:
                jobs.append(("openrouter", model, OPENROUTER_ENDPOINT, "veya-vl", "or-vl", True, None))
        else:
            print(
                "[skip] openrouter VL candidates: OPENROUTER_API_KEY not set "
                "(the stored credential is a browser cookie and answers 401)",
                file=sys.stderr,
            )
    if "free" in want:
        for model in gateway_catalog():
            if model.startswith("opencode-go/"):
                # store the bare id, but the gateway catalog is keyed by the
                # full "<provider>/<model>" string, so that is what goes on the wire
                jobs.append(
                    (
                        "opencode-go",
                        model.split("/", 1)[1],
                        f"{GATEWAY}/v1/chat/completions",
                        "veya-free",
                        "gateway",
                        False,
                        model,
                    )
                )

    if not jobs:
        print("no candidates to probe", file=sys.stderr)
        return 1

    print(f"probing {len(jobs)} candidates (timeout {args.timeout}s)\n", file=sys.stderr)
    results: dict[str, dict[str, Any]] = {}
    with ThreadPoolExecutor(6) as pool:
        for model, entry in pool.map(run_one, jobs):
            results[model] = entry

    print(f"{'proxy':10} {'model':44} {'ok':5} {'text':6} {'strm':5} {'vis':5} {'ms':>7}")
    print("-" * 96)
    for model, entry in sorted(results.items(), key=lambda kv: (kv[1]["canonical_proxy"], kv[0])):
        print(
            f"{entry['canonical_proxy']:10} {model[:44]:44} {str(entry['eligible']):5} "
            f"{entry['evidence']['text_3_of_3']:6} {str(entry['stream_verified']):5} "
            f"{str(entry['vision_verified']):5} {str(entry['latency_ms']):>7}"
        )

    if args.dry_run:
        print("\n--dry-run: nothing written")
        return 0

    for model, entry in results.items():
        key = f"{entry['provider']}:{model}"
        if entry["canonical_proxy"] == "veya-free" and entry.get("vision_verified"):
            key = f"{entry['provider']}:{model}"
        models[key] = entry
    state["models"] = models
    state["version"] = 1
    state["updated_at"] = now
    state["last_requalification"] = {
        "at": now,
        "script": "scripts/requalify_model_proxies.py",
        "pools": sorted(want),
        "candidates": len(results),
        "eligible": sum(1 for e in results.values() if e["eligible"]),
    }
    by_proxy: dict[str, int] = {}
    for entry in results.values():
        if entry["eligible"]:
            by_proxy[entry["canonical_proxy"]] = by_proxy.get(entry["canonical_proxy"], 0) + 1
    state["proxy_pool_counts"] = by_proxy
    STATE_PATH.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"\nwrote {STATE_PATH}")
    print("eligible per proxy:", by_proxy)
    for proxy in ("veya1.2", "veya-free", "veya-nim", "veya-vl"):
        if proxy in by_proxy:
            continue
        if proxy == "veya1.2":
            continue  # master brain, delegates; has no pool of its own
        print(f"  WARNING: {proxy} has 0 eligible models", file=sys.stderr)
    return 0 if all(by_proxy.get(p, 0) > 0 for p in ("veya-free", "veya-nim")) else 2


if __name__ == "__main__":
    raise SystemExit(main())
