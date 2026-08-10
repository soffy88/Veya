"""File tree REST — 工作区文件树 (P3, 借鉴 ccgui 文件树)。

只读端点, 工作区边界 = 容器工作目录 (VEYA_WORKSPACE 或 cwd), 防逃逸。
主脑零改动; 前端 FileTree 点击文件 → 注入 @path → 主脑 read_file 读。

deny-by-default: 只读浏览 + 读文件; 写入走 hicode (隔离执行器)。
"""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request, UploadFile

router = APIRouter(tags=["file-tree"])

_SKIP_DIRS = {".git", ".venv", "node_modules", "__pycache__", ".loopx",
              ".svelte-kit", "venv", "dist", "build", ".ruff_cache",
              ".pytest_cache", "site"}

_MAX_DEPTH = 4
_MAX_ENTRIES = 300
_MAX_READ = 200_000  # 200KB 读上限


def _extract_pdf_text(path: Path, max_chars: int) -> str:
    """PDF → 纯文本 (pypdf; 容器 site-packages 只读 → 从 ~/.veya/pylibs 加载)。"""
    import sys

    pylibs = str(Path.home() / ".veya" / "pylibs")
    if pylibs not in sys.path:
        sys.path.insert(0, pylibs)
    try:
        from pypdf import PdfReader
    except ImportError:
        raise HTTPException(status_code=503, detail="PDF 解析库未安装 (pypdf)")
    try:
        reader = PdfReader(str(path))
        parts: list[str] = []
        total = 0
        for page in reader.pages:
            t = page.extract_text() or ""
            parts.append(t)
            total += len(t)
            if total >= max_chars:
                break
        return "\n".join(parts)[:max_chars]
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"PDF 解析失败: {exc}")


def _workspace_root() -> Path:
    root = os.environ.get("VEYA_WORKSPACE") or os.getcwd()
    return Path(root).resolve()


def _resolve(rel: str) -> Path:
    """工作区内路径解析 (防逃逸: 必须位于根内)。"""
    root = _workspace_root()
    p = Path(rel).expanduser()
    if not p.is_absolute():
        p = root / p
    rp = p.resolve()
    if rp != root and root not in rp.parents:
        raise HTTPException(status_code=403, detail=f"路径在工作区外: {rel}")
    return rp


@router.get("/api/v1/fs/tree")
async def fs_tree(path: str = "", depth: int = _MAX_DEPTH) -> dict:
    """工作区目录树 (受限深度/条目, 噪声目录剔除)。"""
    try:
        root = _resolve(path or ".")
    except HTTPException:
        raise
    if not root.is_dir():
        raise HTTPException(status_code=404, detail=f"目录不存在: {path}")
    depth = max(1, min(int(depth), _MAX_DEPTH))

    def _walk(dirp: Path, d: int) -> list[dict]:
        out: list[dict] = []
        try:
            entries = sorted(dirp.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
        except OSError:
            return out
        for e in entries:
            if len(out) >= _MAX_ENTRIES:
                break
            if e.name.startswith(".") and e.name not in (".github",):
                continue
            if e.is_dir():
                if e.name in _SKIP_DIRS:
                    continue
                node: dict = {"name": e.name, "type": "dir", "path": str(e.relative_to(_workspace_root()))}
                if d > 0:
                    node["children"] = _walk(e, d - 1)
                out.append(node)
            elif e.is_file():
                try:
                    size = e.stat().st_size
                except OSError:
                    continue
                if size > 5_000_000:
                    continue  # >5MB 文件不列 (避免大文件/二进制噪音)
                out.append({"name": e.name, "type": "file",
                            "path": str(e.relative_to(_workspace_root())),
                            "size": size})
        return out

    return {"root": str(_workspace_root()), "entries": _walk(root, depth - 1)}


@router.get("/api/v1/fs/read")
async def fs_read(path: str, max_chars: int = 12000) -> dict:
    """读工作区内文件内容 (UTF-8, 截断防爆)。uploads/ 前缀 → ~/.veya/uploads (上传目录)。"""
    if str(path).startswith("uploads/"):
        # 上传目录在 ~/.veya/uploads (veya-data rw), 非工作区根
        root = Path.home() / ".veya"
        rp = (root / path).resolve()
        if root not in rp.parents and rp != root:
            raise HTTPException(status_code=403, detail=f"路径在工作区外: {path}")
    else:
        rp = _resolve(path)
    if not rp.is_file():
        raise HTTPException(status_code=404, detail=f"文件不存在: {path}")
    # PDF → 提取文本 (pypdf, 轻量; 不受 200KB 二进制限制 — 提取时截断)
    if rp.suffix.lower() == ".pdf":
        try:
            size = rp.stat().st_size
        except OSError as exc:
            raise HTTPException(status_code=500, detail=f"stat 失败: {exc}")
        text = _extract_pdf_text(rp, max_chars)
        return {"path": str(rp), "content": text, "truncated": len(text) >= max_chars,
                "size": size, "kind": "pdf"}
    try:
        size = rp.stat().st_size
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"stat 失败: {exc}")
    if size > _MAX_READ:
        raise HTTPException(status_code=413, detail=f"文件过大 ({size} bytes), 上限 {_MAX_READ}")
        text = _extract_pdf_text(rp, max_chars)
        return {"path": str(rp), "content": text, "truncated": len(text) >= max_chars,
                "size": size, "kind": "pdf"}

    try:
        text = rp.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"读取失败: {exc}")
    truncated = len(text) > max_chars
    return {"path": str(rp), "content": text[:max_chars], "truncated": truncated,
            "size": size}


@router.post("/api/v1/fs/upload")
async def fs_upload(request: Request, name: str = "upload.bin") -> dict:
    """大文件上传到工作区 .veya-uploads/ (≤100MB), 返回引用路径供 @path 读取。

    文本类大文件不直接注入消息 (撑爆上下文) — 存工作区, 消息放 @uploads/<name>
    引用, 模型用 read_file/long_read 按需读取。
    传输: application/octet-stream 原始 body + x-file-name 头 (绕过 SvelteKit
    对 multipart form 的 CSRF 检查, 且二进制安全)。
    """
    import uuid

    import urllib.parse

    filename = urllib.parse.unquote(name) or "upload.bin"
    if not filename.strip():
        raise HTTPException(status_code=422, detail="缺少文件名")
    # /app 只读挂载 → 上传目录用 ~/.veya/uploads (veya-data volume rw)
    root = Path.home() / ".veya"
    uploads = root / "uploads"
    uploads.mkdir(parents=True, exist_ok=True)
    safe_name = Path(filename).name.replace("/", "_").replace("\\", "_")[:120]
    dst = uploads / f"{uuid.uuid4().hex[:8]}_{safe_name}"
    MAX_UPLOAD = 100 * 1024 * 1024  # 100MB
    body = await request.body()
    size = len(body)
    if size > MAX_UPLOAD:
        raise HTTPException(status_code=413, detail="文件超过 100MB 上限")
    try:
        dst.write_bytes(body)
    except Exception:
        dst.unlink(missing_ok=True)
        raise
    return {"path": str(dst.relative_to(root)), "name": filename, "size": size}
