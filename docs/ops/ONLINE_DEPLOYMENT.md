# Veya 线上部署与故障排查手册

> 实战沉淀 2026-08 · 域名 veya.aiinote.com · 若此文档与 AGENTS.md 运维章节冲突，以本文档为准并回写 AGENTS.md。

---

## 1. 部署拓扑（一句话版）

```
浏览器 → Cloudflare(443, DNS: 104.21.90.117/172.67.200.102)
      → 前端 SvelteKit adapter-node (systemd veya-web, PORT=3105)
      → VEYA_GATEWAY=http://127.0.0.1:8767
      → docker backend 容器 (deploy/docker-compose.yml)
         端口映射: 8767:8765 (gateway 通道), 9120:8765 (legacy 通道)
         CMD: uvicorn server.app:app --host 0.0.0.0 --port 8765  ← 根 app!
```

**关键事实**：线上后端跑的是**根 app（server.app:app）**，不是 veya L4 gateway。因此所有 `/api/v1/*` 端点必须能在根 app 上访问（见 §3 端点归属表）。

## 2. 端口与进程归属

| 端口 | 宿主服务 | 说明 |
|---|---|---|
| 3105 | `veya-web` (systemd) | node build/index.js，`Environment=VEYA_GATEWAY=http://127.0.0.1:8767` |
| 8767 | docker `veya-backend` 映射 | **若被 systemd `veya-gateway`（`python3 -m veya.server.app --port 8767`）占用 → docker bind 失败，旧进程继续服务 404**。deploy.sh 有"先停旧 systemd 网关"逻辑，手动部署时最容易漏 |
| 9120 | docker `veya-backend` 映射 | legacy 通道 (VEYA_LEGACY) |
| 8765 | 本机开发机 = helivex api-gateway（**外部项目**） | 开发环境别把 curl 8765 当 veya |

## 3. 端点归属（改端点前必读）

| 端点 | 根 app (server.app) | veya L4 (veya.server.app) |
|---|---|---|
| `/api/v1/agent/stream` | ✅ legacy_agent router | ✅ |
| `/api/v1/agent/run` | ✅ legacy_agent router | ✅ |
| `/api/v1/scheduler` | ✅ **cindy_compat.py** | ✅ (原生) |
| `/api/v1/plugin/manage` | ✅ **cindy_compat.py** | ✅ (原生) |
| `/api/v1/knowledge` | ✅ **cindy_compat.py** | ✅ (原生) |
| `/api/v1/mcp/health` | ✅ **cindy_compat.py** | ✅ (原生) |
| `/api/v1/mcp/categories` | ✅ **cindy_compat.py** | ✅ (原生) |
| `/api/v1/agent/skills-inject` | ✅ **cindy_compat.py** | ✅ (原生) |
| `/api/v1/voice/ws` | ✅ **voice_compat.py** (2026-08-18 补) | ✅ (原生) |

- **新 Cindy 类端点：两头都要挂**。根 app 侧挂在 `server/routes/cindy_compat.py`（惰性 import 3O 主库，照 legacy_agent 兼容模式），veya L4 侧在 `veya/server/app.py`。
- 前端探活路径是 `/api/v1/mcp/health`（`apps/web/src/lib/upstreamProbe.ts`），删改需同步。

## 4. 部署步骤（docker 形态）

```bash
cd /data/soffy/projects/veya
git pull                              # 必须已 push 到 origin/main
# 防 8767 冲突: 先停 systemd 网关
sudo systemctl stop veya-gateway 2>/dev/null; sudo systemctl disable veya-gateway 2>/dev/null
# server/ 与 veya_loop/ 已挂载进容器 → 免 build, 直接重建容器
docker compose -f deploy/docker-compose.yml up -d backend
# 同步主脑 LLM 配置 (config.json / llm-router.json → veya-data volume)
./scripts/sync-veya-config.sh
# 验证矩阵
curl -s -X POST http://127.0.0.1:8767/api/v1/scheduler -H 'Content-Type: application/json' -d '{"action":"list"}'
curl -s -X POST http://127.0.0.1:8767/api/v1/plugin/manage -H 'Content-Type: application/json' -d '{"action":"marketplace"}'
curl -s http://127.0.0.1:8767/api/v1/mcp/health
```

> 若采用 systemd 形态（veya-gateway.service，veya.server.app）：
> `git pull && sudo systemctl restart veya-gateway`，并确保前端 VEYA_GATEWAY 指向 8767。

## 4.1 主脑 LLM 配置固化（重建后必读）

主脑默认回答链路（无参调用 → veya1.2 别名路由）依赖两份用户配置：

| 文件 | 作用 | 消费方 |
|---|---|---|
| `~/.veya/config.json` 的 `llm` 段 | 无参调用兜底默认 `provider/model`（当前 `veya1.2`） | `veya/llm.py get_provider_config` |
| `~/.veya/llm-router.json` | 路由矩阵（quick/text/tool/code/long → veya1.2 OpenRouter 双模型；reason/frontier/planner → openai@10100） | `oprim/_llm_router.py load_matrix`（mtime 热重载） |

**位置**：容器内 `/home/soffy/.veya` 挂载自 named volume `deploy_veya-data`（`/data/docker/volumes/deploy_veya-data/_data`）——`docker cp` 进 volume 即持久，**容器重建不丢**。

**同步命令**（宿主改配置后执行）：

```bash
./scripts/sync-veya-config.sh
```

**重建 volume 恢复步骤**（`docker volume rm deploy_veya-data` 后）：

```bash
cd /data/soffy/projects/veya
# 先起容器 (空 volume 自动创建)
docker compose -f deploy/docker-compose.yml up -d backend
./scripts/sync-veya-config.sh   # 恢复 config.json + llm-router.json
```

> ⚠ 配置缺失症状：`coordinator.chat()` 返回 `LLM provider not configured`（stub）或回答人格异常——先查 `docker exec veya-backend ls /home/soffy/.veya/`。

## 5. 故障排查矩阵（按症状定位）

| 症状 | 第一步 | 根因示例 |
|---|---|---|
| 前端插件市场/定时任务"加载失败"，`/api/v1/*` 返回 `{"detail":"Not Found"}` | `curl https://veya.aiinote.com/api/v1/mcp/health` | 后端没部署新代码 / 8767 被旧进程占用 / 端点只挂在 veya L4 而线上跑根 app |
| 返回 `{"code":404,"message":"path ... was not found"}` | — | 打到了 helivex 等**外部 FastAPI 服务**（非 veya 404 格式） |
| `docker compose up` 报 `bind: address already in use` | `sudo ss -tlnp \| grep 8767` | systemd veya-gateway 占用 8767 |
| 前端报"网关不是 veya 服务"引导 | 见引导文案 | `VEYA_GATEWAY` 指向非 veya 端口（默认 8765 在本机是 helivex） |
| `agent/stream` 200 但其他 `/api/v1/*` 404 | 对照 §3 归属表 | 线上跑根 app，端点没挂 cindy_compat |

- veya 404 格式：`{"detail":"Not Found"}`（FastAPI 标准）
- 外部 helivex 404 格式：`{"code":404,"message":"..."}`
- 前端 bundle 是否含某 API 调用：下载 `/_app/immutable/**/*.js` 后 grep。

## 6. 开发环境注意事项

- 本机 8765 = helivex（外部），开发一律 `veya start`（自动避让端口）或 `--port` 指定，前端 `apps/web/.env` 设 `VEYA_GATEWAY=http://127.0.0.1:<port>`。
- `veya doctor --json`：版本 / Key / Ollama / 工作区 / 端口自检。
- 根 app 本地起服：`uvicorn server.app:app --port 9124`（或 pytest TestClient，见 `tests/test_cindy_compat.py`）。
