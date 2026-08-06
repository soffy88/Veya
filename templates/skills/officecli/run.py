"""officecli 技能包 — Office 文档生产 (docx/xlsx/pptx)。

3O 范式: 本技能是业务装配 (veya 主仓 skills 层), 二进制能力由 officecli 提供。

安全边界 (零信任):
  - 写操作 (add/edit/merge/batch) 路径白名单: workspace + ~/.veya/templates/
  - 读操作 (read/dump/convert) readonly, 免审批
  - 每次文档变更写审计 (~/.veya/audit/officecli.jsonl)
  - 凭证不进文档模板 (模板数据过上层 redact hook)
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any

ALL_OPS = ("add", "edit", "read", "convert", "merge", "dump", "batch", "render", "watch")
READONLY_OPS = ("read", "dump", "convert")
WRITE_OPS = tuple(o for o in ALL_OPS if o not in READONLY_OPS)

HOME = Path.home()
TEMPLATES_DIR = HOME / ".veya" / "templates"
AUDIT_DIR = HOME / ".veya" / "audit"
WORKSPACE = Path(os.environ.get("VEYA_WORKSPACE", str(Path.cwd())))


def _allowed_write_roots() -> list[Path]:
    return [WORKSPACE.resolve(), TEMPLATES_DIR.resolve()]


def _check_write_path(path: str | Path, op: str) -> Path:
    """写操作路径白名单校验 (零信任: 白名单外拒绝)。"""
    p = Path(path).resolve()
    for root in _allowed_write_roots():
        if p == root or root in p.parents:
            return p
    raise PermissionError(
        f"officecli[{op}] 写路径被拒: {p} (仅允许 {[str(r) for r in _allowed_write_roots()]})"
    )


def _audit(op: str, *, input_path: str, output_path: str, exit_code: int,
           detail: str = "") -> None:
    """文档变更审计 (post_result hook 等价物, 追加 JSONL)。"""
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    entry = {
        "ts": time.time(), "op": op, "input": input_path, "output": output_path,
        "exit_code": exit_code, "detail": detail[:500],
        "audit_id": f"office_{uuid.uuid4().hex[:12]}",
    }
    with open(AUDIT_DIR / "officecli.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def render_template(template: str | Path, data: dict[str, Any]) -> str:
    """模板渲染: {{key}} 占位符替换 (文档模板联动)。"""
    text = Path(template).read_text(encoding="utf-8") if Path(template).exists() else template
    for key, value in data.items():
        text = text.replace("{{" + key + "}}", str(value))
    return text


def main(op: str, input: str = "", output: str = "", data_json: str = "",
         options: dict[str, Any] | None = None, **_: Any) -> dict[str, Any]:
    """执行 officecli 操作。op 语义透传 CLI。"""
    if op not in ALL_OPS:
        raise ValueError(f"未知 op: {op}; 可选 {ALL_OPS}")

    binary = shutil.which("officecli")
    if binary is None:
        raise RuntimeError(
            "officecli 未安装。安装 (官方渠道后 sha256 校验入库): "
            "curl -fsSL https://officecli.ai/install.sh | bash  或  brew install officecli / npm i -g officecli"
        )

    # 写操作: 路径白名单
    if op in WRITE_OPS:
        if output:
            _check_write_path(output, op)
        if input and Path(input).exists() and op in ("edit", "merge", "batch"):
            _check_write_path(input, op)

    # 透传 CLI: officecli <op> <input> [--output <output>] [data_json] [options]
    cmd = [binary, op]
    if input:
        cmd.append(input)
    if output:
        cmd += ["--output", output]
    if data_json:
        cmd += ["--data", data_json]
    if options:
        cmd += ["--options", json.dumps(options, ensure_ascii=False)]

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    except subprocess.TimeoutExpired:
        _audit(op, input_path=input, output_path=output, exit_code=-1, detail="timeout")
        return {"ok": False, "op": op, "error": "officecli 超时 (600s)"}

    _audit(op, input_path=input, output_path=output, exit_code=proc.returncode,
           detail=proc.stderr[-300:])
    if proc.returncode != 0:
        return {"ok": False, "op": op, "error": proc.stderr[-2000:] or f"exit={proc.returncode}"}
    return {
        "ok": True, "op": op,
        "stdout": proc.stdout[:4000],
        "output_path": output or None,
        "readonly": op in READONLY_OPS,
    }
