---
name: answer-me-with-html
description: 把复杂答案渲染成一页 HTML 讲解稿。当用户需要理解流程、对比方案、或需要可视化说明时主动使用。
---

# answer-me-with-html

调用本技能将 Markdown 草稿渲染为单文件 HTML 讲解稿。返回 HTML 路径与可访问链接。

## 何时使用

- 解释流程/协议/状态机（如 TCP 三次握手）
- ≥3 维度的对比或权衡分析
- 多步计划或决策树
- 诊断报告或评审结果
- 用户明确要求"画个图"或"可视化说明"

## 何时不使用

- 简单问答（1-2 句即可回答）
- 闲聊或确认性回复
- 纯数据查询结果

## 参数说明

| 参数 | 必填 | 说明 |
|------|------|------|
| `draft` | ✓ | Markdown 草稿，包含 frontmatter 和至少一个 ## 面板 |
| `output` | ✗ | 输出路径，缺省自动命名 |
| `theme` | ✗ | 主题：auto/blueprint/shadcn/paper |
| `template` | ✗ | 版式：sheet/doc |
| `style` | ✗ | STE 检查：off/80/strict |

## 草稿格式示例

```markdown
---
title: TCP 三次握手
---

## A 连接建立
SYN 包从客户端发往服务器。

## B 确认响应
服务器回复 SYN-ACK。

## C 最终确认
客户端发送 ACK，连接建立完成。
```

## 返回值

技能返回 JSON，包含：
- `html_path`: 本地 HTML 文件路径
- `file_url`: 可访问的 HTTP 链接
- `share_url`: 可选的公开分享链接

主脑应使用 `file_url` 作为回复中的链接，让用户可以直接点击查看。
