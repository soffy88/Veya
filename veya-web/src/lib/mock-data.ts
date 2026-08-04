/**
 * veya mock 数据 — USE_MOCK=true 时用,后端三路由待补期间让 UI 可看。
 */
import type { Project, SessionMeta, ChatMessage, FileChange, ModelInfo, Persona, SquadState } from '@/types/api';

export const MOCK_PROJECTS: Project[] = [
  { id: 'p-30',       name: '30',      icon: '◆' },
  { id: 'p-pending',  name: '待入库',  icon: '◷' },
  { id: 'p-helivex',  name: 'helivex', icon: '⬡' },
  { id: 'p-stratum',  name: 'stratum', icon: '▤' },
  { id: 'p-aegis',    name: 'aegis',   icon: '🛡' },
  { id: 'p-mneme',    name: 'mneme',   icon: '✦' },
];

export const MOCK_SESSIONS: SessionMeta[] = [
  { id: 's-1', title: '修复 Caddyfile 反代配置', updated_at: '2026-06-15T09:00:00Z', project: 'aegis' },
  { id: 's-2', title: '重构 Dockerfile 多阶段构建', updated_at: '2026-06-15T08:00:00Z', project: 'aegis' },
  { id: 's-3', title: 'SSE 流式接口联调', updated_at: '2026-06-14T15:00:00Z', project: 'mneme' },
];

export const MOCK_MODELS: ModelInfo[] = [
  { id: 'mimo-v2.5-free', name: 'MiMo V2.5 Free', provider: 'DashScope', free: true },
  { id: 'qwen-max',       name: 'Qwen Max',       provider: 'DashScope' },
  { id: 'deepseek-v3',    name: 'DeepSeek V3',    provider: 'DeepSeek' },
];

export const MOCK_PERSONAS: Persona[] = ['build', 'plan', 'research', 'execute'];

export const MOCK_MESSAGES: ChatMessage[] = [
  { id: 'm-1', role: 'user', parts: [{ type: 'text', text: '帮我检查 tgz 文件是否都存在,然后修复 Caddyfile' }] },
  { id: 'm-2', role: 'assistant', parts: [
    { type: 'text', text: '我先验证 tgz 文件是否存在。' },
    { type: 'tool_call', tool_name: 'Shell', tool_input: { cmd: 'ls -la *.tgz' } },
    { type: 'tool_result', output: 'helios-blocks-2.7.1.tgz\nhelios-oui-1.6.0.tgz', exit_code: 0 },
    { type: 'text', text: '文件都在。现在修复 Caddyfile 的反代配置。' },
    { type: 'diff', path: 'Caddyfile', diff: '@@ -1,3 +1,3 @@\n reverse_proxy {\n-  to localhost:3000\n+  to localhost:8000\n }' },
  ] },
];

export const MOCK_CHANGES: FileChange[] = [
  { path: 'Caddyfile', additions: 6, deletions: 12, diff: '@@ -1,12 +1,6 @@\n-old config line 1\n-old config line 2\n+new config line 1\n reverse_proxy localhost:8000' },
  { path: 'Dockerfile.prod', additions: 6, deletions: 2, diff: '@@ -1,2 +1,6 @@\n FROM node:20-slim\n+WORKDIR /app\n+COPY package.json .\n+RUN npm install' },
  { path: 'scripts/check_health.sh', additions: 3, deletions: 0, diff: '@@ -0,0 +1,3 @@\n+#!/bin/bash\n+curl -f localhost:8000/health\n+echo ok' },
];

export const MOCK_SQUADS: SquadState[] = [
  { squad_id: 'sq-1', role: 'research', status: 'success', current_action: '分析代码库结构', cost: 0.02 },
  { squad_id: 'sq-2', role: 'plan',     status: 'running', current_action: '制定修改方案', cost: 0.01 },
  { squad_id: 'sq-3', role: 'execute',  status: 'running', current_action: '编辑 Caddyfile', cost: 0.03 },
];

// mock 流式:模拟 SSE 逐块吐字
export function mockStream(onEvent: (data: string) => void, onDone: () => void) {
  const events = [
    JSON.stringify({ type: 'session_start', session_id: 's-mock' }),
    JSON.stringify({ type: 'squad_start', squad_id: 'sq-1', role: 'research' }),
    JSON.stringify({ type: 'text_delta', delta: '我来' }),
    JSON.stringify({ type: 'text_delta', delta: '分析这个' }),
    JSON.stringify({ type: 'text_delta', delta: '问题。\n\n' }),
    JSON.stringify({ type: 'squad_step', squad_id: 'sq-1', action: '读取 Caddyfile' }),
    JSON.stringify({ type: 'tool_call', tool_name: 'Shell', tool_input: { cmd: 'cat Caddyfile' } }),
    JSON.stringify({ type: 'tool_result', output: 'reverse_proxy localhost:3000', exit_code: 0 }),
    JSON.stringify({ type: 'squad_done', squad_id: 'sq-1', status: 'success', cost: 0.02 }),
    JSON.stringify({ type: 'text_delta', delta: '找到问题了,端口配置错误。' }),
    JSON.stringify({ type: 'cost_update', cost_usd: 0.06 }),
  ];
  let i = 0;
  const iv = setInterval(() => {
    if (i >= events.length) { clearInterval(iv); onEvent('[DONE]'); onDone(); return; }
    onEvent(events[i]!); i++;
  }, 400);
  return () => clearInterval(iv);
}
