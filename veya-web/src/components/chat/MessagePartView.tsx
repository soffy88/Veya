/**
 * MessagePartView — 按 part type 分发渲染
 * text(markdown)/ tool_call / tool_result(可折叠)/ diff / reasoning
 */
'use client';

import { useState } from 'react';
import type { MessagePart } from '@/types/api';

export function MessagePartView({ part }: { part: MessagePart }) {
  const [collapsed, setCollapsed] = useState(part.type === 'tool_result' || part.type === 'reasoning');

  switch (part.type) {
    case 'text':
      return <div className="hc-part-text">{renderMarkdown(part.text ?? '')}</div>;

    case 'reasoning':
      return (
        <div className="hc-part-reasoning">
          <button className="hc-collapse-btn" onClick={() => setCollapsed(c => !c)}>
            {collapsed ? '▸' : '▾'} 思考过程
          </button>
          {!collapsed && <div className="hc-reasoning-body">{part.text}</div>}
        </div>
      );

    case 'tool_call':
      return (
        <div className="hc-part-tool-call">
          <span className="hc-tool-icon">⚙</span>
          <span className="hc-tool-name">调用 {part.tool_name}</span>
          {part.tool_input != null && (
            <code className="hc-tool-input">{JSON.stringify(part.tool_input)}</code>
          )}
        </div>
      );

    case 'tool_result': {
      const out = part.output ?? part.stderr ?? '';
      const isErr = part.exit_code != null && part.exit_code !== 0;
      return (
        <div className="hc-part-tool-result" data-error={isErr ? 'true' : undefined}>
          <button className="hc-collapse-btn" onClick={() => setCollapsed(c => !c)}>
            {collapsed ? '▸' : '▾'} 输出 {isErr && <span className="hc-exit-bad">exit {part.exit_code}</span>}
          </button>
          {!collapsed && <pre className="hc-tool-output">{out}</pre>}
        </div>
      );
    }

    case 'diff':
      return (
        <div className="hc-part-diff">
          <div className="hc-diff-path">{part.path}</div>
          <DiffView diff={part.diff ?? ''} />
        </div>
      );

    default:
      return null;
  }
}

/** 极简 markdown:代码块 + 行内代码 + 段落(不引重型依赖) */
function renderMarkdown(text: string): React.ReactNode {
  const blocks = text.split(/```/);
  return blocks.map((b, i) => {
    if (i % 2 === 1) {
      // 代码块
      const lines = b.split('\n');
      const lang = lines[0]?.trim();
      const code = lang && !lang.includes(' ') ? lines.slice(1).join('\n') : b;
      return <pre key={i} className="hc-codeblock"><code>{code}</code></pre>;
    }
    return <span key={i} className="hc-md-text">{b}</span>;
  });
}

/** diff 渲染:增绿删红 */
export function DiffView({ diff }: { diff: string }) {
  return (
    <div className="hc-diff">
      {diff.split('\n').map((line, i) => {
        let cls = 'hc-diff-line';
        if (line.startsWith('+') && !line.startsWith('+++')) cls += ' hc-diff-add';
        else if (line.startsWith('-') && !line.startsWith('---')) cls += ' hc-diff-del';
        else if (line.startsWith('@@')) cls += ' hc-diff-hunk';
        return <div key={i} className={cls}>{line || ' '}</div>;
      })}
    </div>
  );
}
