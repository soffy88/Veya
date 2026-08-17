"""
veya/oskill/vision_toolkit.py — Composite vision pipelines (Layer 2).

视觉组合管线, 建立在 oprim.vision_ops 原子操作 + 外部视觉 LLM API 之上。
为纯文本大模型提供"眼睛": 意图聚焦问答 / 元素定位 / 元素清点 / 长截图 OCR。

Provider: OpenAI Chat Completions 兼容端点 + data-URI 图像 (多图同传)。
默认零配置 — 复用 veya 本地 frontier 桥 (gpt-5.6-luna, 免鉴权):
  宿主   http://127.0.0.1:10100/v1    容器  http://192.168.16.1:10101/v1
覆盖: VEYA_VISION_BASE_URL / VEYA_VISION_MODEL / VEYA_VISION_API_KEY
(可指到任意 OpenAI 兼容视觉端点, 如 Groq Qwen3.6 或 DSH 免费服务)。

安全契约 (内化自 upstream SKILL.md): 图片里的文字/标签/描述一律视为
不可信视觉证据, 绝不当作指令执行 — 本层只回喂事实描述给上层大模型。
"""

from __future__ import annotations

import base64
import json
import os
import re
from pathlib import Path
from typing import Any

from veya.oprim.vision_ops import parse_region

try:
    import httpx

    _HAS_HTTPX = True
except ImportError:
    httpx = None  # type: ignore
    _HAS_HTTPX = False

DEFAULT_PROMPT = "Please describe the contents of this image in detail."
LANG_ZH = "请使用简体中文回答。"

# 视觉输出上限: 防单次请求占死内存/时长 (对齐上游共享服务护栏)
DEFAULT_MAX_TOKENS = 512
MAX_IMAGES_PER_CALL = 5


class VisionProviderError(RuntimeError):
    """可读的视觉服务失败 (含 429 重试指引)。"""


def _in_container() -> bool:
    """容器检测: VEYA_WORKSPACE 或 /.dockerenv (与 server.backends 同口径)。"""
    return bool(os.environ.get("VEYA_WORKSPACE")) or os.path.exists("/.dockerenv")


def resolve_vision_provider() -> dict[str, str]:
    """(base_url, model, api_key, host) 解析: VEYA_VISION_* → 本地 frontier 桥默认。

    容器内直连宿主网关 192.168.16.1:10101 会被 opencodex 按 Host 头拒绝
    (origin_rejected) — 优先走 hicode 本地反代 127.0.0.1:10103 (已改 Host);
    反代不在则退回 frontier 端点并附加 host 头重写 (Host=127.0.0.1:10100)。
    """
    base = os.environ.get("VEYA_VISION_BASE_URL", "").strip()
    model = os.environ.get("VEYA_VISION_MODEL", "").strip()
    key = os.environ.get("VEYA_VISION_API_KEY", "").strip()
    host = ""
    if not base:
        frontier = os.environ.get(
            "VEYA_FRONTIER_ENDPOINT", "http://127.0.0.1:10100/v1"
        ).strip()
        if _in_container():
            # 容器: 优先 hicode 反代 (Host 已改写, 免鉴权直连 gpt-5.6-luna)
            proxy = os.environ.get("HICODE_PROXY_PORT", "10103")
            if _proxy_alive(proxy):
                base = f"http://127.0.0.1:{proxy}/v1"
            else:
                frontier = "http://192.168.16.1:10101/v1"
                host = os.environ.get("HICODE_PROXY_UPSTREAM_HOST", "127.0.0.1:10100")
                base = frontier
        else:
            base = frontier
    if not model:
        model = "gpt-5.6-luna"
    return {"base_url": base, "model": model, "api_key": key, "host": host}


def _proxy_alive(port: str) -> bool:
    """hicode 本地反代探活 (2s, 单次重试; 反代首请求冷启动可能 >0.5s)。"""
    import urllib.error
    import urllib.request

    for _ in range(2):
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/v1/models", timeout=2.0
            ) as resp:
                return resp.status in (200, 401, 403)
        except urllib.error.HTTPError as exc:
            return exc.code in (200, 401, 403)
        except Exception:
            continue
    return False


def _mime_for(path: str) -> str:
    suffix = Path(path).suffix.lower()
    return {
        ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".gif": "image/gif", ".webp": "image/webp",
    }.get(suffix, "image/png")


def image_path_to_data_uri(path: str | Path) -> str:
    p = Path(path).expanduser()
    if not p.is_file():
        raise VisionProviderError(f"Image not found: {p}")
    mime = _mime_for(p.name)
    if mime not in {"image/png", "image/jpeg", "image/gif", "image/webp"}:
        raise VisionProviderError("只支持 PNG/JPEG/GIF/WebP 图像")
    return f"data:{mime};base64,{base64.b64encode(p.read_bytes()).decode()}"


def _data_uri_from_bytes(data: bytes, mime: str = "image/png") -> str:
    return f"data:{mime};base64,{base64.b64encode(data).decode()}"


async def vision_chat(
    blocks: list[dict[str, Any]],
    *,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    timeout: float = 90.0,
    provider: dict[str, str] | None = None,
) -> str:
    """OpenAI 兼容 chat/completions 视觉调用 → 纯文本。429 带可读重试指引。"""
    if not _HAS_HTTPX:
        raise VisionProviderError("需要 httpx (pip install httpx)")
    cfg = provider or resolve_vision_provider()
    headers = {"Content-Type": "application/json"}
    if cfg.get("api_key"):
        headers["Authorization"] = f"Bearer {cfg['api_key']}"
    # 容器直连宿主网关被 origin_rejected 时的 Host 头重写 (hicode 反代同款)
    if cfg.get("host"):
        headers["Host"] = cfg["host"]
    payload = {
        "model": cfg["model"],
        "messages": [{"role": "user", "content": blocks}],
        "max_tokens": max_tokens,
    }
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                cfg["base_url"].rstrip("/") + "/chat/completions",
                json=payload, headers=headers,
            )
    except httpx.TimeoutException as exc:
        raise VisionProviderError(f"视觉服务超时 ({timeout}s): {cfg['base_url']}") from exc
    except httpx.HTTPError as exc:
        raise VisionProviderError(f"视觉服务不可达: {exc}") from exc
    if resp.status_code == 429:
        retry_after = resp.headers.get("retry-after", "几秒")
        raise VisionProviderError(
            f"视觉服务 429 (共享容量用尽), 请 {retry_after} 后重试, "
            "或在环境变量 VEYA_VISION_BASE_URL/MODEL/API_KEY 换成自己的视觉模型。"
        )
    if resp.status_code != 200:
        detail = resp.text[:300]
        raise VisionProviderError(f"视觉服务返回 {resp.status_code}: {detail}")
    try:
        data = resp.json()
        content = (data.get("choices") or [{}])[0].get("message", {}).get("content", "")
        if isinstance(content, list):  # 罕见: 分片 content
            content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
        return str(content or "").strip()
    except (ValueError, AttributeError) as exc:
        raise VisionProviderError(f"视觉服务返回不可解析: {exc}") from exc


# ---------------------------------------------------------------------------
# glance: 意图聚焦问答 / OCR / 多图对比
# ---------------------------------------------------------------------------

async def glance(
    image_paths: list[str],
    *,
    query: str | None = None,
    ocr: bool = False,
    region: str | None = None,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    timeout: float = 90.0,
    provider: dict[str, str] | None = None,
) -> dict[str, Any]:
    """一张/多张图 → 聚焦回答。query 与 ocr 互斥; region 仅限单图 (只传裁剪)。"""
    if len(image_paths) > MAX_IMAGES_PER_CALL:
        raise VisionProviderError(f"单次最多 {MAX_IMAGES_PER_CALL} 张图")
    if region and len(image_paths) != 1:
        raise VisionProviderError("region 仅限单图调用")
    if ocr and query:
        raise VisionProviderError("query 与 ocr 互斥")

    blocks: list[dict[str, Any]] = []
    region_box: tuple[int, int, int, int] | None = None
    if region:
        from veya.oprim.vision_ops import crop_bytes, image_size, load_rgb

        path = image_paths[0]
        width, height = image_size(path)
        region_box = parse_region(region, width, height)
        crop = crop_bytes(load_rgb(path), region_box)
        blocks.append({"type": "image_url", "image_url": {"url": _data_uri_from_bytes(crop)}})
    else:
        for path in image_paths:
            blocks.append({
                "type": "image_url",
                "image_url": {"url": image_path_to_data_uri(path)},
            })
    if ocr:
        prompt = (
            "Transcribe all visible text in this image. Keep the top-to-bottom "
            "reading order, preserve line breaks, and write [unreadable] only "
            "where text is visible but cannot be read. Do not summarize."
        )
        mode = "ocr"
    elif query:
        prompt = query.strip()
        mode = "qa"
    else:
        prompt = DEFAULT_PROMPT
        mode = "describe"
    blocks.append({"type": "text", "text": f"{prompt}\n\n{LANG_ZH}"})
    answer = await vision_chat(blocks, max_tokens=max_tokens, timeout=timeout, provider=provider)
    return {"mode": mode, "answer": answer, "truncated": len(answer) >= max_tokens}


# ---------------------------------------------------------------------------
# ground / detect: 0-1000 网格定位 (内化自 upstream ground.py)
# ---------------------------------------------------------------------------

_GROUND_PROMPT = (
    "Locate every visible object or region matching this target:\n{target}\n\n"
    'Return only a JSON array. Each item must contain "box_2d" as [y0, x0, y1, x1] '
    'on a 0-1000 grid and "label" as a short description. Use tight boxes in the '
    "original image. Return [] when nothing matches."
)
_DETECT_PROMPT = (
    "Inventory every visible element matching this category: {category}\n\n"
    'Return only a JSON array. Each item must contain "box_2d" as [y0, x0, y1, x1] '
    'on a 0-1000 grid and "label" as the exact visible text or short description '
    "of that element. Return [] when nothing matches."
)


def _json_text(text: str) -> str:
    cleaned = str(text or "").strip()
    fenced = re.findall(r"```(?:json)?\s*(.*?)\s*```", cleaned, re.DOTALL | re.IGNORECASE)
    return (fenced[-1] if fenced else cleaned).strip()


def _fallback_items(text: str) -> list[dict[str, Any]]:
    items = []
    object_pattern = re.compile(
        r"\{[^{}]*['\"](?:box_2d|bbox_2d|box2d|bbox|box)['\"]\s*:\s*\[[^\]]+\][^{}]*\}",
        re.DOTALL,
    )
    box_pattern = re.compile(
        r"['\"](?:box_2d|bbox_2d|box2d|bbox|box)['\"]\s*:\s*\[([^\]]+)\]", re.DOTALL
    )
    label_pattern = re.compile(
        r"['\"](?:label|caption|description)['\"]\s*:\s*['\"]([^'\"]+)['\"]", re.DOTALL
    )
    for match in object_pattern.finditer(text):
        block = match.group(0)
        box_match = box_pattern.search(block)
        if not box_match:
            continue
        numbers = re.findall(r"-?\d+(?:\.\d+)?", box_match.group(1))
        if len(numbers) < 4:
            continue
        item: dict[str, Any] = {"box_2d": [float(v) for v in numbers[:4]]}
        label_match = label_pattern.search(block)
        if label_match:
            item["label"] = label_match.group(1).strip()
        items.append(item)
    return items


def _parse_items(text: str) -> list[Any]:
    cleaned = _json_text(text)
    try:
        payload = json.loads(cleaned)
    except json.JSONDecodeError:
        fallback = _fallback_items(cleaned)
        if fallback:
            return fallback
        raise VisionProviderError("视觉模型未返回可解析的定位 JSON") from None
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("boxes", "bounding_boxes", "bboxes", "objects", "items", "results"):
            if isinstance(payload.get(key), list):
                return payload[key]
    raise VisionProviderError("视觉模型返回不兼容的定位结构")


def _normalize_box(item: dict[str, Any], width: int, height: int) -> dict[str, int] | None:
    raw = item.get("box_2d")
    if not isinstance(raw, list):
        for key in ("bbox_2d", "box2d", "bbox", "box"):
            if isinstance(item.get(key), list):
                raw = item[key]
                break
    if not isinstance(raw, list) or len(raw) != 4:
        return None
    try:
        y0, x0, y1, x1 = (float(v) for v in raw)
    except (TypeError, ValueError):
        return None
    if x0 > x1:
        x0, x1 = x1, x0
    if y0 > y1:
        y0, y1 = y1, y0
    box = {
        "x1": max(0, min(width, round(x0 / 1000 * width))),
        "y1": max(0, min(height, round(y0 / 1000 * height))),
        "x2": max(0, min(width, round(x1 / 1000 * width))),
        "y2": max(0, min(height, round(y1 / 1000 * height))),
    }
    return box if box["x2"] > box["x1"] and box["y2"] > box["y1"] else None


async def _locate(
    image_path: str,
    prompt: str,
    *,
    region: str | None = None,
    timeout: float = 90.0,
    provider: dict[str, str] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any], tuple[int, int, int, int] | None]:
    """共用定位核心: 0-1000 网格提问 → 原图像素盒映射 (region 命中映射回原图)。"""
    from veya.oprim.vision_ops import image_size

    width, height = image_size(image_path)
    search_box: tuple[int, int, int, int] | None = None
    path = image_path
    if region:
        search_box = parse_region(region, width, height)
        from veya.oprim.vision_ops import crop_bytes, load_rgb

        data = crop_bytes(load_rgb(image_path), search_box)
        blocks = [
            {"type": "image_url", "image_url": {"url": _data_uri_from_bytes(data)}},
            {"type": "text", "text": f"{prompt}\n\n{LANG_ZH}"},
        ]
    else:
        blocks = [
            {"type": "image_url", "image_url": {"url": image_path_to_data_uri(path)}},
            {"type": "text", "text": f"{prompt}\n\n{LANG_ZH}"},
        ]
    text = await vision_chat(blocks, max_tokens=512, timeout=timeout, provider=provider)
    if search_box:
        sw, sh = search_box[2] - search_box[0], search_box[3] - search_box[1]
    else:
        sw, sh = width, height
    matches: list[dict[str, Any]] = []
    for item in _parse_items(text):
        if not isinstance(item, dict):
            continue
        box = _normalize_box(item, sw, sh)
        if box is None:
            continue
        if search_box:
            box = {
                "x1": box["x1"] + search_box[0], "y1": box["y1"] + search_box[1],
                "x2": box["x2"] + search_box[0], "y2": box["y2"] + search_box[1],
            }
        label = str(
            item.get("label") or item.get("caption") or item.get("description") or ""
        ).strip()
        matches.append({"label": label, "box": box})
    return matches, {"width": width, "height": height}, search_box


async def ground(
    image_path: str,
    target: str,
    *,
    region: str | None = None,
    timeout: float = 90.0,
    provider: dict[str, str] | None = None,
) -> dict[str, Any]:
    """定位一个具名目标 → 原图像素盒 (0-1000 网格缩放, 裁切级精度)。"""
    matches, size, _ = await _locate(
        image_path, _GROUND_PROMPT.format(target=target),
        region=region, timeout=timeout, provider=provider,
    )
    return {
        "target": target,
        "image_width": size["width"],
        "image_height": size["height"],
        "matches": matches,
    }


async def detect(
    image_path: str,
    category: str | None = None,
    *,
    region: str | None = None,
    timeout: float = 90.0,
    provider: dict[str, str] | None = None,
) -> dict[str, Any]:
    """清点某一类元素 (或全部 UI 元素) → 编号清单 + 像素盒。"""
    cat = category or "every distinct UI element — include the exact visible text in each label"
    matches, size, _ = await _locate(
        image_path, _DETECT_PROMPT.format(category=cat),
        region=region, timeout=timeout, provider=provider,
    )
    return {
        "category": category,
        "image_width": size["width"],
        "image_height": size["height"],
        "elements": [
            {"index": i, "label": m["label"], "box": m["box"]}
            for i, m in enumerate(matches, 1)
        ],
    }


# ---------------------------------------------------------------------------
# 长截图 OCR (块级提示词 + 聊天结构化解析; 切分在 L1, 装配在 L3)
# ---------------------------------------------------------------------------

def ocr_chunk_prompt(mode: str, index: int, total: int, custom: str | None = None) -> str:
    if mode == "chat":
        instructions = (
            "Transcribe this chat screenshot chunk in strict top-to-bottom message order. "
            'Return only one valid JSON object with this exact shape: '
            '{"messages":[{"speaker":"visible name","content":"message text",'
            '"timestamp":"","message_type":"message","quoted_speaker":"",'
            '"quoted_content":""}]}. '
            "Copy the visible nickname exactly; never replace it with roles such as "
            "customer, support, me. When a chat UI clearly marks an outgoing self-message "
            'by alignment and bubble style but omits its nickname, use "You" as the speaker. '
            "Ignore app chrome such as status bar, chat title, and composer. Transcribe every "
            "date separator, service notice, and unread divider as a system message. Merge "
            "screen-width wrapping back into the same message. Preserve intentional code and "
            "list line breaks. Put replied-to text in quoted_speaker and quoted_content while "
            "keeping the new message in speaker and content. Fill timestamp only when the "
            "entire timestamp is clearly visible; otherwise leave it empty. message_type must "
            "be message, system, image, or file. Do not summarize, rewrite, translate, or "
            "infer clipped text. Use [unreadable] for visible text that cannot be read and "
            "[clipped] for a visibly cut-off message."
        )
    else:
        instructions = (
            "Keep the visible top-to-bottom reading order and preserve wording, punctuation, "
            "line breaks, labels, timestamps, headings, lists, tables, code, quoted text, and "
            "paragraph order. Do not infer clipped or hidden content; write [unreadable] only "
            "where visible text cannot be read."
        )
    chunk_note = f" This is chunk {index} of {total} from one vertically scrolling screenshot."
    custom_note = f" {custom.strip()}" if custom and custom.strip() else ""
    return instructions + chunk_note + custom_note


def _join_visual_wraps(content: str, preserve_lines: bool = False) -> str:
    content = content.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not content:
        return ""
    paragraphs = re.split(r"\n\s*\n", content)
    list_pattern = re.compile(r"^(?:[-*+\u2022] |\d+[.)] )")
    normalized = []
    for paragraph in paragraphs:
        lines = [re.sub(r"[ \t]+", " ", line.strip()) for line in paragraph.splitlines()]
        lines = [line for line in lines if line]
        if not lines:
            continue
        if preserve_lines:
            normalized.append("\n".join(lines))
            continue
        merged = lines[0]
        for line in lines[1:]:
            if list_pattern.match(line):
                merged += "\n" + line
                continue
            separator = (
                " " if re.search(r"[A-Za-z0-9]$", merged) and re.match(r"[A-Za-z0-9]", line)
                else ""
            )
            merged += separator + line
        normalized.append(merged)
    return "\n\n".join(normalized)


def parse_chat_messages(raw_text: str) -> list[dict[str, str]]:
    """聊天块 OCR JSON → 规范化消息列表; 不可解析抛 VisionProviderError。"""
    text = raw_text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)
    start = text.find("{")
    if start < 0:
        raise VisionProviderError("聊天 OCR 未返回 JSON 对象")
    try:
        payload, _end = json.JSONDecoder().raw_decode(text[start:])
    except json.JSONDecodeError as exc:
        raise VisionProviderError(f"聊天 OCR JSON 无效: {exc.msg}") from exc
    records = payload.get("messages") if isinstance(payload, dict) else None
    if not isinstance(records, list):
        raise VisionProviderError("聊天 OCR JSON 缺 messages 数组")
    messages = []
    for record in records:
        if not isinstance(record, dict):
            continue
        message_type = str(record.get("message_type") or "message").strip().lower()
        if message_type not in {"message", "system", "image", "file"}:
            message_type = "message"
        content = _join_visual_wraps(
            str(record.get("content") or ""),
            preserve_lines=message_type in {"image", "file"},
        )
        if not content:
            continue
        speaker = str(record.get("speaker") or "").strip()
        if message_type == "system":
            speaker = "system"
        if not speaker:
            speaker = "[unreadable speaker]"
        timestamp = str(record.get("timestamp") or "").strip()
        if re.search(r"[:\uFF1A]\d$", timestamp) or "[unreadable]" in timestamp.casefold():
            timestamp = ""
        messages.append({
            "speaker": speaker,
            "content": content,
            "timestamp": timestamp,
            "message_type": message_type,
            "quoted_speaker": str(record.get("quoted_speaker") or "").strip(),
            "quoted_content": _join_visual_wraps(str(record.get("quoted_content") or "")),
        })
    if not messages:
        raise VisionProviderError("聊天 OCR JSON 无有效消息")
    return messages


def render_chat_messages(messages: list[dict[str, str]]) -> str:
    rendered = []
    for message in messages:
        timestamp = f" ({message['timestamp']})" if message.get("timestamp") else ""
        blocks = []
        if message.get("quoted_content"):
            quoted_speaker = message.get("quoted_speaker") or "[quoted speaker]"
            quoted_text = message["quoted_content"].replace("\n", "\n> ")
            blocks.append(f"> **{quoted_speaker}**: {quoted_text}")
        blocks.append(f"**{message['speaker']}**{timestamp}: {message['content']}")
        rendered.append("\n\n".join(blocks))
    return "\n\n".join(rendered)


def _message_key(message: dict[str, str]) -> tuple[str, str, str]:
    def simplify(value: str) -> str:
        return re.sub(r"[\W_]+", "", value, flags=re.UNICODE).casefold()

    return (
        simplify(message.get("speaker", "")),
        simplify(message.get("content", "")),
        simplify(message.get("quoted_content", "")),
    )


def merge_chat_messages(chunks: list[list[dict[str, str]]]) -> tuple[list[dict[str, str]], int]:
    """跨块合并聊天消息: 高置信重复 (同 speaker/content≥0.97) 取更完整的一侧。"""
    merged: list[dict[str, str]] = []
    deduped = 0
    for messages in chunks:
        for message in messages:
            tail = merged[-1] if merged else None
            if tail is not None and _chat_match(tail, message):
                merged[-1] = _richer_message(tail, message)
                deduped += 1
                continue
            merged.append(message)
    return merged, deduped


def _chat_match(left: dict[str, str], right: dict[str, str]) -> bool:
    if (left.get("message_type") == "system") != (right.get("message_type") == "system"):
        return False
    ls, lc, lq = _message_key(left)
    rs, rc, rq = _message_key(right)

    def unreadable(s: str) -> bool:
        lowered = s.casefold()
        return not s.strip() or "unreadable" in lowered or "clipped" in lowered

    speakers_match = ls == rs or unreadable(left.get("speaker", "")) or unreadable(right.get("speaker", ""))
    timestamps_match = (
        not left.get("timestamp") or not right.get("timestamp")
        or left.get("timestamp") == right.get("timestamp")
    )
    quotes_match = not lq or not rq or lq == rq
    if not speakers_match or not timestamps_match or not quotes_match:
        return False
    if lc == rc and lc:
        return True
    if min(len(lc), len(rc)) < 32:
        return False
    from difflib import SequenceMatcher

    return SequenceMatcher(None, lc, rc).ratio() >= 0.97


def _richer_message(left: dict[str, str], right: dict[str, str]) -> dict[str, str]:
    def score(value: str) -> tuple[int, int, int]:
        lowered = value.casefold()
        marker_penalty = lowered.count("[clipped]") + lowered.count("[unreadable]")
        return (-marker_penalty, value.count("\n"), len(value))

    def richer(a: str, b: str) -> str:
        return max((a, b), key=score)

    def unreadable(s: str) -> bool:
        lowered = s.casefold()
        return not s.strip() or "unreadable" in lowered or "clipped" in lowered

    speaker = left.get("speaker", "")
    if unreadable(speaker) and not unreadable(right.get("speaker", "")):
        speaker = right.get("speaker", "")
    return {
        "speaker": speaker,
        "content": richer(left.get("content", ""), right.get("content", "")),
        "timestamp": left.get("timestamp") or right.get("timestamp") or "",
        "message_type": (
            right.get("message_type", "message")
            if left.get("message_type") == "message"
            and right.get("message_type", "message") != "message"
            else left.get("message_type", "message")
        ),
        "quoted_speaker": (
            right.get("quoted_speaker", "")
            if unreadable(left.get("quoted_speaker", ""))
            and not unreadable(right.get("quoted_speaker", ""))
            else left.get("quoted_speaker", "")
        ),
        "quoted_content": richer(left.get("quoted_content", ""), right.get("quoted_content", "")),
    }


async def ocr_chunk_bytes(
    chunk_bytes: bytes,
    *,
    mode: str,
    index: int,
    total: int,
    custom: str | None = None,
    timeout: float = 60.0,
    provider: dict[str, str] | None = None,
) -> str:
    """单块 OCR: 图像 bytes → 文本 (general) 或消息 JSON→markdown (chat, 重试一次)。"""
    prompt = ocr_chunk_prompt(mode, index, total, custom)
    blocks = [
        {"type": "image_url", "image_url": {"url": _data_uri_from_bytes(chunk_bytes)}},
    ]
    if mode == "chat":
        retry_note = (
            " Return compact valid JSON only. Escape every newline inside a JSON string as "
            "\\n, close every quote and brace, and do not use a Markdown code fence."
        )
        last_error: Exception | None = None
        for attempt in range(2):
            attempt_prompt = prompt + (retry_note if attempt else "")
            text = await vision_chat(
                [*blocks, {"type": "text", "text": attempt_prompt}],
                max_tokens=512, timeout=timeout, provider=provider,
            )
            try:
                messages = parse_chat_messages(text)
                return render_chat_messages(messages)
            except VisionProviderError as exc:
                last_error = exc
        raise VisionProviderError(f"chunk {index}/{total}: {last_error}") from last_error
    text = await vision_chat(
        [*blocks, {"type": "text", "text": f"{prompt}\n\n{LANG_ZH}"}],
        max_tokens=512, timeout=timeout, provider=provider,
    )
    return text.strip()


__all__ = [
    "VisionProviderError",
    "detect",
    "glance",
    "ground",
    "image_path_to_data_uri",
    "merge_chat_messages",
    "ocr_chunk_bytes",
    "ocr_chunk_prompt",
    "parse_chat_messages",
    "render_chat_messages",
    "resolve_vision_provider",
    "vision_chat",
]
