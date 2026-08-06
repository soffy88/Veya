"""comfyui_provider — SWITCH_PROVIDER 动作的真实 AI 视频 provider 预留。

闭环六动作 (REGENERATE/ADJUST_PROMPT/SWITCH_PROVIDER/NARROW_CLIP/CLARIFY/ABORT)
中, SWITCH_PROVIDER 目前只有映射无实现。本模块把 ComfyUI (MiniMax H3
音视频模型) 定义为可切换的目标 provider:

    hevi-lite (HTML→录屏→TTS, 零 GPU)  ←当前默认
    comfyui   (MiniMax H3 原生采样)     ←本模块 (需 ≥24GB 显存)

契约检查借鉴 ComfyUI-Spectrum-MiniMax-H3 节点: 应用/切换时显式校验
模型与采样器契约, 不兼容 → 明确的 ContractError (而非隐式失败), 使
闭环的 failure signature 可读 (kind=ENV → CLARIFY/ABORT, 不烧返工预算)。
"""

from __future__ import annotations

import json
import shutil
import subprocess
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class ProviderContractError(RuntimeError):
    """provider 契约不满足 (模型缺失/显存不足/端点不符) → ENV 签名。"""


@dataclass
class ComfyUIProvider:
    """ComfyUI 视频生成 provider (MiniMax H3 音视频模型)。

    不加载模型: 仅做契约检查 + 提交采样请求 (HTTP, 兼容 veya
    generate_fn 契约: prompt + failure_context -> 本地视频路径)。
    """

    base_url: str = "http://127.0.0.1:8188"          # ComfyUI 默认端口
    workflow_template: Path | None = None          # JSON workflow 模板
    min_vram_gb: int = 24                             # MiniMax H3 硬件门槛
    timeout_s: float = 1800.0

    # 采样参数 (Spectrum 加速节点可叠加, 见 repo ComfyUI-Spectrum-MiniMax-H3)
    sampler: str = "euler"
    steps: int = 28
    spectrum_enabled: bool = False                    # 加速路径 (轨迹 A/B 有差异)
    spectrum_args: dict[str, Any] = field(default_factory=dict)

    # ── 契约检查 (借鉴 H3 节点: 应用时显式校验, 不兼容→ContractError) ──
    def check_contract(self) -> None:
        """切换前硬校验: 可执行性契约, 不满足抛 ProviderContractError。"""
        if not shutil.which("nvidia-smi"):
            raise ProviderContractError("无 NVIDIA GPU (nvidia-smi 不存在)")
        try:
            out = subprocess.run(
                ["nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=30,
            )
            vram_mb = int(out.stdout.strip().splitlines()[0].strip())
        except Exception as exc:
            raise ProviderContractError(f"nvidia-smi 查询失败: {exc}") from exc
        if vram_mb < self.min_vram_gb * 1024:
            raise ProviderContractError(
                f"MiniMax H3 需 ≥{self.min_vram_gb}GB 显存, 当前 {vram_mb // 1024}GB"
            )
        # 端点可达性
        try:
            with urllib.request.urlopen(f"{self.base_url}/system_stats",
                                        timeout=5) as resp:
                stats = json.loads(resp.read().decode())
        except Exception as exc:
            raise ProviderContractError(f"ComfyUI 端点不可达: {exc}") from exc
        if self.spectrum_enabled and "spectrum" not in str(stats.get("system", {})):
            # Spectrum 加速需要精确的 MiniMax H3 原生采样 API (契约匹配)。
            raise ProviderContractError(
                "Spectrum 加速要求 MiniMax H3 原生采样 API (ComfyUI 特定 commit)"
            )
        return None

    def generate(self, prompt: str, spec: Any,
                 failure_context: dict[str, Any] | None = None) -> Path:
        """提交 ComfyUI 采样 → 下载产物 → 本地视频路径。

        failure_context.preferred_action == "SWITCH_PROVIDER" 时, 首次切换
        即做 contract check; 检查失败 → ContractError → 闭环转 ENV 签名
        (CLARIFY/ABORT, 不烧返工预算)。
        """
        failure_context = failure_context or {}
        self.check_contract()
        if self.workflow_template is None:
            raise ProviderContractError(
                "workflow_template 未配置: 请提供 MiniMax H3 workflow JSON"
            )
        workflow = json.loads(self.workflow_template.read_text())
        # 注入 prompt / 画幅 (aspect → width/height) / Spectrum 加速参数
        wf = self._inject(workflow, prompt, spec, failure_context)
        # POST /prompt (ComfyUI 原生 API)
        req = urllib.request.Request(
            f"{self.base_url}/prompt",
            data=json.dumps({"prompt": wf}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                queued = json.loads(resp.read().decode())
        except Exception as exc:
            raise ProviderContractError(f"ComfyUI 提交失败: {exc}") from exc
        prompt_id = queued.get("prompt_id")
        if not prompt_id:
            raise ProviderContractError(f"ComfyUI 未返回 prompt_id: {queued}")
        # 轮询 history 直到完成 → 取首个视频产物
        video = self._wait_video(prompt_id)
        return Path(video)

    # ── 内部 ──────────────────────────────────────────────────────────
    def _inject(self, workflow: dict[str, Any], prompt: str, spec: Any,
                failure_context: dict[str, Any]) -> dict[str, Any]:
        """把 prompt/画幅/加速参数注入 workflow 节点 (按 class_type 匹配)。"""
        width, height = _aspect_size(spec)
        for node in workflow.values():
            ct = node.get("class_type", "")
            inputs = node.setdefault("inputs", {})
            if ct == "CLIPTextEncode" and "text" in inputs and inputs.get("text") in ("", "prompt"):
                inputs["text"] = prompt
            if ct in ("EmptyLatentImage", "EmptySD3LatentImage"):
                inputs["width"] = width
                inputs["height"] = height
        if self.spectrum_enabled:
            # Spectrum Apply MiniMax H3 节点 (sampling/spectrum)
            workflow["spectrum"] = {
                "class_type": "SpectrumApplyMiniMaxH3",
                "inputs": {"enabled": True, **self.spectrum_args},
            }
        return workflow

    def _wait_video(self, prompt_id: str) -> str:
        import time

        deadline = time.time() + self.timeout_s
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(
                    f"{self.base_url}/history/{prompt_id}", timeout=10
                ) as resp:
                    history = json.loads(resp.read().decode())
            except Exception:
                time.sleep(5)
                continue
            entry = history.get(prompt_id)
            if not entry:
                time.sleep(5)
                continue
            status = entry.get("status", {})
            if status.get("status_str") == "error":
                raise ProviderContractError(
                    f"ComfyUI 采样失败: {status.get('messages', [])[-3:]}"
                )
            if status.get("completed") is True:
                outputs = entry.get("outputs", {})
                for out in outputs.values():
                    for img in out.get("images", []):
                        if img.get("type") == "output":
                            return self._download(img["filename"])
                # 无图像输出 → 可能只写了视频到 output 目录, 尝试 VHS 节点路径
                raise ProviderContractError(
                    "ComfyUI 完成但未找到视频产物 (检查 workflow 输出节点)"
                )
            time.sleep(5)
        raise ProviderContractError(f"ComfyUI 采样超时 ({self.timeout_s}s)")

    def _download(self, filename: str) -> str:
        local = Path("/tmp/comfyui_out") / filename
        local.parent.mkdir(parents=True, exist_ok=True)
        urllib.request.urlretrieve(
            f"{self.base_url}/view?filename={filename}&type=output", local
        )
        return str(local)


def _aspect_size(spec: Any) -> tuple[int, int]:
    ratios = getattr(spec, "aspect_ratios", None) or ["16:9"]
    ratio = ratios[0]
    if ratio == "9:16":
        return 768, 1344
    if ratio == "1:1":
        return 1024, 1024
    return 1344, 768


__all__ = ["ComfyUIProvider", "ProviderContractError"]
