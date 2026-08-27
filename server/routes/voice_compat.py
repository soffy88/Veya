"""server.routes.voice_compat — 语音 WebSocket 兼容端点。

背景: veya L4 gateway (veya/server/app.py) 原生提供 /api/v1/voice/ws, 但线上
Agent OS 主 app (server/app.py) 的 Caddy 反代把 /api/v1/* 打到本 app ——
根 app 缺这条路由 → 语音通话在生产环境连不上 (docs/ops/ONLINE_DEPLOYMENT.md §3)。

本模块照 cindy_compat 兼容模式, 把该端点以自包含 router 挂到根 app,
使两条入口 (L4 gateway 与 Agent OS 主 app) 具备一致的能力面。
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

router = APIRouter(tags=["voice-compat"])


# =========================================================================
# WebSocket /api/v1/voice/ws  (连续可打断的实时语音对话, 与 veya/server/app.py 保持一致)
# =========================================================================


@router.websocket("/api/v1/voice/ws")
async def voice_ws(websocket: WebSocket, session_id: str | None = None) -> None:
    """双工实时语音: 浏览器发 16kHz/mono/16-bit PCM 二进制帧, 收到的二进制帧
    是同格式的 TTS 音频块, 之间穿插 JSON 控制消息 (state/transcript)。持续监听
    + 可打断由 VoiceAgent.run_streaming() 负责 (veya/omodul/voice_agent.py);
    llm_handler 直接调 Master Brain 的内部入口 (跟 /api/v1/agent/stream 同一条
    chat_stream, 复用同一个 session_id → 语音这轮跟文字聊天记录连续,
    工具调用/记忆都在)。"""
    await websocket.accept()

    from server.coordinator_master import master_coordinator
    from veya.omodul.voice_agent import VoiceAgent, VoiceSessionConfig
    from veya.server.manifests import new_session_id

    sid = session_id or new_session_id()
    agent = VoiceAgent(VoiceSessionConfig())
    voice_tasks: set[asyncio.Task[None]] = set()

    def send_json_fire_and_forget(payload: dict[str, Any]) -> None:
        async def _send() -> None:
            with contextlib.suppress(Exception):
                await websocket.send_json(payload)

        task = asyncio.create_task(_send())
        voice_tasks.add(task)
        task.add_done_callback(voice_tasks.discard)

    async def llm_handler(messages: list[dict]) -> dict:
        result = await master_coordinator.chat_stream(messages[-1]["content"], session_id=sid)
        return {"content": result.get("final_answer", "")}

    agent.llm_handler = llm_handler

    def on_state_change(state: Any, extra: dict) -> None:
        payload: dict[str, Any] = {"type": "state", "state": state.value, **extra}
        if state.value == "speaking":
            payload["sample_rate"] = agent.config.tts_sample_rate
        send_json_fire_and_forget(payload)

    agent.on_state_change = on_state_change
    agent.on_transcript = lambda text, final: send_json_fire_and_forget(
        {"type": "transcript", "text": text, "final": final}
    )

    async def audio_source():
        while True:
            chunk = await websocket.receive_bytes()
            yield chunk

    async def on_audio_chunk(chunk: bytes) -> None:
        await websocket.send_bytes(chunk)

    try:
        await agent.run_streaming(audio_source(), on_audio_chunk=on_audio_chunk)
    except WebSocketDisconnect:
        pass
    finally:
        with contextlib.suppress(Exception):
            await websocket.close()


__all__ = ["router"]
