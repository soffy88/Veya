# DISCOVERY — Veya Loop A+C 接入勘察与验收记录

> CC 规格执行记录 · 方案 A（veya-loop 库嵌入）+ 方案 C（pytest 独立沙箱容器）

## 1. 主仓勘察结果（CC §1）

| 项 | 勘察结果 |
|---|---|
| **主服务入口** | `server/app.py`（FastAPI, uvicorn）；`cli/simple_cli.py`（CLI） |
| **代码生成入口** | `cli/simple_cli.py:158 handle_generate`（CLI 生成命令，演示生成器）；`server/coordinator.py`（LLM 主脑工具链：patch_file/write_file/run_in_sandbox） |
| **已有 Docker** | 是 — `deploy/Dockerfile.backend` + `docker-compose.yml`；Docker 29.6.2 可用 |
| **任务队列** | 无独立队列；APScheduler（Automata 守护进程）+ asyncio 后台任务 |
| **Python 版本** | 3.14.4（沙箱容器固定 3.12-slim，镜像内自含依赖） |
| **现有测试** | `tests/`（559 项主仓回归）+ `veya_loop/tests/`（72 项） |
| **veya-loop 依赖** | 已 `pip install -e ./veya_loop`（venv 内 veya_loop 0.5.0） |

**接线点决策**：`cli/simple_cli.py:handle_generate`（最小 diff）。
- 开关：`CODE_RELIABILITY_LOOP=1` 或 `--reliable` → 可靠性闭环；否则旧 generate-only 路径（回滚，CC §9）。
- 演示生成器 `_sample_generate` 作为可注入 veya_generate（真实 LLM 生成按 CC §5 契约替换：`spec/workspace/failure_context/tests` 关键字）。

## 2. 实现清单

| 文件 | 内容 |
|---|---|
| `platform/3O/omodul/omodul/code_reliability_loop.py` | Loop 闭环事务（主库）：CodeTask/TestResult/FailureSignature/PatchArtifact/CodeLoopResult + run_code_reliability_loop（spec 门禁 → 生成 → 沙箱测试 → 修复轮 ≤max_repairs → merged/clarify/aborted；审计 JSONL） |
| `veya_loop/omodul/code_reliability_loop.py` | 单一来源转发（规格 import 路径） |
| `infra/code_sandbox/Dockerfile` | python:3.12-slim + sandbox 用户 + pytest；`/work` 属 sandbox |
| `infra/code_sandbox/run_tests.py` | stdin JSON → stdout JSON 协议；路径穿越拒绝；超时捕获；junitxml 解析（版本无关） |
| `infra/code_sandbox/entrypoint.sh` | 显式入口文档 |
| `services/code_sandbox_client.py` | Docker 后端（无外网/内存/CPU/只读/tmpfs uid）+ local 回退后端 |
| `services/code_agent_reliability.py` | adapt_veya_generate / make_test_fn / run_veya_code_agent |
| `cli/simple_cli.py` | `--reliable` + `CODE_RELIABILITY_LOOP` 开关接线 |
| `veya_loop/tests/test_code_reliability_loop.py` | 7 项单测（CC §6.1） |

## 3. 验收结果（CC §6）

### 6.1 单元 — 7/7 通过
```
test_code_reliability_loop.py ............... 7 passed
覆盖: merged_candidate / 修复轮带 failure_context / aborted 超预算 /
      clarify 低规格 / timeout 签名 / env_error 不崩 / 审计 JSONL
```

### 6.2 沙箱 — 通过
```bash
docker build -t veya-code-sandbox:latest infra/code_sandbox   # ✓
# 通过/失败/超时/多文件 4 场景全过; failed_nodeids 精确到 nodeid
```

### 6.3 闭环集成（stub generate）— 通过
```
trace: ['generate', 'test', 'repair', 'test', 'merged_candidate']
首轮故意错 → 修复轮带 failure_context → 修对 → merged_candidate
```

### 6.4 产品行为 — 4/4 通过
| 场景 | 结果 |
|---|---|
| 测试全过 | `merged_candidate` + patch ✓ |
| 规格质量低 (0.3) | `clarify`，无补丁 ✓ |
| 连续失败超 max_repairs=2 | `aborted` + signature(fingerprint) + trace ✓ |
| 沙箱超时 | signature.kind=timeout，主进程不崩 ✓ |

### CLI 冒烟
```bash
CODE_RELIABILITY_LOOP=1 python -m cli.simple_cli generate "return 42" --reliable
  → merged_candidate (trace: generate→test→repair→test→merged)
python -m cli.simple_cli generate "hello"   # 回滚开关 → 旧 generate-only 路径 ✓
```

## 4. 风险与已知边界（CC §7 PR 附注）

1. **沙箱无外网**（`--network=none`）：测试依赖第三方包时需在镜像内预装；镜像构建需外网（pip）。
2. **`pytest-json-report` 兼容性坑**：1.5.0 在 pytest 9.1 容器内不产 report → 已改用**内建 junitxml**（零插件依赖），规格 §3.3 "或解析 pytest 输出" 路径。
3. **pytest 9.1 容器内 `-q` 吞 summary 行**：计数不依赖 stdout，以 junitxml 为准。
4. **tmpfs 所有权**：`--tmpfs ... uid=10001` 必须与镜像 `USER sandbox` 对齐，否则 /work 写入 PermissionError。
5. **max_repairs 硬限制**：`CODE_RELIABILITY_MAX_REPAIRS`（默认 3）；aborted 后不自动重开（规格 §4.2 禁止）。
6. **镜像版本**：pytest 未锁版本（Dockerfile `pip install pytest`）→ 建议 CI 锁 `pytest==9.1.1`。

## 5. 回滚（CC §9）

- `CODE_RELIABILITY_LOOP=0`（默认）→ 旧 generate-only 路径。
- 沙箱镜像异常时 client 返回 `env_error` → Loop 走 clarify/abort，不阻塞 Veya 主进程（测试 `test_sandbox_raising_test_fn_does_not_crash` 守护）。
