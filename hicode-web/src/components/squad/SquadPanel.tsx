/**
 * SquadPanel — 多分队可视化(P2,hicode 招牌)
 *
 * 文档 §5:协调器派多分队并行,前端要让它"看得见"。
 * 每分队一卡片:角色 + 状态 + 当前动作 + cost。并行同时更新。
 */
'use client';

import type { SquadState } from '@/types/api';

const STATUS_META: Record<SquadState['status'], { color: string; label: string; icon: string }> = {
  running: { color: 'var(--primary)',                       label: '运行中', icon: '●' },
  success: { color: 'var(--success, oklch(0.62 0.18 145))', label: '完成',   icon: '✓' },
  failed:  { color: 'var(--destructive)',                   label: '失败',   icon: '✕' },
};

const ROLE_LABEL: Record<string, string> = {
  research: '研究', plan: '规划', execute: '执行', build: '构建',
};

export function SquadPanel({ squads }: { squads: SquadState[] }) {
  if (squads.length === 0) return null;

  const totalCost = squads.reduce((s, q) => s + (q.cost ?? 0), 0);

  return (
    <div className="hc-squads">
      <div className="hc-squads__head">
        <span className="hc-squads__title">多分队并行</span>
        <span className="hc-squads__cost">${totalCost.toFixed(3)}</span>
      </div>
      <div className="hc-squads__grid">
        {squads.map(q => {
          const m = STATUS_META[q.status];
          return (
            <div key={q.squad_id} className="hc-squad" data-status={q.status}>
              <div className="hc-squad__top">
                <span className="hc-squad__icon" style={{ color: m.color }}
                  data-spin={q.status === 'running' ? 'true' : undefined}>{m.icon}</span>
                <span className="hc-squad__role">{ROLE_LABEL[q.role] ?? q.role}</span>
                <span className="hc-squad__status" style={{ color: m.color }}>{m.label}</span>
              </div>
              {q.current_action && (
                <div className="hc-squad__action">{q.current_action}</div>
              )}
              {q.cost != null && (
                <div className="hc-squad__cost">${q.cost.toFixed(3)}</div>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}
