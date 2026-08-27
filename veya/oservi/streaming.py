"""
流式输出引擎 - P0 核心能力
功能：实时 token 输出、进度指示、中断支持
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncGenerator, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class StreamEventType(StrEnum):
    """流式事件类型"""

    START = "start"  # 开始
    TOKEN = "token"  # 新 token
    THOUGHT = "thought"  # 思考过程
    ACTION = "action"  # 执行动作
    RESULT = "result"  # 动作结果
    PROGRESS = "progress"  # 进度更新
    ERROR = "error"  # 错误
    COMPLETE = "complete"  # 完成
    INTERRUPTED = "interrupted"  # 中断


class StreamStatus(StrEnum):
    """流状态"""

    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    INTERRUPTED = "interrupted"


@dataclass
class StreamEvent:
    """流式事件对象"""

    type: StreamEventType
    data: dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {"type": self.type.value, "data": self.data, "timestamp": self.timestamp}


class StreamingManager:
    """
    流式输出管理器

    功能：
    1. 实时逐 token 输出
    2. 进度跟踪与更新
    3. 支持中断和暂停
    4. 多客户端订阅
    5. 事件过滤
    """

    def __init__(self, stream_id: str | None = None):
        self.stream_id = stream_id or f"stream_{int(time.time() * 1000)}"
        self.status = StreamStatus.RUNNING
        self.start_time = time.time()
        self.last_event_time = self.start_time
        self.event_count = 0

        # 事件队列（用于 SSE）
        self._event_queue: asyncio.Queue[StreamEvent] = asyncio.Queue()

        # 订阅者（用于多客户端）
        self._subscribers: list[Callable[[StreamEvent], None]] = []

        # 中断事件
        self._interrupt_event = asyncio.Event()

        # 缓存的事件（用于重播）
        self._event_history: list[StreamEvent] = []
        self._max_history_size = 1000

    async def emit(self, event_type: StreamEventType, data: dict[str, Any] | None = None):
        """发出事件"""
        if self.status in [StreamStatus.FAILED, StreamStatus.COMPLETED, StreamStatus.INTERRUPTED]:
            return

        event = StreamEvent(type=event_type, data=data or {})

        # 添加到队列
        await self._event_queue.put(event)

        # 通知订阅者
        for subscriber in self._subscribers:
            try:
                subscriber(event)
            except Exception as e:
                print(f"[Streaming] Subscriber error: {e}")

        # 添加到历史记录
        self._event_history.append(event)
        if len(self._event_history) > self._max_history_size:
            self._event_history.pop(0)

        self.event_count += 1
        self.last_event_time = event.timestamp

        # 特殊处理：完成或中断事件
        if event_type == StreamEventType.COMPLETE:
            self.status = StreamStatus.COMPLETED
        elif event_type == StreamEventType.ERROR:
            self.status = StreamStatus.FAILED

    async def get_events(self) -> AsyncGenerator[StreamEvent, None]:
        """获取事件流（用于 SSE）"""
        while True:
            try:
                # 等待新事件
                event = await asyncio.wait_for(
                    self._event_queue.get(),
                    timeout=30.0,  # 30秒超时，避免长连接问题
                )
                yield event

                # 如果是完成或错误事件，停止
                if event.type in [
                    StreamEventType.COMPLETE,
                    StreamEventType.ERROR,
                    StreamEventType.INTERRUPTED,
                ]:
                    break

            except TimeoutError:
                # 发送心跳
                yield StreamEvent(type=StreamEventType.TOKEN, data={"text": ""})
                continue
            except Exception as e:
                await self.emit(StreamEventType.ERROR, {"message": str(e)})
                break

    async def interrupt(self):
        """中断流"""
        if self.status == StreamStatus.RUNNING:
            await self.emit(StreamEventType.INTERRUPTED, {"reason": "user_request"})
            self.status = StreamStatus.INTERRUPTED
            self._interrupt_event.set()

    def is_interrupted(self) -> bool:
        """检查是否被中断"""
        return self._interrupt_event.is_set()

    async def wait_for_completion(self, timeout: float | None = None) -> bool:
        """等待流完成"""
        try:
            while self.status == StreamStatus.RUNNING:
                if timeout:
                    await asyncio.sleep(0.1)
                    if time.time() - self.start_time > timeout:
                        await self.interrupt()
                        return False
                else:
                    await asyncio.sleep(0.1)
            return True
        except Exception:
            return False

    def subscribe(self, callback: Callable[[StreamEvent], None]):
        """订阅事件"""
        self._subscribers.append(callback)

    def unsubscribe(self, callback: Callable[[StreamEvent], None]):
        """取消订阅"""
        if callback in self._subscribers:
            self._subscribers.remove(callback)

    def get_stats(self) -> dict[str, Any]:
        """获取流统计信息"""
        duration = time.time() - self.start_time
        events_per_second = self.event_count / duration if duration > 0 else 0

        return {
            "stream_id": self.stream_id,
            "status": self.status.value,
            "duration": round(duration, 2),
            "event_count": self.event_count,
            "events_per_second": round(events_per_second, 2),
            "start_time": self.start_time,
            "last_event_time": self.last_event_time,
        }

    def get_history(self) -> list[dict[str, Any]]:
        """获取事件历史（用于调试）"""
        return [event.to_dict() for event in self._event_history]


class TokenStreamer:
    """
    Token 流式生成器

    功能：
    1. 模拟 LLM token 生成
    2. 支持思考过程和动作
    3. 可中断
    """

    def __init__(self, manager: StreamingManager):
        self.manager = manager

    async def stream_response(self, response_text: str, user_query: str):
        """流式生成响应"""
        # 发送开始事件
        await self.manager.emit(
            StreamEventType.START, {"query": user_query, "timestamp": time.time()}
        )

        # 模拟思考过程
        thoughts = ["分析用户需求...", "规划解决方案...", "确定执行步骤...", "准备生成响应..."]

        for thought in thoughts:
            await self.manager.emit(StreamEventType.THOUGHT, {"text": thought})
            await asyncio.sleep(0.5)

            # 检查是否被中断
            if self.manager.is_interrupted():
                return

        # 流式发送 token
        words = response_text.split(" ")
        buffer = ""

        for i, word in enumerate(words):
            buffer += (" " + word) if buffer else word

            # 每 3 个词发送一次
            if i % 3 == 2 or i == len(words) - 1:
                await self.manager.emit(StreamEventType.TOKEN, {"text": buffer})
                buffer = ""
                await asyncio.sleep(0.1)  # 模拟网络延迟

                # 检查是否被中断
                if self.manager.is_interrupted():
                    return

        # 完成
        await self.manager.emit(
            StreamEventType.COMPLETE, {"final_text": response_text, "word_count": len(words)}
        )


class VoiceStreamManager:
    """Voice streaming manager — combines audio input, transcription, LLM,
    and TTS output into a unified streaming pipeline.

    Builds on top of StreamingManager to add audio-specific event types:
    - ``audio_chunk``: raw audio byte chunks (input or output)
    - ``transcript_partial``: interim STT results
    - ``transcript_final``: final STT result
    - ``tts_chunk``: TTS audio output chunk
    - ``turn_start`` / ``turn_end``: conversation turn boundaries

    Usage::

        vstream = VoiceStreamManager()
        await vstream.start()
        await vstream.push_audio_input(pcm_chunk)
        # events flow: audio_chunk → transcript_partial → transcript_final →
        #              thinking → tts_chunk → turn_end
    """

    def __init__(self, stream_id: str | None = None):
        self.manager = StreamingManager(stream_id)

        # Voice-specific state
        self.audio_buffer: list[bytes] = []
        self.current_transcript = ""
        self.tts_buffer: list[bytes] = []
        self.is_user_speaking = False
        self.is_agent_speaking = False

    async def start(self):
        """Start the voice stream."""
        await self.manager.emit(StreamEventType.START, {"mode": "voice"})

    async def stop(self):
        """Stop the voice stream."""
        await self.manager.emit(StreamEventType.COMPLETE, {"mode": "voice"})

    async def push_audio_input(self, chunk: bytes, timestamp_ms: float = 0.0):
        """Push an audio input chunk (raw PCM bytes)."""
        self.audio_buffer.append(chunk)
        await self.manager.emit(
            StreamEventType.TOKEN,
            {"type": "audio_chunk", "size": len(chunk), "timestamp_ms": timestamp_ms},
        )

    async def push_transcript_partial(self, text: str):
        """Push a partial (interim) transcription result."""
        self.current_transcript = text
        await self.manager.emit(
            StreamEventType.TOKEN,
            {"type": "transcript_partial", "text": text},
        )

    async def push_transcript_final(self, text: str):
        """Push a final transcription result."""
        self.current_transcript = text
        await self.manager.emit(
            StreamEventType.PROGRESS,
            {"type": "transcript_final", "text": text},
        )

    async def push_agent_thinking(self):
        """Signal that the agent is thinking."""
        await self.manager.emit(
            StreamEventType.THOUGHT,
            {"type": "thinking", "text": "Agent is processing..."},
        )

    async def push_tts_chunk(self, chunk: bytes):
        """Push a TTS audio output chunk."""
        self.tts_buffer.append(chunk)
        await self.manager.emit(
            StreamEventType.TOKEN,
            {"type": "tts_chunk", "size": len(chunk)},
        )

    async def push_turn_end(self):
        """Signal the end of a conversation turn."""
        await self.manager.emit(StreamEventType.PROGRESS, {"type": "turn_end"})

    async def push_error(self, error: str):
        """Push an error event."""
        await self.manager.emit(StreamEventType.ERROR, {"type": "error", "message": error})

    def clear_audio_buffer(self) -> bytes:
        """Clear and return the accumulated audio input buffer."""
        data = b"".join(self.audio_buffer)
        self.audio_buffer.clear()
        return data

    def clear_tts_buffer(self) -> bytes:
        """Clear and return the accumulated TTS output buffer."""
        data = b"".join(self.tts_buffer)
        self.tts_buffer.clear()
        return data

    async def get_events(self) -> AsyncGenerator[StreamEvent, None]:
        """Get the event stream (for SSE)."""
        async for event in self.manager.get_events():
            yield event

    async def interrupt(self):
        """Interrupt the voice stream."""
        await self.manager.interrupt()

    def get_stats(self) -> dict[str, Any]:
        """Get voice stream statistics."""
        base_stats = self.manager.get_stats()
        base_stats.update(
            {
                "audio_chunks": len(self.audio_buffer),
                "audio_bytes": sum(len(c) for c in self.audio_buffer),
                "tts_chunks": len(self.tts_buffer),
                "tts_bytes": sum(len(c) for c in self.tts_buffer),
                "current_transcript": self.current_transcript[:100],
            }
        )
        return base_stats


# 便捷函数
def create_stream_manager(stream_id: str | None = None) -> StreamingManager:
    """创建流管理器"""
    return StreamingManager(stream_id)


def create_voice_stream_manager(stream_id: str | None = None) -> VoiceStreamManager:
    """创建语音流管理器"""
    return VoiceStreamManager(stream_id)


if __name__ == "__main__":
    # 测试
    async def test_streaming():
        manager = create_stream_manager()
        streamer = TokenStreamer(manager)

        # 启动任务
        task = asyncio.create_task(
            streamer.stream_response(
                "这是一个测试响应，用于验证流式输出功能。我们正在检查实时输出、进度指示和中断支持。",
                "测试流式输出",
            )
        )

        # 监听事件
        async for event in manager.get_events():
            print(f"[{event.type}] {json.dumps(event.data, ensure_ascii=False)}")

            # 模拟中断
            if (
                event.type == StreamEventType.THOUGHT
                and event.data.get("text") == "准备生成响应..."
            ):
                print("\n--- 请求中断 ---\n")
                await manager.interrupt()
                break

        # 等待任务完成
        await task

        # 显示统计
        print(f"\n流统计: {json.dumps(manager.get_stats(), indent=2)}")

    # 运行测试
    asyncio.run(test_streaming())
