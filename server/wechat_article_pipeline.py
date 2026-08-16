"""server/wechat_article_pipeline.py — 公众号图文自动生产流水线 (主链路工具)。

写手(Writer)→ 配图(hevi resolve, best-effort)→ 审核(Reviewer, 只读不改)→
不通过则定向打回重写 → 再审核, 直到通过或达到 max_iterations 才收工
(超限不假装通过, 明确标 best-effort + 剩余问题)。

实现基于 3O 主库 oskill 内化 (单源契约, 不在 veya 重实现):
  * 闭环状态机 → oskill.wechat_review_loop.WechatReviewLoop (LLM 注入分离);
  * 写手/审核/改写 prompt → oskill.wechat_writing prompt 工厂;
  * 合规审核证据 → oskill.wechat_writing.scan_compliance 确定性扫描。
oskill 由 veya.platform 惰性注入 sys.path (子模块缺失时返回清晰错误,
不静默降级)。

铁律: 对主脑而言是一个不透明能力 (register 成一个工具), 内部自己的状态机循环,
不碰 coordinator_master 的主循环路由 (ARCHITECTURE_STABLE 冻结: 一个 LLM, 零程序
路由)。JSON 解析约定: 找首尾括号切片 json.loads, 失败即判定为"无信号"而非崩溃。
"""

from __future__ import annotations

import hashlib
import html as _html
import json
import time
from typing import Any

from veya.llm import llm_call

_OSKILL_CACHE: Any = None


def _get_oskill() -> Any | None:
    """惰性加载 3O 主库 oskill (veya.platform 注入 sys.path; 缺失返回 None)。"""
    global _OSKILL_CACHE
    if _OSKILL_CACHE is None:
        try:
            from veya.platform import oskill as _load

            _OSKILL_CACHE = _load()
        except Exception:
            _OSKILL_CACHE = False
    return _OSKILL_CACHE or None


def _parse_json_value(raw: str) -> Any:
    """LLM 输出→ JSON 值。找首尾括号切片解析, 失败返回 None(不做修复重试)。"""
    text = (raw or "").strip()
    for open_c, close_c in (("{", "}"), ("[", "]")):
        start, end = text.find(open_c), text.rfind(close_c)
        if start >= 0 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                continue
    return None


def _content_of(resp: dict) -> str:
    return ((resp.get("choices") or [{}])[0].get("message") or {}).get("content") or ""


# ── 闭环注入函数 (LLM 调用在 veya 侧, 状态机/prompt 在 oskill 侧) ────


def _make_writer(llm_kwargs: dict) -> Any:
    """写手注入: write_prompt 工厂 → llm_call → draft dict|None。"""

    async def _writer(topic: str, requirements: str, extra_constraints: str) -> dict | None:
        from oskill.wechat_writing import write_prompt

        p = write_prompt(topic, requirements=requirements)
        user = p["user"]
        if extra_constraints:
            user += f"\n\n上一版审核未通过, 必须解决以下问题:\n{extra_constraints}"
        resp = await llm_call(
            [
                {"role": "system", "content": p["system"]},
                {"role": "user", "content": user},
            ],
            max_tokens=3000,
            **llm_kwargs,
        )
        data = _parse_json_value(_content_of(resp))
        if not isinstance(data, dict) or not isinstance(data.get("sections"), list):
            return None
        return data

    return _writer


def _make_reviewer(llm_kwargs: dict) -> Any:
    """审核注入: reviewer_prompt 工厂 + 确定性合规扫描证据 → 审核 dict。"""

    async def _reviewer(
        draft: dict,
        image_results: list[dict],
        topic: str,
        requirements: str,
        image_miss_streak: int,
    ) -> dict:
        from oskill.wechat_writing import compliance_report, reviewer_prompt

        p = reviewer_prompt()
        sections_text = "\n\n".join(
            f"## {s.get('heading')} (配图: {img['status']}"
            + (f" - {img.get('reason', '')}" if img["status"] != "ok" else "")
            + f")\n{s.get('body')}"
            for s, img in zip(draft.get("sections", []), image_results, strict=True)
        )
        user = p["user_extra"].format(
            topic=topic,
            requirements=requirements,
            title=draft.get("title", ""),
            sections=sections_text,
            closing=draft.get("closing", ""),
            image_miss_streak=image_miss_streak,
        )
        # 确定性违禁词扫描命中作为证据注入 (审核第 2 条标准的可验证支撑)
        plain = _html_strip(draft.get("title", "") + "\n" + sections_text)
        report = compliance_report(plain)
        if report["hits"]:
            user += "\n\n确定性违禁词扫描命中(仅作参考, 请按上下文判断是否真违规):\n" + "\n".join(
                f"- [{h['criterion']}] 「{h['keyword']}」…{h['snippet']}…"
                for h in report["hits"][:10]
            )
        resp = await llm_call(
            [
                {"role": "system", "content": p["system"]},
                {"role": "user", "content": user},
            ],
            max_tokens=1500,
            **llm_kwargs,
        )
        data = _parse_json_value(_content_of(resp))
        if not isinstance(data, dict) or "pass" not in data:
            return {
                "pass": False,
                "issues": [
                    {
                        "criterion": "reviewer_output",
                        "section": None,
                        "detail": "审核输出无法解析为 JSON",
                        "fix_instruction": "重新生成审核结果",
                    }
                ],
            }
        data.setdefault("issues", [])
        return data

    return _reviewer


def _make_reviser(llm_kwargs: dict) -> Any:
    """定向改写注入: reviser_prompt 工厂 → patch 列表|None。"""

    async def _reviser(
        draft: dict, issues: list[dict], topic: str, requirements: str
    ) -> list | None:
        from oskill.wechat_writing import reviser_prompt

        p = reviser_prompt()
        issue_lines = "\n".join(
            f"- [{i.get('criterion')}] 章节「{i.get('section') or '整体'}」: "
            f"{i.get('detail')} → {i.get('fix_instruction')}"
            for i in issues
        )
        user = p["user_extra"].format(
            topic=topic,
            requirements=requirements,
            draft_json=json.dumps(draft, ensure_ascii=False),
            issues=issue_lines,
        )
        resp = await llm_call(
            [
                {"role": "system", "content": p["system"]},
                {"role": "user", "content": user},
            ],
            max_tokens=2000,
            **llm_kwargs,
        )
        data = _parse_json_value(_content_of(resp))
        return data if isinstance(data, list) else None

    return _reviser


def _html_strip(text: str) -> str:
    import re

    return re.sub(r"<[^>]+>", " ", text or "")


async def _resolve_image(brief: str, tools: Any) -> dict:
    """hevi 媒体网关 best-effort 配图。不可用/失败一律降级为 missing, 不重试不阻塞。"""
    if not tools.has("mcp_hevi"):
        return {"status": "missing", "reason": "mcp_hevi 网关未接入(hevi 服务不可达或未配置)"}
    try:
        raw = await tools.execute(
            "mcp_hevi", {"action": "resolve", "args": {"kind": "image", "query": brief}}
        )
    except Exception as exc:  # 任何失败都降级, 不让外部服务卡死流水线
        return {
            "status": "missing",
            "reason": f"mcp_hevi.resolve 调用失败: {type(exc).__name__}: {exc}",
        }
    path = _extract_media_path(raw)
    if not path:
        return {"status": "missing", "reason": "mcp_hevi.resolve 未返回可用文件路径/URL"}
    return {"status": "ok", "path": path}


def _extract_media_path(raw: str) -> str | None:
    """从 mcp_hevi 返回里找媒体路径/URL — 未知返回形态, 尽量兼容常见 key。"""
    import re

    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        data = None
    if isinstance(data, dict):
        for key in ("path", "file", "file_path", "local_path", "url", "image_url"):
            val = data.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
    text = (raw or "").strip()
    if text.startswith(("http://", "https://", "/")) or re.search(
        r"\.(png|jpe?g|webp|gif)(\?|$)", text, re.IGNORECASE
    ):
        return text
    return None


def _esc(text: Any) -> str:
    return _html.escape(str(text or ""), quote=False)


def render_html(title: str, sections: list[dict], closing: str, image_results: list[dict]) -> str:
    """公众号可直接粘贴的内联样式 HTML(纯函数, 不过 LLM, 保证前后可比对)。"""
    parts = [
        f'<h1 style="font-size:22px;font-weight:700;line-height:1.4;margin:0 0 16px;'
        f'color:#1a1a1a;">{_esc(title)}</h1>'
    ]
    for section, img in zip(sections, image_results, strict=True):
        parts.append(
            f'<h2 style="font-size:18px;font-weight:700;margin:24px 0 12px;'
            f'color:#1a1a1a;">{_esc(section.get("heading"))}</h2>'
        )
        for para in str(section.get("body") or "").split("\n"):
            para = para.strip()
            if para:
                parts.append(
                    f'<p style="font-size:16px;line-height:1.8;color:#333;'
                    f'margin:0 0 16px;">{_esc(para)}</p>'
                )
        if img.get("status") == "ok":
            parts.append(
                f'<p style="margin:0 0 16px;text-align:center;">'
                f'<img src="{_esc(img.get("path"))}" style="max-width:100%;border-radius:8px;" '
                f'alt="{_esc(section.get("heading"))}"></p>'
            )
        else:
            parts.append(
                f"<!-- TODO 配图缺失: {_esc(section.get('image_brief'))} "
                f"({_esc(img.get('reason'))}) -->"
            )
    parts.append(
        f'<p style="font-size:16px;line-height:1.8;color:#333;margin:24px 0 0;">{_esc(closing)}</p>'
    )
    return "\n".join(parts)


async def produce_wechat_article_tool(
    topic: str,
    requirements: str,
    max_iterations: int = 3,
    *,
    model: str | None = None,
    provider: str | None = None,
    config: dict | None = None,
    _master_tools: Any | None = None,
) -> str:
    """主脑工具: 写手↔审核打回闭环 (oskill 状态机), 落盘公众号 HTML + 草稿。"""
    oskill = _get_oskill()
    if oskill is None:
        return (
            "produce_wechat_article: 3O 主库 oskill 未挂载"
            " (platform/3O/oskill 子模块缺失), 无法生产公众号文章。"
        )
    from oskill.wechat_review_loop import WechatReviewLoop

    from server.tool_registry import _resolve_write_path

    tools = _master_tools
    if tools is None:
        from server.tool_registry import master_tools as tools

    llm_kwargs = {"model": model, "provider": provider, "config": config}
    max_iterations = max(1, min(int(max_iterations), 5))

    loop = WechatReviewLoop(
        writer=_make_writer(llm_kwargs),
        reviewer=_make_reviewer(llm_kwargs),
        reviser=_make_reviser(llm_kwargs),
        resolve_image=lambda brief: _resolve_image(brief, tools),
        max_iterations=max_iterations,
    )
    result = await loop.run(topic, requirements)

    draft = result["draft"]
    if draft is None:
        return (
            "produce_wechat_article: 写手未能生成合法草稿"
            "(LLM 输出无法解析为 JSON), 请重试或检查模型配置。"
        )

    article_html = render_html(
        draft.get("title", ""),
        draft.get("sections", []),
        draft.get("closing", ""),
        result["image_results"],
    )

    ts = time.strftime("%Y%m%d-%H%M%S")
    slug = hashlib.sha1(topic.encode("utf-8")).hexdigest()[:8]
    rel_dir = f"wechat_articles/{ts}-{slug}"
    html_path = _resolve_write_path(f"{rel_dir}/article.html")
    draft_path = _resolve_write_path(f"{rel_dir}/draft.json")
    html_path.parent.mkdir(parents=True, exist_ok=True)
    html_path.write_text(article_html, encoding="utf-8")
    draft_path.write_text(json.dumps(draft, ensure_ascii=False, indent=2), encoding="utf-8")

    passed = bool(result["passed"])
    lines = [
        "✅ 通过审核" if passed else "⚠️ 已达轮次上限, 未完全通过审核 (best-effort)",
        f"标题: {draft.get('title', '')}",
        f"迭代次数: {result['iterations']}",
        f"产物: {html_path}",
        f"草稿(结构化): {draft_path}",
    ]
    if not passed:
        lines.append("遗留问题:")
        for i in result["issues"]:
            lines.append(
                f"  - [{i.criterion}] {i.section or '整体'}: {i.detail}"
            )
    return "\n".join(lines)


def register(master_tools: Any) -> None:
    """把 produce_wechat_article 挂到主脑注册表(由 tool_registry 在装配期调用)。"""
    if master_tools.has("produce_wechat_article"):
        return
    master_tools.register(
        name="produce_wechat_article",
        description=(
            "端到端产出一篇完整、可直接粘贴进公众号编辑器的图文文章(内联样式 HTML)。"
            "USE THIS ONLY WHEN the user explicitly asks for a complete, publish-ready "
            "WeChat official-account (公众号) article/图文 to be produced — not for "
            "casual writing help or a short paragraph. Internally runs a writer→reviewer→"
            "revise loop (up to max_iterations rounds) that checks topic/requirement match, "
            "WeChat compliance wording, image-text relevance, and readability structure "
            "before returning; a reviewer that never passes gets one final best-effort "
            "result with outstanding issues listed, never a silent false pass. Images are "
            "best-effort resolved via the hevi media gateway (mcp_hevi) when available; "
            "missing images are flagged, not fatal. Writes the final HTML + structured "
            "draft JSON to disk and returns their paths. For themes/layouts/prompt "
            "variants run wechat_discover first."
        ),
        parameters={
            "type": "object",
            "properties": {
                "topic": {"type": "string", "description": "文章主题"},
                "requirements": {
                    "type": "string",
                    "description": "具体要求(受众/字数/风格/必须包含的要点等)",
                },
                "max_iterations": {
                    "type": "integer",
                    "description": "写手↔审核最大轮次, 默认 3, 上限 5",
                },
            },
            "required": ["topic", "requirements"],
        },
        func=produce_wechat_article_tool,
        max_result_chars=4000,
    )
