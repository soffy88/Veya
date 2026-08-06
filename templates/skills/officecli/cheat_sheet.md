# OfficeCLI 速查 (AI 操作手册 — 供 LLM 选工具时内嵌)

## 通用形态

```
officecli <op> <input> [--output <out>] [--data <json>] [--options <json>]
```

## 操作表

| op | 语义 | 读写 | 示例 |
|---|---|---|---|
| `add` | 新建文档 | 写 | `officecli add report.docx --options '{"type":"doc","prop":{"title":"周报"}}'` |
| `edit` | 修改文档 | 写 | `officecli edit report.docx --options '{"prop":{"author":"veya"}}'` |
| `read` | 读取内容 | 只读 | `officecli read report.docx` |
| `convert` | 格式转换 | 只读 | `officecli convert report.docx --output report.pdf` |
| `merge` | 合并文档 | 写 | `officecli merge a.docx b.docx --output merged.docx` |
| `dump` | 导出结构化内容 | 只读 | `officecli dump sheet.xlsx --options '{"sheet":"Sheet1"}'` |
| `batch` | 批量操作 (data 驱动) | 写 | `officecli batch --data '[{...},{...}]'` |
| `render` | 渲染 HTML/PNG | 写 | `officecli render report.docx --output report.png` |
| `watch` | 监听变化 | 写 | `officecli watch report.docx` |

## 渲染→观察→修复闭环

```
officecli render report.docx → report.png
  → Veya G13 Vision (/api/v1/vision/analyze) 检查排版溢出/图表重叠
  → officecli edit 定点修复 → 再 render → 通过
```

## 安全边界 (硬性)

- 写操作仅限: workspace 与 `~/.veya/templates/` 输出区 (白名单外拒绝)
- 只读操作免审批; 写操作需 permission_gate
- 凭证/客户敏感数据过 redact, 永不进模板
