/**
 * hicode 后端 API 契约 + SSE 事件协议
 * 来源:hicode Web 前端需求文档。schema 以后端实际为准。
 */

// ── 项目 / 会话 ──────────────────────────────────
export interface Project {
  id: string;
  name: string;
  icon?: string;
}

export interface SessionMeta {
  id: string;
  title: string;
  updated_at: string;
  project?: string;
}

// ── 消息 parts(后端返回结构,前端不自拼)──────────
export type MessagePartType =
  | 'text' | 'reasoning' | 'tool_call' | 'tool_result' | 'diff';

export interface MessagePart {
  type: MessagePartType;
  // text / reasoning
  text?: string;
  // tool_call
  tool_name?: string;
  tool_input?: unknown;
  // tool_result
  output?: string;
  stderr?: string;
  exit_code?: number;
  // diff
  path?: string;
  diff?: string;
}

export interface ChatMessage {
  id: string;
  role: 'user' | 'assistant' | 'system';
  parts: MessagePart[];
}

// ── SSE 事件(前端按 type 路由)──────────────────
export type SSEEvent =
  | { type: 'session_start'; session_id: string }
  | { type: 'text_delta'; delta: string }
  | { type: 'tool_call'; tool_name: string; tool_input?: unknown; call_id?: string }
  | { type: 'tool_result'; call_id?: string; output?: string; stderr?: string; exit_code?: number }
  | { type: 'squad_start'; squad_id: string; role: string }
  | { type: 'squad_step'; squad_id: string; action: string }
  | { type: 'squad_done'; squad_id: string; status: 'success' | 'failed'; cost?: number }
  | { type: 'cost_update'; cost_usd: number }
  | { type: 'done' };

// ── Git 改动 ─────────────────────────────────────
export interface FileChange {
  path: string;
  additions: number;
  deletions: number;
  diff: string;
}

// ── persona / 模型 ───────────────────────────────
export type Persona = 'build' | 'plan' | 'research' | 'execute';

export interface ModelInfo {
  id: string;
  name: string;
  provider?: string;
  free?: boolean;
}

export type ReasoningEffort = 'low' | 'medium' | 'high';

// ── 多分队 ───────────────────────────────────────
export interface SquadState {
  squad_id: string;
  role: string;
  status: 'running' | 'success' | 'failed';
  current_action?: string;
  cost?: number;
}
