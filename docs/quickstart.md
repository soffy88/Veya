# 快速开始

## 1. 配置 LLM Provider

支持三种 provider，通过环境变量或 `config/loader.py` 配置：

```bash
# DashScope（默认，qwen-plus）
export DASHSCOPE_API_KEY=sk-xxx
# 或 OpenAI
export OPENAI_API_KEY=sk-xxx
export VEYA_LLM_PROVIDER=openai
export VEYA_LLM_MODEL=gpt-4o-mini
# 或 Anthropic
export ANTHROPIC_API_KEY=sk-ant-xxx
export VEYA_LLM_PROVIDER=anthropic
```

没有 API key 时自动降级为 stub 响应（离线开发/测试友好）。

## 2. CLI

```bash
veya                          # 交互式（默认 persona=build）
veya --persona research       # 指定智能体
veya --resume <session_id>    # 从 checkpoint 恢复
veya-headless --agent plan --input "..."   # 无头
veya-simple                   # 轻量交互（含权限确认）
```

## 3. HTTP 服务

```bash
veya serve
```

主要端点（无 `/api/v1` 前缀）：

| 端点 | 说明 |
|------|------|
| `POST /agent/{name}` | 执行 agent（plan/research/build） |
| `POST /vscode/run-stream` | 后台执行 + 返回 SSE session |
| `GET /stream/{session_id}` | SSE 事件流 |
| `POST /permission/evaluate` | 权限评估（可能返回 pending） |
| `POST /permission/{id}/approve` | 批准权限请求 |

## 4. 多模态（G12）

```python
from veya.multimodal import MultimodalProcessor
from veya import llm as hllm

messages = MultimodalProcessor().build_vision_messages(
    "这是什么截图？", ["/tmp/shot.png"], system="你是视觉助手"
)
resp = await hllm.llm_call(messages, provider="dashscope", model="qwen-vl-max")
```

图片以 OpenAI 风格 content blocks（`image_url` data-URI）发送；
Anthropic provider 自动转换为原生 `image` block。
