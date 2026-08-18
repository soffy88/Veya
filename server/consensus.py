"""server.consensus — 高歧义/高影响任务的 best-of-N 共识 (装配 3O oskill.fan_out_and_synthesize)。

3O 单一来源: 编排机制 (扇出→比对→合并) 在 oskill.fan_out_and_synthesize (纯、可测);
本层只注入 veya.llm.llm_call —— 用不同 temperature 制造 N 个候选, 再由一次 leader
调用综合成最终答案 (Grok leader-synthesizer / orca 扇出-合并范式)。

**selective by design**: 这是按需能力, 不进主链路自动触发。调用方 (如对高歧义意图
判定、关键路由决策) 显式调用才多花算力; 常规请求仍走单次通过, 成本不变。

shared_prefix (opt-in, Orchard prefix-sharing 内化): 先一次生成"共享推理骨架", N 条分支
从同一前缀继续, 只在决定性分叉处发散 —— 分支不再各自重推公共前提, 多样性聚焦在真正的
分歧点。多花一次骨架调用, 换更聚焦的扇出。编排仍委托 oskill.fan_out_and_synthesize。
"""

from __future__ import annotations

from typing import Any

# 候选生成的温度梯度: 低→高, 制造多样性 (超出 N 则循环取模)。
_TEMPS = (0.2, 0.7, 1.0, 0.4, 0.9)


async def consensus_answer(
    task: str,
    *,
    n: int = 3,
    model: str | None = None,
    provider: str | None = None,
    config: dict | None = None,
    endpoint: str | None = None,
    synthesize: bool = True,
    shared_prefix: bool = False,
    max_tokens: int = 2048,
    system_prompt: str | None = None,
    _llm: Any | None = None,
) -> dict[str, Any]:
    """对同一 task 扇出 n 个候选并收敛为一个答案。

    synthesize=True → leader 综合 n 个候选 (更稳); False → 多数票/首个 (省一次调用)。
    shared_prefix=True → 先生成共享推理骨架, N 条分支从同一前缀继续 (多花一次骨架调用)。
    返回 oskill.fan_out_and_synthesize 的结果 dict (chosen/candidates/method/errors)。
    _llm 供测试注入 (async (messages, **kw) -> openai-format dict); 缺省用 veya.llm.llm_call。
    """
    from veya.platform import oskill as _load_oskill

    fan_out_and_synthesize = _load_oskill().fan_out_and_synthesize

    if _llm is not None:
        llm = _llm
    else:
        from veya.llm import llm_call as llm

    base_msgs = []
    if system_prompt:
        base_msgs.append({"role": "system", "content": system_prompt})

    def _content(resp: dict) -> str:
        return ((resp.get("choices") or [{}])[0].get("message") or {}).get("content") or ""

    async def _stem(task_text: str) -> str:
        """共享推理骨架: 所有可行解都应遵守的前提/约束/推理主线 (不含最终答案)。"""
        prompt = (
            "先给出解决该任务的**共享推理骨架**: 所有可行解都必须遵守的前提、约束与推理主线。"
            "不要给出完整最终答案, 只写各分支应当共享的那部分:\n\n"
            f"# 任务\n{task_text}"
        )
        resp = await llm(
            [*base_msgs, {"role": "user", "content": prompt}],
            model=model,
            provider=provider,
            config=config,
            endpoint=endpoint,
            temperature=0.2,
            max_tokens=max_tokens,
        )
        return _content(resp).strip()

    # 骨架失败不该拖垮整次共识: 回落到无前缀 (与 fan-out 的逐候选容错同语义)。
    if shared_prefix:
        try:
            stem = await _stem(task)
        except Exception:
            stem = ""
    else:
        stem = ""

    async def _generate(task_text: str, i: int) -> str:
        msgs = [*base_msgs, {"role": "user", "content": task_text}]
        if stem:
            # 共享前缀: N 条分支从同一骨架继续, 只在决定性分叉处发散
            msgs.append({"role": "assistant", "content": stem})
            msgs.append({"role": "user", "content": "基于以上共享骨架, 给出完整最终答案。"})
        resp = await llm(
            msgs,
            model=model,
            provider=provider,
            config=config,
            endpoint=endpoint,
            temperature=_TEMPS[i % len(_TEMPS)],
            max_tokens=max_tokens,
        )
        text = _content(resp).strip()
        if not text:
            raise ValueError("empty candidate")
        return text

    async def _synthesize(task_text: str, candidates: list[str]) -> str:
        numbered = "\n\n".join(f"[候选{i + 1}]\n{c}" for i, c in enumerate(candidates))
        prompt = (
            "以下是对同一任务的多个独立回答。综合它们的正确部分、剔除分歧中的错误, "
            "给出一个最终、准确、完整的答案 (不要提及'候选'或综合过程):\n\n"
            f"# 任务\n{task_text}\n\n# 独立回答\n{numbered}"
        )
        resp = await llm(
            [*base_msgs, {"role": "user", "content": prompt}],
            model=model,
            provider=provider,
            config=config,
            endpoint=endpoint,
            temperature=0.2,
            max_tokens=max_tokens,
        )
        return _content(resp).strip() or candidates[0]

    return await fan_out_and_synthesize(
        task,
        generate=_generate,
        n=n,
        synthesize=_synthesize if synthesize else None,
    )
