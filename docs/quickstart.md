# 5 分钟快速开始

从安装到完成第一个真实任务，全程 5 分钟。

---

## 第 1 步：安装（1 分钟）

```bash
# Linux / macOS
curl -fsSL https://raw.githubusercontent.com/soffy88/Veya/main/install.sh | bash

# 或 pip / pipx
pip install veya
```

> 需要 Python ≥ 3.11。安装脚本会自动选择 pipx 或 venv，装完 `veya` 命令即可用。

## 第 2 步：接入模型（1 分钟）

```bash
veya init
```

跟着向导走：

1. **选模型**：OpenAI / Anthropic / DashScope / DeepSeek，或本地 **Ollama**（自动探测，免 Key）
2. **粘贴 API Key**：输入为空则进入离线 stub 模式（先体验流程，之后随时 `veya init` 补）
3. **选工作目录**：Agent 的文件读写与命令执行范围

完成后自动生成：
- `~/.veya/config.json` — 主配置
- `~/.veya/.env` — Key（权限 600）
- 工作区 `.veya/security.yaml` — 安全策略（危险操作默认受限）

验证：`veya doctor` — 全 ✔ 即可开工。

## 第 3 步：跑第一个真实任务（2 分钟）

```bash
# 进入你的工作目录
cd ~/my-project

# 方式 A：交互式（推荐）
veya "帮我审查这个目录的代码，指出 3 个最值得修的问题"

# 方式 B：无头单次任务（适合脚本/CI）
veya-headless --agent plan --text "为这个项目写一个 README 的快速开始章节"

# 方式 C：本地服务 + 浏览器
veya start
# 打开 http://127.0.0.1:8765 后即可在 Web UI 对话
```

## 升级 & 迁移

```bash
# 检查版本 + 待迁移项
veya upgrade --check

# 执行配置迁移 (跨版本兼容)
veya migrate --apply
```

## 跨入口会话与任务

会话使用统一的 `sess_<uuid7>` ID，CLI、Web 和 API 共享同一持久历史：

```bash
veya sessions
veya attach <session_id>
veya resume <session_id>
```

任务中心可查看真实工具轨迹、审批、成本、checkpoint 和取消状态；需要脚本化时
使用 `/api/v1/tasks` 与 `/api/v1/tasks/{task_id}/events`。

详情见 [故障排查](troubleshooting.md)。

## 下一步

- 读 [使用示例](examples.md) 看更多具体场景的完整命令
- 遇到问题先查 [故障排查](troubleshooting.md)
- 读 [落地页](index.md) 了解全部能力（量化协处理 / 零信任金库 / 因果诊断 / 反脆弱闭环…）
- 读 [路线图](roadmap.md) 了解产品化演进
- 开发者：见 [架构说明](architecture.md)，测试：`python -m pytest tests/ -q`
