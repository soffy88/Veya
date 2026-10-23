"""harness2 proposer 通道：把 Veya 的 provider 配置接到 harness2 Proposer 上。

harness2 的 proposer 通道只有 ``opencode``（bun 直调 opencode CLI）和
``claude_cli``（claude CLI）两种，两者都需要外部 checkout 与凭据。Veya 已经有
一套多 provider 兜底链（``veya.obase.llm``），Phase 1 直接复用它。

解析顺序（第一优先 DeepSeek Flash，与任务约定一致）：

1. ``HARNESS2_IMPROVER_MODEL`` / ``HARNESS2_IMPROVER_PROVIDER`` 显式覆盖；
2. ``opencode-go`` 的 ``deepseek-v4.1-flash``——Veya 主脑池（``veya1.2``）的
   第一档，key 从 opencode 本地凭据解析；
3. Veya 默认 provider（``get_provider_config()``）及其默认模型。

候选是否真的可用由 :func:`probe_channel` 判定：一次最小 ``llm_call``，返回
stub 兜底文案或 ``error=True`` 即视为不可用，换下一个候选。全程**不读取、不
返回、不打印任何 key**。
"""

from __future__ import annotations

import os
from dataclasses import dataclass

#: 显式覆盖用环境变量（与 harness2 的 ``HARNESS2_IMPROVER_MODEL`` 同名同义，
#: 额外提供 provider 维度的 ``HARNESS2_IMPROVER_PROVIDER``）。
MODEL_ENV = "HARNESS2_IMPROVER_MODEL"
PROVIDER_ENV = "HARNESS2_IMPROVER_PROVIDER"

#: DeepSeek Flash：Veya 主脑池第一档（provider opencode-go）。
DEEPSEEK_FLASH_PROVIDER = "opencode-go"
DEEPSEEK_FLASH_MODEL = "deepseek-v4.1-flash"

#: 可用性探测的 max_tokens。DeepSeek Flash 是推理模型，8 token 只会喂饱
#: reasoning 就撞 length 上限、content 为空——那会被误判成"通道不可用"。
PROBE_MAX_TOKENS = 32


@dataclass(frozen=True)
class ProposerChannel:
    """一个候选 proposer 通道，以及这个结论是怎么来的。"""

    provider: str
    model: str
    source: str

    @property
    def label(self) -> str:
        return f"{self.provider}/{self.model}"


def candidate_channels() -> list[ProposerChannel]:
    """按优先级列出候选通道（不含可用性判定）。"""
    out: list[ProposerChannel] = []

    override_model = os.environ.get(MODEL_ENV, "").strip()
    override_provider = os.environ.get(PROVIDER_ENV, "").strip()
    if override_model or override_provider:
        if not override_model:
            raise ValueError(
                f"{PROVIDER_ENV} is set without {MODEL_ENV}; the proposer channel "
                "needs both a provider and a model"
            )
        out.append(
            ProposerChannel(
                provider=override_provider or _veya_default_provider(),
                model=override_model,
                source="env-override",
            )
        )
        return out

    out.append(
        ProposerChannel(
            provider=DEEPSEEK_FLASH_PROVIDER,
            model=DEEPSEEK_FLASH_MODEL,
            source="deepseek-flash",
        )
    )
    default_provider = _veya_default_provider()
    if default_provider and (default_provider, _veya_default_model()) != (
        DEEPSEEK_FLASH_PROVIDER,
        DEEPSEEK_FLASH_MODEL,
    ):
        out.append(
            ProposerChannel(
                provider=default_provider,
                model=_veya_default_model(),
                source="veya-default-provider",
            )
        )
    return out


def _veya_default_provider() -> str:
    try:
        from veya.obase.llm import get_provider_config

        provider, _model = get_provider_config()
        return str(provider or "")
    except Exception:  # pragma: no cover - 依赖环境
        return ""


def _veya_default_model() -> str:
    try:
        from veya.obase.llm import get_provider_config

        _provider, model = get_provider_config()
        return str(model or "")
    except Exception:  # pragma: no cover - 依赖环境
        return ""


def probe_channel(
    channel: ProposerChannel,
    *,
    probe: object | None = None,
) -> tuple[bool, str]:
    """判定一个候选通道是否真的可用：(ok, 人类可读原因)。

    ``probe`` 可注入（测试用）；缺省发一次最小 ``llm_call``。返回 stub 兜底
    文案或 ``error=True`` 都算不可用——那说明缺 key 或上游不可达，而不是
    "模型回答得不好"。
    """
    if probe is None:
        probe = _default_probe
    try:
        reply = probe(channel)  # type: ignore[operator]
    except Exception as exc:
        return False, f"{channel.label} probe raised {type(exc).__name__}: {exc}"
    message = ((reply or {}).get("choices") or [{}])[0].get("message") or {}
    text = str(message.get("content") or "")
    if (reply or {}).get("error") is True:
        return False, f"{channel.label} answered with error=True"
    if "LLM provider not configured" in text:
        return False, f"{channel.label} has no resolvable credential"
    if not text.strip():
        return False, f"{channel.label} returned an empty reply"
    return True, f"{channel.label} answered a minimal probe"


def _default_probe(channel: ProposerChannel) -> dict:
    """一次最小可用性探测：极短 prompt、小 max_tokens（见 PROBE_MAX_TOKENS）。"""
    import asyncio
    import inspect

    from veya.obase.llm import llm_call

    outcome = llm_call(
        [{"role": "user", "content": "Reply with the single word: ok"}],
        provider=channel.provider,
        model=channel.model,
        max_tokens=PROBE_MAX_TOKENS,
    )
    if inspect.isawaitable(outcome):
        return asyncio.run(_settle(outcome))
    return outcome


async def _settle(awaitable):
    return await awaitable


def select_channel(
    *,
    probe: object | None = None,
) -> tuple[ProposerChannel | None, list[str]]:
    """按优先级选第一个可用通道；返回 (channel, 每个候选的判定记录)。

    全部不可用时返回 ``(None, records)``——调用方必须显式报错，而不是静默
    换一个没验证过的模型。
    """
    records: list[str] = []
    for candidate in candidate_channels():
        ok, reason = probe_channel(candidate, probe=probe)
        records.append(reason)
        if ok:
            return candidate, records
    return None, records


__all__ = [
    "DEEPSEEK_FLASH_MODEL",
    "DEEPSEEK_FLASH_PROVIDER",
    "MODEL_ENV",
    "PROVIDER_ENV",
    "ProposerChannel",
    "candidate_channels",
    "probe_channel",
    "select_channel",
]
