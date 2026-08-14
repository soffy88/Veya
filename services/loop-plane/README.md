# Loop Plane 微服务

> 版本 1.0 · 代号 loop-plane · SPEC: `docs/LOOP_PLANE_SPEC.md`（用户提供）

长程 Goal/Todo/Gate/Quota、因果规划与诊断、硬化干预、审计、可选调度与 Skill 实验。
**主链路仍为单 LLM + 工具；不替代 hicode；不恢复程序路由。**

## 架构与依赖方向

```
Caller (server master_tools) → loop-plane (:8787) → veya-loop / 3O 主库
```

- 单一部署单元（默认与 Veya 同 compose），也可进程内 client（`LOOP_PLANE_INPROCESS=true`）。
- 状态单一真相源 = EventStore（`events.jsonl` append-only）；旧 plan JSON 仅投影/迁移。

## 快速启动

```bash
cd services/loop-plane
pip install -e .          # 或直接 uvicorn（仓库 venv 已含依赖）
uvicorn app.main:app --port 8787
```

```bash
curl localhost:8787/healthz
curl -X POST localhost:8787/v1/loop/goals -H 'content-type: application/json' \
  -d '{"objective":"发布 v1","todos":[{"id":"t1","title":"写测试"}]}'
```

## 配置（SPEC §10）

| 变量 | 默认 | 说明 |
|---|---|---|
| `LOOP_PLANE_PORT` | 8787 | 服务端口 |
| `LOOP_DATA_DIR` | `~/.veya/loop` | 数据根（events/audit/graphs/skills/exports） |
| `LOOP_WORKSPACE` | cwd | sandbox 根 |
| `VEYA_LOOP_OPTIONAL` | true | 无 veya-loop 库时 causal 降级 |
| `LOOP_PLANE_URL` | — | server 工具 HTTP 转发地址 |
| `LOOP_PLANE_INPROCESS` | false | server 工具进程内转发（同一代码路径） |

## API（完整契约见 openapi.yaml）

- `GET /healthz`
- `POST/GET /v1/loop/goals[/{id}]` · `todos/{tid}` · `claim` · `quota/should_run|spend` · `gates/check` · `terminal_check`
- `POST /v1/loop/plan/goal|diagnose`（默认 execute=false；审计五节点写入 audit_trail.jsonl）
- `POST /v1/loop/exec/dispatch` · `GET runs/{id}` · `GET adapters`（mode 服务端强制收缩；白名单；sandbox 禁 `python -m` 任意路径）
- `POST/GET/DELETE /v1/loop/sched/jobs[/{id}]` · `POST trigger`（门面，内核委托 automata）
- `/v1/loop/skills/*`（P2 stub，501）

## 与主链路的关系

- **只加工具**：`loop_plan_goal` / `loop_diagnose` / `loop_intervene` 注册进 master_tools；
  `create_plan/plan_status/update_todo` 在 `LOOP_PLANE_URL` 或 `LOOP_PLANE_INPROCESS` 开启时转发到本服务（T8 可切回旧 plan_todo）。
- **不改主循环**：无 Coordinator 意图分流、无工具裁剪、hicode_run 不动。

## 测试

```bash
cd /data/soffy/projects/veya && ./venv/bin/python -m pytest services/loop-plane/tests -q
```

验收映射：T1 goal 创建/投影 · T2 update+evidence 事件 · T3 claim 冲突 409 · T4 plan/goal 报告 ·
T5 diagnose 结构 · T6 dispatch 白名单/mode 收缩 · T7 审计 trace 关联 · T8 flag 切回旧路径。

## 迁移

```bash
python services/loop-plane/scripts/migrate_plans_to_events.py --plans-dir ~/.veya/plans --dry-run
```

## 非目标

- 不替代 hicode / 不重写主 ReAct 循环
- 不恢复 Coordinator 程序化意图路由 / 工具裁剪
- 不强制多容器拆分（plan/exec/learn 分进程）
- Skill 实验（P2）接口已定，未实现
