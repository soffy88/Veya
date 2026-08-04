/**
 * HicodeShell — 三栏布局主容器(P0)
 * 左:项目+会话 | 中:对话流+多分队 | 右:Git审查
 * 三栏可调宽(拖拽),右栏可折叠。
 */
'use client';

import { useState, useRef, useCallback } from 'react';
import { LeftPanel } from './LeftPanel';
import { ChatStream } from '../chat/ChatStream';
import { GitPanel } from '../git/GitPanel';
import { SquadPanel } from '../squad/SquadPanel';
import { veyaApi, USE_MOCK } from '@/lib/api-client';
import { MOCK_SQUADS } from '@/lib/mock-data';

export function HicodeShell() {
  const [project, setProject] = useState<string | null>('p-aegis');
  const [session, setSession] = useState<string | null>('s-1');
  const [leftW, setLeftW]   = useState(260);
  const [rightW, setRightW] = useState(320);
  const [rightOpen, setRightOpen] = useState(true);
  const [cost, setCost] = useState(0.06);

  // mock 下展示多分队(真实从 SSE squads 来,这里 demo)
  const squads = USE_MOCK ? MOCK_SQUADS : [];

  const drag = useCallback((which: 'left' | 'right') => (e: React.PointerEvent) => {
    e.preventDefault();
    const startX = e.clientX;
    const startL = leftW, startR = rightW;
    const move = (ev: PointerEvent) => {
      if (which === 'left') setLeftW(Math.max(180, Math.min(420, startL + (ev.clientX - startX))));
      else setRightW(Math.max(220, Math.min(520, startR - (ev.clientX - startX))));
    };
    const up = () => { window.removeEventListener('pointermove', move); window.removeEventListener('pointerup', up); };
    window.addEventListener('pointermove', move);
    window.addEventListener('pointerup', up);
  }, [leftW, rightW]);

  const newSession = async () => {
    if (USE_MOCK) { setSession(`s-${Date.now()}`); return; }
    const r = await veyaApi.createSession(project ?? 'default');
    setSession(r.id);
  };

  return (
    <div className="hc-shell">
      {/* 左栏 */}
      <div className="hc-shell__left" style={{ width: leftW }}>
        <LeftPanel
          currentProject={project} currentSession={session}
          onProject={setProject} onSession={setSession} onNewSession={newSession}
        />
      </div>
      <div className="hc-resizer" onPointerDown={drag('left')} />

      {/* 中栏 */}
      <div className="hc-shell__center">
        <div className="hc-statusbar">
          <span className="hc-statusbar__item">persona: build</span>
          <span className="hc-statusbar__item">session: {session ?? '—'}</span>
          <span className="hc-statusbar__item hc-statusbar__cost">${cost.toFixed(3)}</span>
        </div>
        {squads.length > 0 && <SquadPanel squads={squads} />}
        <ChatStream sessionId={session} />
      </div>

      {/* 右栏 Git(可折叠) */}
      {rightOpen ? (
        <>
          <div className="hc-resizer" onPointerDown={drag('right')} />
          <div className="hc-shell__right" style={{ width: rightW }}>
            <button className="hc-collapse-right" onClick={() => setRightOpen(false)} title="折叠">⟩</button>
            <GitPanel sessionId={session} />
          </div>
        </>
      ) : (
        <button className="hc-expand-right" onClick={() => setRightOpen(true)} title="展开 Git">⟨ Git</button>
      )}
    </div>
  );
}
