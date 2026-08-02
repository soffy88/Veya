/**
 * GitPanel — 右栏 Git 审查(P1)
 * 文件改动列表 + 点开看 diff,统一/拆分视图模式。
 */
'use client';

import { useEffect, useState } from 'react';
import type { FileChange } from '@/types/api';
import { hicodeApi, USE_MOCK } from '@/lib/api-client';
import { MOCK_CHANGES } from '@/lib/mock-data';
import { DiffView } from '../chat/MessagePartView';

type ViewMode = 'unified' | 'split';

export function GitPanel({ sessionId }: { sessionId: string | null }) {
  const [changes, setChanges] = useState<FileChange[]>([]);
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [mode, setMode] = useState<ViewMode>('unified');

  useEffect(() => {
    if (!sessionId) { setChanges([]); return; }
    if (USE_MOCK) { setChanges(MOCK_CHANGES); return; }
    hicodeApi.changes(sessionId).then(r => setChanges(r.changes ?? [])).catch(() => setChanges([]));
  }, [sessionId]);

  const toggle = (path: string) =>
    setExpanded(s => { const n = new Set(s); n.has(path) ? n.delete(path) : n.add(path); return n; });

  const expandAll = () => setExpanded(new Set(changes.map(c => c.path)));

  const fileIcon = (path: string) => {
    if (path.endsWith('.sh')) return '⌘';
    if (path.includes('Dockerfile')) return '⬡';
    if (path.includes('Caddyfile')) return '◆';
    return '◌';
  };

  if (changes.length === 0) {
    return (
      <div className="hc-git hc-git--empty">
        <div className="hc-git__title">Git changes</div>
        <div className="hc-git__empty-text">本会话暂无文件改动</div>
      </div>
    );
  }

  return (
    <div className="hc-git">
      <div className="hc-git__head">
        <span className="hc-git__title">Git changes</span>
        <span className="hc-git__count">{changes.length} 个文件</span>
      </div>

      <div className="hc-git__toolbar">
        <button data-active={mode === 'unified' ? 'true' : undefined} onClick={() => setMode('unified')}>统一</button>
        <button data-active={mode === 'split' ? 'true' : undefined} onClick={() => setMode('split')}>拆分</button>
        <button onClick={expandAll}>全部展开</button>
      </div>

      <div className="hc-git__list">
        {changes.map(c => (
          <div key={c.path} className="hc-change">
            <button className="hc-change__head" onClick={() => toggle(c.path)}>
              <span className="hc-change__icon">{fileIcon(c.path)}</span>
              <span className="hc-change__path">{c.path}</span>
              <span className="hc-change__stat">
                <span className="hc-change__add">+{c.additions}</span>
                <span className="hc-change__del">-{c.deletions}</span>
              </span>
            </button>
            {expanded.has(c.path) && (
              <div className="hc-change__diff" data-mode={mode}>
                <DiffView diff={c.diff} />
              </div>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}
