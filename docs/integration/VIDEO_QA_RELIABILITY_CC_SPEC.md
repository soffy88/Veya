# VIDEO QA RELIABILITY — CC 可执行规格

> 视频生产服务出片后**不能直接上线**时, 走:
> `生成 → 沙箱质检 → 失败签名 → 有限次返工动作 → 通过|澄清|中止`
> 与代码 Agent 闭环 (`code_reliability_loop`) 同构; Veya Loop 只做编排与门禁,
> 不负责提升生成模型画质。**禁止无预算无限重生成; 本阶段禁止自动公开发布。**

## 0. 架构

```
视频生产服务
  import veya_loop.omodul.video_reliability_loop
  generate_fn  → hevi / 可灵 / 即梦 / 本地管线 (先 stub 再接)
  evaluate_fn  → VideoSandboxClient
        │  docker run --network=none (或 LocalVideoEvaluator)
        ▼
  veya-video-sandbox (infra/video_sandbox)
        ffprobe + 规则质检 → VideoEvalResult JSON
```

| 层 | 部署 |
|----|------|
| 可靠性闭环 | 库嵌入主服务 (`veya_loop.omodul.video_reliability_loop`) |
| 质检执行 | 独立沙箱容器 (`--network=none`, 只读挂载); dev 用 `LocalVideoEvaluator` |

## 1. 仓库落点

```text
infra/video_sandbox/
  Dockerfile          # python:3.12-slim + ffmpeg + orjson, 只读无网络
  evaluate.py         # stdin JSON → stdout VideoEvalResult
veya_loop/src/veya_loop/omodul/video_reliability_loop.py   # 控制面 (转发主库)
platform/3O/omodul/omodul/video_reliability_loop.py        # 主库实现 (同构 code)
templates/video_services/
  video_sandbox_client.py      # Docker 客户端 + LocalVideoEvaluator
  video_agent_reliability.py   # run_veya_video_agent + adapt_hevi
docs/integration/VIDEO_QA_RELIABILITY_CC_SPEC.md
veya_loop/tests/test_video_reliability_loop.py
```

## 2. 质检协议 (沙箱 I/O)

### 输入 (stdin JSON)
```json
{"video_path": "/work/input.mp4",
 "spec": {"min_duration_s": 5, "max_duration_s": 60,
          "min_width": 720, "min_height": 720,
          "aspect_ratios": ["9:16", "16:9", "1:1"],
          "require_audio": true, "max_size_mb": 100, "platform": "generic"}}
```

### 输出 (stdout JSON, 对齐 VideoEvalResult)
```json
{"passed": false, "duration_s": 3.2, "width": 1280, "height": 720,
 "fps": 24.0, "has_audio": true, "size_mb": 12.4,
 "issues": [{"code": "DURATION_TOO_SHORT", "message": "3.2s < min 5s",
             "severity": "high"}],
 "metrics": {}, "stderr": ""}
```

### 硬性规则 (v1, 仅 ffprobe + 文件元数据)
| code | 条件 |
|------|------|
| `FILE_MISSING` | 文件不存在或不可读 |
| `DURATION_TOO_SHORT` / `TOO_LONG` | 超出规格时长 |
| `RESOLUTION_LOW` | 宽或高低于下限 |
| `ASPECT_NOT_ALLOWED` | 比例不在白名单 (±0.05) |
| `NO_AUDIO` | `require_audio` 且无音轨 |
| `FILE_TOO_LARGE` | 超过 `max_size_mb` |
| `PROBE_FAILED` | ffprobe 失败 |

v2 可选 (本阶段非必须): 黑帧比例 / 响度 / OCR 违禁 / 美学分 —— 同一 `issues` 扩展。

## 3. 控制面 (video_reliability_loop)

与 `code_reliability_loop` 同构核心类型:
`VideoTask` / `VideoArtifact` / `VideoEvalResult` / `FailureSignature` /
`VideoRepairAction`(REGENERATE | ADJUST_PROMPT | SWITCH_PROVIDER | NARROW_CLIP |
CLARIFY | ABORT) / `run_video_reliability_loop`。

### 失败签名映射
| issue code | FailureKind | 偏好动作 |
|------------|-------------|----------|
| DURATION_* | SPEC_OR_DURATION | ADJUST_PROMPT / CLARIFY |
| RESOLUTION_LOW / ASPECT_* | FORMAT | ADJUST_PROMPT / REGENERATE |
| NO_AUDIO | AUDIO | REGENERATE |
| PROBE_FAILED / FILE_* | ENV | CLARIFY / ABORT |
| (v2) POLICY_* | POLICY | CLARIFY / ABORT |
| 多次同类失败 | — | ABORT |

硬规则: `repairs_used >= max_repairs → ABORT`; 规格矛盾 (`min>max`) → CLARIFY;
沙箱挂 (`SANDBOX_ERROR`) → ENV 签名, 不崩主进程。

## 4. 生成函数约定

```python
def generate_fn(task, signature, parent_artifact) -> VideoArtifact:
    """
    failure_context 来自 signature (时长不够 / 要 9:16 / 无音轨等)
    返回本地路径或可被沙箱访问的路径
    """
```

先 stub (第一次短视频、第二次合格) 验证闭环, 再接 hevi 出片路径。

## 5. 验收

```bash
# 沙箱
ffmpeg -f lavfi -i color=c=blue:s=1280x720:d=3 -y /tmp/short.mp4
#   期望: DURATION_TOO_SHORT → passed=false
ffmpeg -f lavfi -i color=c=blue:s=1280x720:d=8 -f lavfi -i sine=f=440:d=8 -y /tmp/ok.mp4
#   期望: passed=true (spec 允许 16:9 与音频)

# 闭环
pytest veya_loop/tests/test_video_reliability_loop.py -q
```

| 场景 | 期望 |
|------|------|
| 时长过短 | 签名 DURATION_*, 动作 ADJUST/REGENERATE, 有限次后 ABORT |
| 合格片 | success (merged_candidate, 待人工发布) |
| 规格矛盾 (min>max) | CLARIFY |
| 沙箱挂 | ENV 签名, 不崩主进程 |

## 6. 非目标
- 训练/微调 hevi; 完整平台内容安全审核 (v2 挂 API); 自动发布到抖音/B 站;
  用因果 SCM 建模像素。
