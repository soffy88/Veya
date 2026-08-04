# Veya — AI 编码 agent

Veya 是模块化 AI 编排框架：**LLM 意图分解 → 沙箱执行 → SSE 流式返回**，
支持代码感知推理、研究、规划与构建（plan → research → build）。

> 原名 **hicode**，v0.5.0 起更名为 **Veya**。

## 核心特性

| 能力 | 说明 |
|------|------|
| 🧭 真实 LLM Provider | `veya/llm.py`：OpenAI / Anthropic / DashScope 统一层（补全/流式/工具调用/成本计算） |
| 🧠 LLM 意图分类 | `veya/intent.py`：长短文本快速路径 + 关键词 + LLM 兜底（省 token） |
| 🏖️ 沙箱执行 | `veya/sandbox.py`：内存/CPU ulimit 子进程隔离、危险命令拦截、参数数组执行 |
| 📡 SSE 流式 | `server/` SSE 队列 + 事件流（session/squad/token/cost/task 事件） |
| 🔐 交互式权限 | `veya/obase/authz.py`：allow/deny/ask 规则引擎 + CLI/HTTP 确认 |
| 📊 可观测性 | `veya/obase/telemetry.py`：JSONL trace + ContextVar 传播 + `@traced` |
| 🧩 3O 范式基座 | `veya/obase/`：依赖方向铁律（obase 永不 import 业务层）+ manifest 契约 |
| 🖥️ 编辑器闭环 | VS Code 扩展：发起任务 → SSE 增量渲染 → 工作区刷新 |
| 📈 可视化 | 2D/3D 图（networkx/plotly/matplotlib）、架构图、JSON/image 导出 |

## 快速上手

```bash
# 安装
pip install -e .

# CLI 交互式
veya --persona build

# 无头模式
veya-headless --agent plan --input "设计一个数据管道"

# HTTP 服务
veya serve
```

## 一键验证

```bash
pytest tests/ --cov=veya --cov=server --cov=agents --cov=tools   # 测试 + 覆盖率
ruff check veya/ server/ cli/                                     # lint
mypy --config-file pyproject.toml server/coordinator.py veya/...  # 类型检查
python3 scripts/check_obase_no_reverse_dep.py                     # 3O 依赖方向
```

## 项目结构

```
veya/          # 核心包：llm / intent / sandbox / multimodal / obase(3O 基座)
server/        # FastAPI + SSE + coordinator(DAG 编排)
agents/        # plan / research / build 智能体
tools/         # 可组合工具函数
cli/           # veya / veya-headless / veya-simple
config/        # 配置加载 + 权限策略
hooks/         # 生命周期钩子（pre/post dispatch、权限、安全）
registries/    # 工具/模型/技能/插件动态注册
tests/         # 200+ 集成与 E2E 测试
docs/          # 本站点 + 标杆差距报告
```
