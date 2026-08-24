# Veya — 面向长期任务、工具执行与闭环验证的开放 Agent Runtime

> **下载 / 快速开始 / 填 API Key 就能跑。** Coding 是第一个应用面，不是最终
> 定义——持久会话/记忆（Persistent）+ 工具执行/沙箱（Executable）+ 审计/
> 评估闭环（Verifiable，还在补齐中，见 `docs/dev/rfc-09`/`rfc-10`）。

## 立刻开始

```bash
curl -fsSL https://raw.githubusercontent.com/soffy88/Veya/main/install.sh | bash
veya init      # 30 秒接模型 + 选工作目录
veya start     # 本地服务 + 浏览器
```

或阅读 [5 分钟快速开始](quickstart.md)。

## 能力总览

| 层 | 能力 |
|---|---|
| **Agent** | Plan（任务分解）/ Research（研究）/ Build（执行），多 Agent 蜂群编排 |
| **执行** | 隔离沙箱（netns / Docker --network=none）、硬化执行器、零信任金库 HITL 审批 |
| **认知** | Workspace RAG、因果图 + do-calculus 诊断、贝叶斯意图雷达、蜜罐反间谍、多步反事实规划、策略自演化 |
| **3O 主库** | obase（数据面）/ oprim（推理）/ oskill（技能）/ omodul（业务事务）/ oservi（服务编排） |
| **控制面** | 量化协处理（控制面/数据面分离）、Omni-Channel 全渠道网关、决策审计统一写出口 |
| **入口** | CLI / Web UI / TUI / VSCode 扩展 / HTTP + SSE |

## 模型接入

- **云端**：OpenAI / Anthropic / DashScope / DeepSeek —— `veya init` 一键写入 Key
- **本地**：Ollama 自动探测，免 Key
- **兼容端点**：`VEYA_LLM_ENDPOINT` 指向任意 OpenAI 兼容服务（NIM / vLLM / 网关）

## 文档

- [快速开始](quickstart.md)
- [架构说明](architecture.md)（开发者）
- [路线图](roadmap.md)
- [Veya Loop](https://github.com/soffy88/Veya/tree/main/veya_loop) — 因果闭环控制基板（独立包 `veya-loop`）
