/**
 * veya API client — REST 封装(对接 veya server FastAPI)
 * 文档 §7 接口清单。⚠️ 标注的三个路由后端需补,前端按契约对接。
 */
import type {
  Project, SessionMeta, ChatMessage, FileChange, ModelInfo, Persona,
} from '@/types/api';

export const API_BASE = process.env.NEXT_PUBLIC_API_BASE ?? 'http://localhost:8000';
export const USE_MOCK = (process.env.NEXT_PUBLIC_USE_MOCK ?? 'true').toLowerCase() === 'true';

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers: { 'Content-Type': 'application/json', ...(init?.headers as Record<string, string>) },
  });
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return res.json() as Promise<T>;
}

export const veyaApi = {
  // ✅ P4 已验证
  prompt:      (text: string, sessionId: string, persona: Persona, model?: string, provider?: string) =>
                 req<void>('/prompt', { method: 'POST', body: JSON.stringify({ text, session_id: sessionId, persona, model, provider }) }),
  createSession: (project?: string) => req<{ id: string; project: string }>('/session', { method: 'POST', body: JSON.stringify({ persona: 'build', project: project ?? 'default' }) }),
  getSession:    (id: string) => req<{ messages: ChatMessage[] }>(`/session/${id}`),
  undo:          (id: string) => req<void>(`/session/${id}/undo`, { method: 'POST' }),
  models:        () => req<{ providers: Record<string, string[]> }>('/models'),
  personas:      () => req<{ personas: { name: string, tools: string[], mode: string }[] }>('/agent/personas'),

  // ✅ P6 已补全
  createProject: (name: string) => req<{ project: Project }>('/projects', { method: 'POST', body: JSON.stringify({ name }) }),
  projects:      () => req<{ projects: Project[] }>('/projects'),
  sessions:      (project?: string) => req<{ sessions: SessionMeta[], total: number }>(`/sessions${project ? `?project=${project}` : ''}`),
  changes:       (id: string) => req<{ changes: FileChange[] }>(`/session/${id}/changes`),

  streamUrl:     (id: string) => `${API_BASE}/stream/${id}`,
};
