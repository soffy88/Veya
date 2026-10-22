# 集成上线报告（2026-10-22）

本轮把 5 条 Phase 0 / 基线修复分支按指定顺序合入 `main`、跑测试与前端构建、
推送远端，并以薄镜像方式上线到 veya.aiinote.com。

- 起始线上版本：`veya-backend:cb03ebfe`（commit `cb03ebfe`，health `0.6.0-cb03ebfe`）
- 上线版本：`veya-backend:9172671f`（commit `9172671f`，health `0.6.0-9172671f`）
- 合并顺序：`fix/baseline` → `feat/olv-phase0` → `feat/dsh-review-phase0` →
  `feat/harness2-phase0` → `feat/speckit-phase0`
- 结果：5 条全部合并，0 条跳过，0 条回滚。

---

## 一、前置等待与分支状态

任务要求先等 `base2.exit` 与 `dsh2.exit`。开始执行时两个标记文件均已存在
（`~/wim/all/base2.exit`、`~/wim/all/dsh2.exit`，时间戳 11:30 / 11:25），
因此等待循环立即返回，未实际阻塞。

`~/wim/all/*.done` 里只有 `dsh.done`、`olv.done`、`spk.done`；`base`/`h2` 没有
`.done`（`h2` 的 worker 在 max turns 处中断）。但两条分支的**已提交**内容齐全：

| 分支 | HEAD | 领先 main | 工作区状态 |
|---|---|---|---|
| `fix/baseline` | `594aa7e8` | 1 commit | 干净（仅一份未跟踪的草稿报告，未合并） |
| `feat/olv-phase0` | `904af423` | 1 commit | 干净 |
| `feat/dsh-review-phase0` | `d93bcbfe` | 1 commit | 干净 |
| `feat/harness2-phase0` | `94e0cbba` | 4 commits | Phase 1 未提交改动留在 worktree，**未合并** |
| `feat/speckit-phase0` | `a5365d33` | 1 commit | 干净 |

两处取舍，都是按「只合已完成且测试通过的部分」处理：

1. **`feat/harness2-phase0` 只合 Phase 0。** 该 worktree 里还有未提交的 Phase 1
   代码（`server/harness/channel.py`、`proposer.py`、`scripts/run_harness2_phase1.py`
   等）。Phase 1 的 worker 在 max turns 处中断，没有完成验证，所以这些文件既没有
   提交也没有合并，仍原样留在 `~/projects/veya-h2`。合并进 main 的是已提交的
   Phase 0 适配器与 base harness 导出器（4 个 commit，9 个测试通过）。
2. **`docs/baseline-fix-20261022.md` 未合并。** 该报告在 `fix/baseline` 的
   worktree 里是未跟踪文件，内容仍是「待填」草稿（11 张表格单元全部是待填）。
   `fix/baseline` 的代码提交本身完整（`test_llm` 回归测试、ruff 修复、
   FlowConsole fetch 流式读取），合并的是代码；草稿报告没有进 main。
   `tests/test_llm.py` 在 main 上实测 37 passed，确认该修复生效。

---

## 二、合并记录

全部使用 `--no-ff` 合并提交，`main` 上一共 5 个 merge commit：

```
9172671f merge: feat/speckit-phase0 (SDD stage templates + .speckit workspace, default OFF)
6c51116a merge: feat/harness2-phase0 (benchmark adapter + base harness export, default OFF)
42b07354 merge: feat/dsh-review-phase0 (decision-review tool slot, default OFF)
1e297774 merge: feat/olv-phase0 (Open-LLM-VTuber bridge, default OFF)
3af53618 merge: fix/baseline (llm test isolation, ruff, FlowConsole fetch stream)
```

### 冲突

两次冲突，都只在 `server/feature_flags.py` 的 `FLAGS` 元组尾部，因为三条分支都在
同一位置追加自己的开关：

| 合并 | 冲突 | 解决方式 |
|---|---|---|
| `feat/dsh-review-phase0` | `VEYA_OLV` vs `VEYA_DSH_DECISION_REVIEW` 同一插入点 | 两条都保留，`VEYA_OLV` 在前 |
| `feat/speckit-phase0` | 同上，再加 `VEYA_SPECKIT` | 三条都保留，追加顺序 OLV → DSH → SPECKIT |

`server/app.py`（olv 加路由 + dsh 加 lifespan 接线）与 `server/tool_registry.py`
（speckit 加注册点）都是自动合并成功，无人工改动。

解决后校验：`FLAGS` 共 66 条，三个新开关都在表尾且 `default=False`。

### 各分支内容

| 分支 | 主要改动 |
|---|---|
| `fix/baseline` | `tests/test_llm.py` 回归测试（隔离 llm pool 状态）、`server/tool_registry.py` 4 处 ruff、`apps/web/src/lib/components/FlowConsole.svelte` 改用 fetch 读流（修掉 11 个 svelte-check 错误） |
| `feat/olv-phase0` | `server/vtuber_bridge.py` 只读状态模块、`server/routes/vtuber.py` 可选状态路由、`config/olv.conf.yaml` 模板、开关 `VEYA_OLV` |
| `feat/dsh-review-phase0` | `server/decision_review.py` + `decision_review_core.py`、`decision_review` 惰性工具槽位、lifespan 接线、开关 `VEYA_DSH_DECISION_REVIEW` |
| `feat/harness2-phase0` | `server/harness/` 基准适配器、只读 base harness 导出器、`scripts/run_harness2_phase0.py` |
| `feat/speckit-phase0` | `server/speckit_{phases,skills,workspace}.py`、`templates/speckit/`（16 个文件）、`tool_registry` 只读模板发现、开关 `VEYA_SPECKIT` |

---

## 三、测试结果

基线取 `main` @ `cb03ebfe`（合并前实测），之后每次合并都重跑同一组套件。
套件用仓库的 CI 定义 `scripts/ci_test_suites.py` 展开，覆盖 CI 矩阵的四个
required suite：`unit-fast`、`runtime`、`goalrun`、`personal`。

| 套件 | 基线（cb03ebfe） | 合并后（9172671f） | 新增失败 |
|---|---|---|---|
| unit-fast | 13 failed, 393 passed | 13 failed, 393 passed | 0 |
| runtime | 8 failed, 532 passed, 5 skipped, 1 xfailed | 8 failed, 532 passed, 5 skipped, 1 xfailed | 0 |
| goalrun | 2 failed, 326 passed, 1 skipped | 2 failed, 326 passed, 1 skipped | 0 |
| personal | 1 failed, 212 passed | 1 failed, 212 passed | 0 |

不只是数量相同：逐条比对失败 nodeid 的集合，四个套件在每次合并后都与基线
**完全一致**（`diff` 无输出）。这 24 个失败在 `cb03ebfe` 上就存在，属于既有
债务，本轮不新增、也不顺手改。

每次合并后的结果（m1…m5 分别对应 5 次合并）与基线一致，因此没有触发
「失败分支跳过」的路径。

### 新增测试

各分支自带的新测试单独跑，全部通过：

| 测试 | 结果 |
|---|---|
| `tests/server/test_vtuber_bridge.py` | 16 passed |
| `tests/server/test_decision_review.py` | 43 passed |
| `tests/server/test_harness_adapter.py` | 9 passed |
| `tests/server/test_speckit_phase0.py` | 22 passed |
| `tests/test_llm.py`（baseline 修复目标） | 37 passed |

### Lint / 类型

- `ruff check`（全仓）：`cb03ebfe` 325 → `9172671f` 321，减少 4 条，无新增。
- `ruff format --check`：对比基线，**没有**新增未格式化文件。
- 逐文件检查本轮新增/改动的 Python：`server/harness/`、`server/speckit_*.py`、
  `server/decision_review*.py`、`server/vtuber_bridge.py`、`server/routes/vtuber.py`、
  `server/feature_flags.py`、`server/tool_registry.py` 均 All checks passed。
- `server/app.py` 有 2 条 `UP041`，在 `cb03ebfe` 上就是同样的两行，属既有问题，
  未改动（按「不修无关代码」处理）。

### 前端

- `svelte-check`：0 errors, 39 warnings（基线里 FlowConsole 的 11 个错误已消失）。
- `pnpm --dir apps/web build`：每次合并后都跑，全部成功。

---

## 四、上线

### 镜像

按任务要求做薄镜像，`FROM` 当前线上镜像，只覆盖 Python 树：

```dockerfile
FROM veya-backend:cb03ebfe
ARG VEYA_BUILD_SHA=9172671f
ENV VEYA_BUILD_SHA=$VEYA_BUILD_SHA
COPY server/ ./server/
COPY veya/ ./veya/
COPY tests/ ./tests/
COPY templates/speckit/ ./templates/speckit/
```

`templates/speckit/` 是额外加的一行，原因：`server/speckit_skills.py` 通过
`Path(__file__).parent.parent / "templates/speckit"` 在运行时读取这 16 个模板文件，
基础镜像里没有 `/app/templates`。不 COPY 的话，`VEYA_SPECKIT` 打开后模板发现会
返回空列表。这是新合入功能自身的运行期资产（196K），不是新增依赖。

镜像 `veya-backend:9172671f`，`VEYA_BUILD_SHA=9172671f`，约 6.02GB（与基线同量级）。

### 部署

```
export HOME=/home/soffy
bash deploy/deploy-immutable.sh
```

脚本输出：

```
[deploy] current=veya-backend:cb03ebfe target=veya-backend:9172671f
deployed_at=20261022T120509Z
image=veya-backend:9172671f
previous_image=veya-backend:cb03ebfe
commit=9172671fe1ff3ad39ffda2705e7e0aa1f2048160
health_version=0.6.0-9172671f
[deploy] healthy immutable image: veya-backend:9172671f
```

全程走 `deploy-immutable.sh`（内含 health/version 校验、禁止代码 bind mount 校验、
依赖 preflight、失败自动回滚）。**没有**执行裸 `docker compose up`。
`backend-override.yml` 只被脚本按设计改写了 `image:` 一行（→ `9172671f`），
其余内容未动。

### 前端

`fix/baseline` 改了 `FlowConsole.svelte`，属于前端改动，按仓库既有方式部署：
`pnpm --dir apps/web build` 后 `systemctl restart veya-web`（adapter-node，
`/data/soffy/projects/veya/apps/web/build/index.js`，端口 3105）。
重启后 `veya-web` active，`http://127.0.0.1:3105/` 200，构建产物已包含
`getReader` 流式读取路径。

---

## 五、上线验证

| 检查 | 结果 |
|---|---|
| `curl https://veya.aiinote.com/health` | **200**，`status: ok`，`details.backend.version = 0.6.0-9172671f` |
| 版本是否为新 sha | 是（`9172671f`） |
| 本地网关 `http://127.0.0.1:8767/health` | 200，`version: 0.6.0-9172671f` |
| 容器 `VEYA_BUILD_SHA` | `9172671f` |
| 容器状态 | `Up (healthy)` |
| tailnet 7767 | `http://100.86.95.70:7767/health` **200**，`{"ok": true, "v": 1, "node": "ubuntu26.taile7dda2.ts.net"}`；监听在 `100.86.95.70:7767`，docker 映射 `7767:7767` |
| 公网首页 | 200，正常返回 HTML |
| 容器启动日志 | `Application startup complete`，无 traceback；`master_tools=204`，durable/personal runtime ready |

### 新开关默认关闭

在运行中的容器里直接读 flag：

```
VEYA_OLV               default=False enabled=False
VEYA_DSH_DECISION_REVIEW default=False enabled=False
VEYA_SPECKIT           default=False enabled=False
```

工具面核对：`master_tools.list_tools()` 175 条，其中没有 `decision_review`、
没有 speckit 模板发现工具；`/api/v1/vtuber/status` 返回 401（路由未注册，落到
全局默认拒绝）。三条新能力在默认状态下都不出现在主脑工具面上。

---

## 六、跳过与未完成

- **没有分支被跳过。** 5 条全部合并、测试无新增失败、前端构建通过。
- `feat/harness2-phase0` 的 **Phase 1** 未合并（worker 中断在 max turns，改动
  未提交且未验证），仍在 `~/projects/veya-h2` worktree 里。
- `docs/baseline-fix-20261022.md` 草稿未合并（内容为「待填」），仍在
  `~/projects/veya-base` worktree 里。
- `main` 工作树里原有的 9 个未跟踪 `docs/*-integration.md` 报告未被本轮触碰。
- 24 个既有测试失败（沙箱边界、执行权威、skill hub 等）未修，属本轮范围外。

---

## 七、回滚

回滚目标镜像：`veya-backend:cb03ebfe`（上一版本，仍在本地）。

推荐方式（与部署同一脚本的状态文件配套）：

```bash
export HOME=/home/soffy
# 1) 把 override 指回上一版本
sed -i -E 's#^    image: veya-backend:.*#    image: veya-backend:cb03ebfe#' \
  /home/soffy/.config/veya/backend-override.yml
# 2) 重建容器（脚本内的 compose 组合，非裸 compose up）
cd /data/soffy/projects/veya
docker compose --env-file deploy/.env \
  --env-file /home/soffy/.config/veya/backend.interp.env \
  -p veya -f deploy/docker-compose.yml \
  -f /home/soffy/.config/veya/backend-override.yml \
  -f deploy/resources.override.yml -f deploy/tnw-tailnet.override.yml \
  up -d --no-build --force-recreate backend
```

回滚后确认 `curl -s http://127.0.0.1:8767/health` 的 `version` 回到
`0.6.0-cb03ebfe`。

部署脚本自己保存的回滚现场：

- 状态文件：`/home/soffy/veya-fix-1017/p0-3-immutable/last-deploy.env`
- 旧容器 inspect：`/home/soffy/veya-fix-1017/p0-3-immutable/veya-backend-inspect-20261022T120509Z.json`
- override 备份：`/home/soffy/veya-fix-1017/p0-3-immutable/backend-override.yml.deploy-20261022T120509Z`
- 解析后的 compose：`/home/soffy/veya-fix-1017/p0-3-immutable/docker-compose-resolved-20261022T120509Z.yml`

前端如需一并回滚：`git checkout cb03ebfe -- apps/web && pnpm --dir apps/web build
&& sudo systemctl restart veya-web`。

---

## 八、提交与推送

- 远端：`origin/main` 已从 `cb03ebfe` 推进到 `9172671f`（`git push` 成功）。
- 镜像与线上 `/health` 的 `VEYA_BUILD_SHA` 是 `9172671f`，即代码合并完成的那个
  commit。其后的 `main` 提交（`4c6897af` 与本报告自身的提交）只改 `docs/`，
  不进镜像，所以线上运行时 sha 与它们无关；这是预期状态，不是漏部署。
- 本轮未 bump 版本号；`/health` 版本串由 `VEYA_BUILD_SHA` 派生。
- 报告中不含任何 token、密钥或密码明文。
