"""img2threejs 技能 — 图片 → 纯代码 procedural Three.js 模型。

装配 3O 主库原语 (oprim) + forge 脚本, 驱动分阶段雕刻管线:

    action=init   初始化状态机 (setup/pass/final 步骤 + 硬停止循环上限)
    action=status 当前状态: 当前步骤 / pass / 循环计数 / 下一步命令 / 待办
    action=run    执行当前步骤的确定性脚本 (白名单映射, 不 exec 任意 shell)
    action=mark   标记步骤完成 (必须带 evidence) / 跳过 (必须带 reason)
    action=gate   像素级确定性视觉门 (silhouette IoU/比例/颜色 ΔE)
    action=review VLM 采样共识门 (sampler 注入 veya 视觉档)
    action=html   把当前 factory 产出为浏览器可预览的 three.js HTML
                  (前端 artifact type=threejs 直接渲染)

状态文件 .img2threejs/state.json 是跨会话可恢复索引; 渲染图/spec/审查历史
仍是权威工件。主库原语: oprim.sculpt_pipeline / silhouette_gate / vlm_consensus。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

try:  # 技能在 veya 装配进程内运行 (oprim 已在 sys.path)
    from oprim import (  # 3O 主库原语 (veya.platform 装配层)
        SculptPipelineError,
        load_pipeline_state,
        new_pipeline_state,
        pipeline_mark,
        pipeline_status,
        run_silhouette_gate,
        run_vlm_consensus,
        save_pipeline_state,
    )
except ImportError:  # 独立运行 (开发/测试): 注入 platform/3O 候选路径
    for _cand in (
        Path(__file__).resolve().parents[3] / "platform" / "3O" / "oprim",
        Path(__file__).resolve().parents[3] / "platform" / "3O",
    ):
        if _cand.is_dir() and str(_cand) not in sys.path:
            sys.path.insert(0, str(_cand))
    from oprim import (
        SculptPipelineError,
        load_pipeline_state,
        new_pipeline_state,
        pipeline_mark,
        pipeline_status,
        run_silhouette_gate,
        run_vlm_consensus,
        save_pipeline_state,
    )

_HERE = Path(__file__).resolve().parent
_FORGE = _HERE / "forge"
_STATE_REL = Path(".img2threejs") / "state.json"

# 白名单: step_id → (forge 脚本, 参数模板) — 不 exec 任意 shell
_WHITELIST: dict[str, tuple[str, list[str]]] = {
    "reference-admission": ("stage1_intake/check_reference_admission.py", ["{reference}"]),
    "pre-spec-assessment": (
        "stage2_spec/new_pre_spec_assessment.py",
        ["{subject}", "--image", "{reference}", "--out", "assessment.json"],
    ),
    "detail-inventory": (
        "stage1_intake/build_detail_inventory.py",
        ["{reference}", "--mode", "grid-3x3", "--out-dir", "detail-inventory", "--out", "di.json"],
    ),
    "spec-authoring": (
        "stage2_spec/new_sculpt_spec.py",
        ["{subject}", "--image", "{reference}", "--assessment", "assessment.json", "--out", "object-sculpt-spec.json"],
    ),
    "strict-validation": (
        "stage2_spec/validate_sculpt_spec.py",
        ["object-sculpt-spec.json", "--strict-quality"],
    ),
    "build-current-pass": (
        "stage3_build/generate_threejs_factory.py",
        ["object-sculpt-spec.json", "--out", "src/createObjectModel.ts", "--pass-id", "{pass_id}"],
    ),
    "part-coverage": ("stage4_review/check_part_coverage.py", ["--spec", "object-sculpt-spec.json", "--manifest", "parts.json"]),
}

# forge CLI 可直接调用的工具 (run 之外)
_CLI_TOOLS: dict[str, list[str]] = {
    "diagnose": ["stage4_review/diagnose_render.py", "--reference", "{reference}", "--render", "{render}"],
    "tier1": ["stage4_review/diagnose_render.py", "--reference", "{reference}", "--render", "{render}"],
}


def _state_path(workdir: str | None) -> Path:
    base = Path(workdir).expanduser() if workdir else _HERE
    return base / _STATE_REL


def _run_forge(script: str, args: list[str], workdir: Path) -> dict[str, Any]:
    """在技能 forge 内执行白名单脚本 (cwd=workdir), 返回结果摘要。"""
    script_path = _FORGE / script
    if not script_path.is_file():
        return {"ok": False, "error": f"forge script missing: {script}"}
    cmd = [sys.executable, str(script_path), *args]
    try:
        proc = subprocess.run(
            cmd, cwd=workdir, capture_output=True, text=True, timeout=120
        )
        tail = (proc.stdout or proc.stderr).strip().splitlines()[-8:]
        return {"ok": proc.returncode == 0, "exitCode": proc.returncode, "output": "\n".join(tail)}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "forge script timed out (120s)"}


def _init_state(reference: str, goal: str, profile: str, workdir: str | None) -> dict[str, Any]:
    st = _state_path(workdir)
    if st.exists():
        state = load_pipeline_state(st)
        return {"ok": True, "note": "状态已存在 (拒绝覆盖), 返回现有状态", **pipeline_status(state)}
    setup = [
        ("image-analysis", "Read grimoire/intake/image_analysis.md and analyze {reference}"),
        ("reference-suitability", "Read grimoire/intake/validation_rubric.md and record a pass/conditional/reject verdict for {reference}"),
        ("reference-admission", "python3 forge/stage1_intake/check_reference_admission.py {reference}"),
        ("pre-spec-assessment", "python3 forge/stage2_spec/new_pre_spec_assessment.py \"<name>\" --image {reference} --out assessment.json"),
        ("detail-inventory", "python3 forge/stage1_intake/build_detail_inventory.py {reference} --mode grid-3x3 --out-dir detail-inventory --out di.json"),
        ("spec-authoring", "python3 forge/stage2_spec/new_sculpt_spec.py \"<name>\" --image {reference} --assessment assessment.json --out object-sculpt-spec.json"),
        ("strict-validation", "python3 forge/stage2_spec/validate_sculpt_spec.py object-sculpt-spec.json --strict-quality"),
    ]
    passes = [
        ("build-current-pass", "python3 forge/stage3_build/generate_threejs_factory.py object-sculpt-spec.json --out src/createObjectModel.ts --pass-id {pass_id}"),
        ("render-capture", "Render {pass_id} and capture the fixed review view plus orbit views"),
        ("tier1-diagnostics", "python3 forge/stage4_review/diagnose_render.py --reference {reference} --render <shot> --pass-id {pass_id}"),
        ("pass-gate-check", "python3 forge/stage3_build/orchestrate_passes.py check object-sculpt-spec.json --pass-id {pass_id}"),
        ("ai-review-recorded", "Create the comparison sheet, inspect with agent vision, and append one review action"),
    ]
    final = [
        ("part-coverage", "python3 forge/stage4_review/check_part_coverage.py --spec object-sculpt-spec.json --manifest parts.json"),
        ("action-ready", "Verify explodable/clickable hierarchy, pivots, sockets, and root.userData.sculptRuntime"),
    ]
    state = new_pipeline_state(
        reference, setup, passes, final,
        profile=profile, spec="object-sculpt-spec.json",
        max_per_pass=3, max_total=6,
        meta={"goal": goal},
    )
    save_pipeline_state(st, state)
    return {"ok": True, "stateFile": str(st), **pipeline_status(state)}


def _run_step(workdir: str | None) -> dict[str, Any]:
    st = _state_path(workdir)
    state = load_pipeline_state(st)
    step = state.get("currentStep")
    entry = next((e for e in state["checklist"] if e["id"] == step), None)
    if entry is None or entry["scope"] not in ("setup", "pass", "final"):
        return {"ok": False, "error": f"当前步骤 {step} 无白名单执行器 (需 agent 视觉/判断, 见 nextCommand)"}
    spec = _WHITELIST.get(step)
    if spec is None:
        return {
            "ok": False,
            "error": f"步骤 '{step}' 需要 agent 判断 (图片分析/视觉对比/渲染截图), 非确定性脚本; 按 nextCommand 执行后 mark",
            "nextCommand": pipeline_status(state).get("nextCommand"),
        }
    script, args = spec
    filled = [a.format(
        reference=str(state["artifacts"]["reference"]),
        spec=str(state["artifacts"].get("spec") or "object-sculpt-spec.json"),
        pass_id=str(state.get("currentPass") or "blockout"),
        subject=str(state.get("meta", {}).get("goal", "subject"))[:40],
    ) for a in args]
    work = (Path(workdir).expanduser() if workdir else _HERE)
    result = _run_forge(script, filled, work)
    result["step"] = step
    result["status"] = pipeline_status(state)
    return result


def _gate(reference: str, render: str, workdir: str | None, spec_path: str | None = None) -> dict[str, Any]:
    """Tier-1 确定性视觉门 (主库原语): 渲染截图 vs 参考图。"""
    spec = None
    if spec_path:
        p = Path(spec_path).expanduser()
        if p.is_file():
            try:
                spec = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                spec = None
    result = run_silhouette_gate(reference, render, spec=spec)
    return {"gate": "silhouette", **result}


def _review(
    reference: str,
    render: str,
    workdir: str | None,
    *,
    n_samples: int = 3,
    claimed_class: str | None = None,
    vlm_sampler: Any = None,
) -> dict[str, Any]:
    """VLM 采样共识门 (主库原语): 确定性硬门先跑, 通过后才邀请模型意见。"""
    hard = run_silhouette_gate(reference, render)
    if not hard["passed"]:
        return {
            "gate": "vlm-consensus",
            "skipped": True,
            "reason": "确定性硬门未过 — VLM 不被邀请 (几何损坏问模型只会得到自信的错误)",
            "silhouette": hard,
        }
    if vlm_sampler is None:
        # 默认 sampler: 提示调用方提供视觉 (veya 视觉档或主脑看图)
        return {
            "gate": "vlm-consensus",
            "skipped": True,
            "reason": "需要 vlm_sampler (veya 视觉档 qwen3.7-flash 或主脑看图打分), 未注入",
            "silhouette": hard,
        }
    decision = run_vlm_consensus(
        vlm_sampler, n_samples=n_samples, claimed_class=claimed_class
    )
    return {"gate": "vlm-consensus", **decision, "silhouette": hard}


def _html(workdir: str | None) -> dict[str, Any]:
    """把当前 factory (src/createObjectModel.ts) 产出为可预览 HTML。

    three.js 通过 CDN 注入, factory TS 需先由 agent 编译为 JS (或本函数对
    无类型标注的简单 factory 直接剥离类型), 产出 index.html 供前端
    artifact type=threejs 渲染。
    """
    work = (Path(workdir).expanduser() if workdir else _HERE)
    ts = work / "src" / "createObjectModel.ts"
    if not ts.is_file():
        return {"ok": False, "error": f"factory 不存在: {ts} (先 run build-current-pass)"}
    js = _strip_types(ts.read_text(encoding="utf-8")) if ts.suffix == ".ts" else ts.read_text(encoding="utf-8")
    html = _WRAPPER.replace("__MODEL_JS__", js)
    out = work / "index.html"
    out.write_text(html, encoding="utf-8")
    return {"ok": True, "htmlPath": str(out), "html": html}


def _strip_types(ts: str) -> str:
    """极简 TS→JS: 去类型标注 (类型标注/接口/泛型)。生产编译建议由 agent 用
    esbuild/tsc 完成; 这里仅兜底让简单 factory 可预览。"""
    import re

    js = re.sub(r"interface\s+\w+\s*\{[^}]*\}", "", ts)
    js = re.sub(r":\s*[A-Za-z_][\w<>\[\]|, ?]*", "", js)
    js = re.sub(r"\bas\s+[A-Za-z_][\w<>]*\b", "", js)
    return js


_WRAPPER = """<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8"><title>img2threejs preview</title>
<style>body{margin:0;background:#111;overflow:hidden}canvas{display:block}#info{position:fixed;bottom:8px;left:8px;color:#888;font:12px monospace}</style>
</head>
<body>
<div id="info">img2threejs — 拖拽旋转 / 滚轮缩放</div>
<script src="https://cdn.jsdelivr.net/npm/three@0.147.0/build/three.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/three@0.147.0/examples/js/controls/OrbitControls.js"></script>
<script>
const scene = new THREE.Scene();
const camera = new THREE.PerspectiveCamera(45, innerWidth/innerHeight, 0.1, 1000);
camera.position.set(4, 3, 6); camera.lookAt(0, 0, 0);
const renderer = new THREE.WebGLRenderer({antialias:true});
renderer.setSize(innerWidth, innerHeight); document.body.appendChild(renderer.domElement);
scene.add(new THREE.AmbientLight(0xffffff, 0.6));
const key = new THREE.DirectionalLight(0xffffff, 0.9); key.position.set(5, 8, 4); scene.add(key);
const controls = new THREE.OrbitControls(camera, renderer.domElement);
const root = new THREE.Group();
try { __MODEL_JS__ } catch (e) { document.getElementById('info').textContent = 'factory error: ' + e.message; }
if (window.sculptModel) { root.add(window.sculptModel); }
scene.add(root);
(function loop() { requestAnimationFrame(loop); controls.update(); renderer.render(scene, camera); })();
</script>
</body>
</html>"""


def main(
    reference: str,
    goal: str = "",
    action: str = "status",
    profile: str = "generic",
    workdir: str | None = None,
    render: str = "",
    spec_path: str | None = None,
    evidence: str = "",
    step_id: str = "",
    n_samples: int = 3,
    claimed_class: str | None = None,
    vlm_sampler: Any = None,
    **_: Any,
) -> dict[str, Any]:
    """技能入口: 状态机驱动的图片→3D 雕刻管线。"""
    if not reference:
        return {"ok": False, "error": "reference (参考图片路径) 必填"}
    ref = Path(reference).expanduser()
    if action in ("init", "gate", "review") and not ref.is_file():
        return {"ok": False, "error": f"参考图片不存在: {ref}"}
    try:
        if action == "init":
            return _init_state(str(ref), goal, profile, workdir)
        if action == "status":
            st = _state_path(workdir)
            if not st.exists():
                return {"ok": False, "error": f"状态不存在: {st} (先 action=init)"}
            return {"ok": True, **pipeline_status(load_pipeline_state(st))}
        if action == "run":
            return _run_step(workdir)
        if action == "mark":
            if not step_id:
                return {"ok": False, "error": "mark 需要 step_id"}
            st = _state_path(workdir)
            state = load_pipeline_state(st)
            kwargs: dict[str, Any] = {}
            if evidence:
                kwargs["evidence"] = [evidence]
            if kwargs.get("evidence") is None and "skip" in (evidence or ""):
                kwargs["status"] = "skipped"
                kwargs["reason"] = evidence
            pipeline_mark(state, step_id, **kwargs)
            save_pipeline_state(st, state)
            return {"ok": True, **pipeline_status(state)}
        if action == "gate":
            if not render:
                return {"ok": False, "error": "gate 需要 render (渲染截图路径)"}
            return _gate(str(ref), render, workdir, spec_path)
        if action == "review":
            if not render:
                return {"ok": False, "error": "review 需要 render (渲染截图路径)"}
            return _review(str(ref), render, workdir, n_samples=n_samples, claimed_class=claimed_class, vlm_sampler=vlm_sampler)
        if action == "html":
            return _html(workdir)
        return {"ok": False, "error": f"未知 action: {action}"}
    except SculptPipelineError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:  # 技能返回必须兜底
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


if __name__ == "__main__":
    print(json.dumps(main(**dict(a.split("=", 1) for a in sys.argv[1:])), ensure_ascii=False, indent=2))
