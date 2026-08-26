# 故障排查

先跑 `veya doctor`（或 `veya doctor --json` 脚本化）——下面每一项都是它实际会检查的项，按它报的 ✗ 对号入座。

## 环境 / 安装

| 症状 | 原因 | 解决 |
|---|---|---|
| `python>=3.11` 报 ✗ | Python 版本过低 | Veya 需要 Python ≥ 3.11，升级解释器后重装 |
| `veya 已安装` 报 ✗ | `import veya` 失败 | `pip install veya`，或在源码仓库里确认 `pip install -e .` 装的是当前目录（不是别的项目残留的 editable 安装——症状是版本号对不上：`pip show veya` 的 `Editable project location` 如果指向别的路径，说明装错了，重新 `pip install -e .` 覆盖） |
| `~/.veya/config.json` 报 ✗ | 从未初始化 | `veya init` 走一遍向导 |

## 模型接入

| 症状 | 原因 | 解决 |
|---|---|---|
| `模型接入 (<provider>)` 报 ✗ | 对应的 `<PROVIDER>_API_KEY` 未设置 | `veya init` 重新粘贴 Key，或直接 `export ANTHROPIC_API_KEY=...`（按 provider 换变量名） |
| Ollama 报"不可达" | 本地 Ollama 服务没启动，或模型没拉取 | `ollama serve` 确认在跑，`ollama pull qwen2.5:7b` 拉模型，再 `veya doctor` 复查 |
| 回复里出现"网关抖动"/"could not reach ..." 这类兜底文案 | 不是崩溃——是主脑内建的三层空回复兜底之一被触发（LLM 网关短暂不可达），见 `docs/ARCHITECTURE_STABLE.md` §2.3 | 直接重试；如果持续出现，检查 `veya doctor` 的模型接入项和 API Key 额度 |

## 工作区 / 端口

| 症状 | 原因 | 解决 |
|---|---|---|
| `工作区` 报 ✗ | 配置的工作目录不存在或未设置 | `veya init` 重新选择存在的目录 |
| `端口 8765` 报 ✗ | 已有进程占用（可能是上一个没退干净的 `veya start`） | `veya start --port 9000` 换端口，或找到并结束占用进程 |

## 升级 / 迁移

| 症状 | 原因 | 解决 |
|---|---|---|
| `veya upgrade --check` 显示待迁移项 | 配置版本落后代码基线 | `veya migrate --apply` 执行迁移 |
| 迁移后配置不生效 | 旧字段未清理 | `veya doctor --json` 复查 `config_version` 是否已更新 |

---

## 权限 / 沙箱

| 症状 | 原因 | 解决 |
|---|---|---|
| 写文件/执行命令被拒绝 | 默认最小权限——工作区 `.veya/security.yaml` 限制了危险操作 | 对话里确认一次性放行，或编辑 `.veya/security.yaml` 放开对应规则 |
| 某个技能加载失败，报"命中高危调用面...strict 模式拒载" | Skill Hub 对技能做静态扫描，命中 `command-exec`/`fs-destructive` 等高危调用面时默认拒绝加载（防止不可信技能悄悄跑命令/删文件） | 确认该技能来源可信后，把技能名加进环境变量 `VEYA_SKILL_TRUSTED_NAMES`（逗号分隔）再重启 |

## 会话 / 恢复

| 症状 | 原因 | 解决 |
|---|---|---|
| 中断后想接着上次的对话 | 用 canonical session_id 恢复，不是重新开始 | `veya sessions` → `veya attach <session_id>` → `veya resume <session_id>`；API 使用 `GET /api/v1/sessions` |
| 长任务中途进程被杀，恢复后丢了最后一点进度 | 断点续跑按固定间隔（默认 15s，见 `VEYA_CHECKPOINT_INTERVAL_S`）落快照，最多丢一个间隔内的工作，不是整轮 | 正常现象；如果丢的太多，调小 `VEYA_CHECKPOINT_INTERVAL_S` |
| Task Center 显示 waiting approval | 高影响工具已经请求审批，任务未完成 | 在权限确认卡片批准/拒绝；检查 `GET /api/v1/tasks/{task_id}/events` 的真实事件 |
| doctor 报 history store schema 错误 | 旧版本使用 `idx` 快照表，新版本使用 immutable `revision` | 运行 `veya migrate --apply` 或重新执行 `veya doctor`；启动时会保留旧快照并完成兼容迁移 |

## 还是没解决？

带上 `veya doctor --json` 的完整输出、复现步骤、Veya 版本号（`veya --version`），去 [GitHub Issues](https://github.com/soffy88/Veya/issues) 提问；涉及安全漏洞不要走公开 Issue，见 [`SECURITY.md`](https://github.com/soffy88/Veya/blob/main/SECURITY.md)。
