/**
 * LeftPanel — 项目列表 + 会话列表(P1/P3)
 */
'use client';

import { useEffect, useState, useCallback } from 'react';
import type { Project, SessionMeta } from '@/types/api';
import { hicodeApi, USE_MOCK } from '@/lib/api-client';
import { MOCK_PROJECTS, MOCK_SESSIONS } from '@/lib/mock-data';

function groupByDay(sessions: SessionMeta[]) {
  const today = new Date().toDateString();
  const yesterday = new Date(Date.now() - 864e5).toDateString();
  const groups: Record<string, SessionMeta[]> = { 今天: [], 昨天: [], 更早: [] };
  for (const s of sessions) {
    const d = new Date(s.updated_at).toDateString();
    if (d === today) groups['今天']!.push(s);
    else if (d === yesterday) groups['昨天']!.push(s);
    else groups['更早']!.push(s);
  }
  return groups;
}

export function LeftPanel({
  currentProject, currentSession, onProject, onSession, onNewSession,
}: {
  currentProject: string | null;
  currentSession: string | null;
  onProject: (id: string) => void;
  onSession: (id: string) => void;
  onNewSession: () => Promise<void>;
}) {
  const [projects, setProjects] = useState<Project[]>([]);
  const [sessions, setSessions] = useState<SessionMeta[]>([]);
  const [search, setSearch] = useState('');

  const loadProjects = useCallback(() => {
    if (USE_MOCK) { setProjects(MOCK_PROJECTS); return; }
    hicodeApi.projects().then(r => setProjects(r.projects ?? [])).catch(() => setProjects(MOCK_PROJECTS));
  }, []);

  const loadSessions = useCallback(() => {
    if (USE_MOCK) { setSessions(MOCK_SESSIONS); return; }
    hicodeApi.sessions(currentProject ?? undefined).then(r => setSessions(r.sessions ?? [])).catch(() => setSessions([]));
  }, [currentProject]);

  useEffect(() => { loadProjects(); }, [loadProjects]);
  useEffect(() => { loadSessions(); }, [loadSessions]);

  const handleNewProject = async () => {
    if (USE_MOCK) { return; }
    const name = prompt('项目名称:');
    if (!name?.trim()) return;
    try {
      await hicodeApi.createProject(name.trim());
      loadProjects();
    } catch (e) {
      console.error('创建项目失败', e);
    }
  };

  const handleNewSession = async () => {
    await onNewSession();
    loadSessions();
  };

  const filtered = sessions.filter(s => !search || s.title.includes(search));
  const groups = groupByDay(filtered);

  return (
    <div className="hc-left">
      {/* 项目区 */}
      <div className="hc-left__section">
        <div className="hc-left__head">
          <span className="hc-left__title">项目</span>
          <button className="hc-left__add" onClick={handleNewProject} title="新建项目">+</button>
        </div>
        <div className="hc-project-list">
          {projects.map(p => (
            <button key={p.id} className="hc-project-item"
              data-active={currentProject === p.id ? 'true' : undefined}
              onClick={() => onProject(p.id)}>
              <span className="hc-project-icon">{p.icon}</span>
              <span className="hc-project-name">{p.name}</span>
            </button>
          ))}
        </div>
      </div>

      {/* 会话区 */}
      <div className="hc-left__section hc-left__section--grow">
        <div className="hc-left__head">
          <span className="hc-left__title">会话</span>
          <button className="hc-left__add" onClick={handleNewSession} title="新建会话">+</button>
        </div>
        <input className="hc-left__search" placeholder="搜索会话"
          value={search} onChange={e => setSearch(e.target.value)} />
        <div className="hc-session-list">
          {Object.entries(groups).map(([day, items]) =>
            items.length === 0 ? null : (
              <div key={day} className="hc-session-group">
                <div className="hc-session-day">{day}</div>
                {items.map(s => (
                  <button key={s.id} className="hc-session-item"
                    data-active={currentSession === s.id ? 'true' : undefined}
                    onClick={() => onSession(s.id)}>
                    <span className="hc-session-title">{s.title}</span>
                    {s.project && <span className="hc-session-project">{s.project}</span>}
                  </button>
                ))}
              </div>
            )
          )}
        </div>
      </div>
    </div>
  );
}
