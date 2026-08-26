# 使用示例

跟 [快速开始](quickstart.md) 配合看：这里是几个具体场景的完整命令，覆盖交互式对话、无头脚本化、代码委派、会话恢复。全部是真实可跑的命令，不是伪代码。

## 交互式代码审查

```bash
cd ~/my-project
veya "帮我审查这个目录的代码，指出 3 个最值得修的问题"
```

主脑会自主决定要不要用 `grep`/`read_file_ast`/`list_files` 等只读工具读代码，不会自己动手改——审查类任务不需要写权限。

## 委派一个真实编码任务（hicode）

```bash
veya "把 utils.py 里的 parse_config 函数拆成两个更小的函数，跑一下测试确认没破坏行为"
```

涉及实际改代码/跑测试的任务，主脑通常会判断该不该委派给 hicode 执行器（独立工作区、可回滚）——这是模型自己的判断，不是关键词匹配触发的，参见 [`docs/architecture.md`](architecture.md) 的主链原则。

## 无头模式（脚本 / CI 里跑）

```bash
veya-headless --agent plan --text "为这个项目写一个 README 的快速开始章节" \
  > readme_draft.md

echo "exit code: $?"
```

`--agent plan` 是只读规划模式（不写文件不跑命令），适合先看方案再决定要不要真正执行。去掉 `--agent plan` 换成默认 agent 模式则可写可执行。

## 本地服务 + Web 对话

```bash
veya start
# 打开 http://127.0.0.1:8765
```

同一个 session_id 在 CLI 开始、Web 打开、TUI 查看，历史应该一致——如果不一致，先查 [故障排查](troubleshooting.md)。

## 中断后恢复

```bash
# 列出当前用户的持久会话
veya sessions

# attach 查看历史，resume 继续同一条 MasterAgent 会话
veya attach sess_abc123
veya resume sess_abc123

# API 也使用同一 canonical history
curl -s http://127.0.0.1:8765/api/v1/sessions/sess_abc123 \
  -H "Authorization: Bearer $VEYA_TOKEN" | jq
```

取消中的任务不会被静默标记为完成。系统会留下 `task.cancelled` 和安全
`checkpoint.created`，之后可以从任务或会话入口继续。

## 权限档位

```bash
# 只读、开发、生产三档；切换后只影响当前用户
curl -s http://127.0.0.1:8765/api/v1/permission/profiles | jq
curl -s -X POST http://127.0.0.1:8765/api/v1/permission/profile \
  -H 'content-type: application/json' -d '{"profile":"READ_ONLY"}'
```

## 图片相关任务（视觉工具链）

```bash
veya "看一下 ./screenshots/error.png，告诉我这个报错弹窗说了什么"
```

纯文本大模型本身看不了图片——主脑会自主判断要不要调用 `vision_glance`/`vision_ground`/`vision_long_screenshot_ocr` 等视觉工具，取证式地处理图片而不是瞎猜，参见 `docs/vision-tools/vision-tools.md`。

## 长程 / 多步骤任务

```bash
veya "调研一下当前项目的依赖有哪些能安全瘦身的，给出分批迁移方案，第一批直接执行"
```

多步骤任务主脑通常先 `create_plan` 拉一个待办清单，每步标 done/blocked 并附证据，而不是一口气全做完再汇报——中途可以用 `veya doctor`/会话历史核实每一步的真实执行轨迹，不是模型自己说了算。
