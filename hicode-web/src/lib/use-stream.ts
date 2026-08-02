/**
 * useHicodeStream — 订阅 hicode SSE 流,按事件 type 分发。
 *
 * 文档 §3.2:assistant 响应逐块出现(打字机),经 SSE 订阅 on_step。
 * 事件:session_start, text_delta, tool_call, tool_result, squad_*, cost_update, DONE
 */
'use client';

import { useCallback, useRef, useState } from 'react';
import type { SSEEvent, MessagePart, SquadState } from '@/types/api';
import { API_BASE } from './api-client';

export interface StreamState {
  streaming: boolean;
  /** 当前正在流式生成的 assistant parts */
  liveParts: MessagePart[];
  squads: SquadState[];
  cost: number;
}

const EMPTY: StreamState = { streaming: false, liveParts: [], squads: [], cost: 0 };

export function useHicodeStream() {
  const [state, setState] = useState<StreamState>(EMPTY);
  const esRef = useRef<EventSource | null>(null);

  const stop = useCallback(() => {
    esRef.current?.close();
    esRef.current = null;
    setState(s => ({ ...s, streaming: false }));
  }, []);

  // 订阅某 session 的流;onComplete 收到最终 parts
  const start = useCallback((
    sessionId: string,
    onComplete?: (parts: MessagePart[]) => void
  ) => {
    esRef.current?.close();
    setState({ ...EMPTY, streaming: true });

    const es = new EventSource(`${API_BASE}/stream/${sessionId}`, { withCredentials: true });
    esRef.current = es;
    const parts: MessagePart[] = [];
    const squadMap = new Map<string, SquadState>();

    const pushText = (delta: string) => {
      const last = parts[parts.length - 1];
      if (last && last.type === 'text') last.text = (last.text ?? '') + delta;
      else parts.push({ type: 'text', text: delta });
    };

    es.onmessage = (e) => {
      if (e.data === '[DONE]') {
        es.close(); esRef.current = null;
        setState(s => ({ ...s, streaming: false }));
        onComplete?.(parts);
        return;
      }
      let ev: SSEEvent;
      try { ev = JSON.parse(e.data) as SSEEvent; } catch { return; }

      switch (ev.type) {
        case 'text_delta':
          pushText(ev.delta);
          break;
        case 'tool_call':
          parts.push({ type: 'tool_call', tool_name: ev.tool_name, tool_input: ev.tool_input });
          break;
        case 'tool_result':
          parts.push({ type: 'tool_result', output: ev.output, stderr: ev.stderr, exit_code: ev.exit_code });
          break;
        case 'squad_start':
          squadMap.set(ev.squad_id, { squad_id: ev.squad_id, role: ev.role, status: 'running' });
          break;
        case 'squad_step': {
          const s = squadMap.get(ev.squad_id);
          if (s) s.current_action = ev.action;
          break;
        }
        case 'squad_done': {
          const s = squadMap.get(ev.squad_id);
          if (s) { s.status = ev.status; s.cost = ev.cost; }
          break;
        }
        case 'cost_update':
          setState(st => ({ ...st, cost: ev.cost_usd }));
          break;
      }
      setState(st => ({
        ...st,
        liveParts: [...parts],
        squads: [...squadMap.values()],
      }));
    };

    es.onerror = () => {
      es.close(); esRef.current = null;
      setState(s => ({ ...s, streaming: false }));
    };
  }, []);

  return { ...state, start, stop };
}
