/**
 * ChatStream — 中栏对话流 + 输入框(P0 核心)
 * 消息按 role 渲染,assistant 流式 parts 打字机出现。
 */
'use client';

import { useState, useEffect, useRef } from 'react';
import type { ChatMessage, Persona, ModelInfo, ReasoningEffort } from '@/types/api';
import { MessagePartView } from './MessagePartView';
import { useHicodeStream } from '@/lib/use-stream';
import { veyaApi, USE_MOCK } from '@/lib/api-client';
import { MOCK_MESSAGES, MOCK_MODELS, MOCK_PERSONAS } from '@/lib/mock-data';

export function ChatStream({ sessionId }: { sessionId: string | null }) {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [input, setInput] = useState('');
  const [persona, setPersona] = useState<Persona>('build');
  const [model, setModel] = useState<ModelInfo>(MOCK_MODELS[0]!);
  const [models, setModels] = useState<ModelInfo[]>(MOCK_MODELS);
  const [personas, setPersonas] = useState<string[]>(MOCK_PERSONAS);
  const [effort, setEffort] = useState<ReasoningEffort>('high');
  const stream = useHicodeStream();
  const scrollRef = useRef<HTMLDivElement>(null);

  // 加载模型与角色
  useEffect(() => {
    if (USE_MOCK) return;
    veyaApi.models().then(r => {
      const all: ModelInfo[] = [];
      Object.entries(r.providers).forEach(([provider, ids]) => {
        ids.forEach(id => all.push({ id, name: id, provider }));
      });
      if (all.length > 0) {
        setModels(all);
        // 优先选 qwen-max 或第一个
        const preferred = all.find(m => m.id === 'qwen-max') || all[0]!;
        setModel(preferred);
      }
    }).catch(() => {});

    veyaApi.personas().then(r => {
      setPersonas(r.personas.map(p => p.name));
    }).catch(() => {});
  }, []);

  // 加载会话历史
  useEffect(() => {
    if (!sessionId) { setMessages([]); return; }
    if (USE_MOCK) { setMessages(MOCK_MESSAGES); return; }
    veyaApi.getSession(sessionId).then(r => setMessages(r.messages)).catch(() => setMessages([]));
  }, [sessionId]);

  // 自动滚到底
  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: 'smooth' });
  }, [messages, stream.liveParts]);

  const send = async () => {
    if (!input.trim() || stream.streaming) return;
    const text = input.trim();
    setInput('');
    setMessages(m => [...m, { id: `u-${Date.now()}`, role: 'user', parts: [{ type: 'text', text }] }]);

    if (USE_MOCK) {
      // mock:直接用 stream hook 的 mock 流(简化:加一条 assistant)
      const { mockStream } = await import('@/lib/mock-data');
      const parts: import('@/types/api').MessagePart[] = [];
      mockStream(
        (data) => {
          if (data === '[DONE]') return;
          try {
            const ev = JSON.parse(data);
            if (ev.type === 'text_delta') {
              const last = parts[parts.length - 1];
              if (last?.type === 'text') last.text += ev.delta;
              else parts.push({ type: 'text', text: ev.delta });
            } else if (ev.type === 'tool_call') parts.push({ type: 'tool_call', tool_name: ev.tool_name, tool_input: ev.tool_input });
            else if (ev.type === 'tool_result') parts.push({ type: 'tool_result', output: ev.output, exit_code: ev.exit_code });
            setMessages(m => {
              const copy = [...m];
              const lastMsg = copy[copy.length - 1];
              if (lastMsg?.role === 'assistant') lastMsg.parts = [...parts];
              else copy.push({ id: `a-${Date.now()}`, role: 'assistant', parts: [...parts] });
              return copy;
            });
          } catch {}
        },
        () => {}
      );
      return;
    }

    await veyaApi.prompt(text, sessionId ?? '', persona, model.id, model.provider);
    stream.start(sessionId ?? '', (finalParts) => {
      setMessages(m => [...m, { id: `a-${Date.now()}`, role: 'assistant', parts: finalParts }]);
    });
  };

  return (
    <div className="hc-chat">
      <div className="hc-chat__scroll" ref={scrollRef}>
        {messages.length === 0 && !stream.streaming && (
          <div className="hc-chat__empty">开始新对话,或从左栏选择会话</div>
        )}
        {messages.map(msg => (
          <div key={msg.id} className="hc-msg" data-role={msg.role}>
            {msg.role === 'user' ? (
              <div className="hc-msg__user">
                {msg.parts.map((p, i) => <MessagePartView key={i} part={p} />)}
              </div>
            ) : (
              <div className="hc-msg__assistant">
                {msg.parts.map((p, i) => <MessagePartView key={i} part={p} />)}
              </div>
            )}
          </div>
        ))}
        {/* 流式中的 live parts */}
        {stream.streaming && stream.liveParts.length > 0 && (
          <div className="hc-msg" data-role="assistant">
            <div className="hc-msg__assistant">
              {stream.liveParts.map((p, i) => <MessagePartView key={i} part={p} />)}
              <span className="hc-cursor" />
            </div>
          </div>
        )}
      </div>

      {/* 输入框 */}
      <div className="hc-input">
        <textarea
          className="hc-input__textarea"
          placeholder="Ask anything, / for commands, @ for context..."
          value={input}
          onChange={e => setInput(e.target.value)}
          onKeyDown={e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); } }}
          rows={2}
        />
        <div className="hc-input__toolbar">
          <select className="hc-input__select" value={persona}
            onChange={e => setPersona(e.target.value as Persona)}>
            {personas.map(p => <option key={p} value={p}>{p}</option>)}
          </select>
          <select className="hc-input__select" value={model.id}
            onChange={e => setModel(models.find(m => m.id === e.target.value)!)}>
            {models.map(m => <option key={m.id} value={m.id}>{m.name}</option>)}
          </select>
          <select className="hc-input__select" value={effort}
            onChange={e => setEffort(e.target.value as ReasoningEffort)}>
            <option value="low">Low</option>
            <option value="medium">Medium</option>
            <option value="high">High</option>
          </select>
          <button className="hc-input__send" onClick={send} disabled={!input.trim() || stream.streaming}>
            {stream.streaming ? '···' : '↑'}
          </button>
        </div>
      </div>
    </div>
  );
}
