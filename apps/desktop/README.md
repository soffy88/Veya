# Veya Desktop — Tauri 2.0 桌面版

> 与网页版（`apps/web`）**同一份前端代码 + 同一份后端代码**，能力完全对齐：
> 桌面窗口 = Tauri 壳 + 网页版静态构建（adapter-static）+ 内嵌启动的 Python 后端。

## 架构

```
┌────────────────────────────────────────────────┐
│ Tauri 壳 (Rust, src-tauri/)                     │
│  ├─ 启动时拉起后端:                             │
│  │   1. resources/backend/veya-backend (PyInstaller, 下载即用) │
│  │   2. <repo>/venv/bin/python -m veya.server.app (开发)       │
│  │   3. python3 -m veya.server.app             │
│  ├─ 健康检查: GET /api/v1/mcp/health (90s 超时) │
│  └─ WebView 加载静态前端 (frontendDist)         │
├─ 静态前端: apps/web/build-desktop/              │
│   (apps/web 源码 adapter-static 构建,           │
│    VITE_VEYA_ENDPOINT=http://127.0.0.1:8767 注入) │
└─ 后端: veya L4 gateway (veya.server.app, 端口 8767) │
```

- **退出清理**：窗口关闭 → 后端子进程 SIGTERM
- **CORS**：后端已放行 `tauri://localhost` / `http(s)://tauri.localhost`（server/app.py）
- **端口**：`VEYA_BACKEND_PORT` 可覆盖（默认 8767）

## 构建

### 1. 静态前端（网页版同一源码）

```bash
bash scripts/build-desktop-web.sh     # → apps/web/build-desktop/
# 构建期间临时移走 +server.ts 路由 (adapter-static 不允许 server 路由), 结束后自动恢复
```

### 2. 后端打包（PyInstaller，Linux 已验证）

```bash
python3.11 -m venv /tmp/veya-pack-venv
/tmp/veya-pack-venv/bin/pip install pyinstaller fastapi "uvicorn[standard]" \
  httpx aiohttp python-dotenv python-multipart networkx structlog apscheduler \
  pandas numpy pyarrow pgmpy scipy pyyaml plotly matplotlib textual chardet \
  cryptography rapidfuzz argon2-cffi asyncpg tree-sitter tree-sitter-python \
  anthropic openai mcp
/tmp/veya-pack-venv/bin/pyinstaller scripts/pyinstaller/veya_backend.spec \
  --noconfirm --distpath dist-backend --workpath build-pyinstaller
# 产物: dist-backend/veya-backend/veya-backend (自包含, 无需系统 Python)
cp -r dist-backend/veya-backend/* apps/desktop/resources/backend/
```

> PyInstaller 要点（spec 已处理）：`pathex` 必须含仓库根（`import veya`）；
> `datas` 复制 `platform/3O` 到 `veya/platform/3O`（veya.platform 按相对结构检查）；
> hiddenimports 补主库惰性导入的第三方（cryptography/rapidfuzz/asyncpg/...）。
> Python 3.14 与 PyInstaller 6.21 不兼容（dataclasses 循环导入）→ 用 3.11 打包。

### 3. Tauri 壳（Rust，需 cargo；CI 已配置三平台矩阵）

```bash
cd apps/desktop
pnpm install
pnpm tauri build     # beforeBuildCommand 自动执行 scripts/build-desktop-web.sh
# 产物: apps/desktop/src-tauri/target/release/bundle/{AppImage,dmg,nsis}/...
```

## CI 发布（GitHub Actions）

`.github/workflows/desktop.yml`：打 tag（`v*`）或手动触发 → 三平台（Ubuntu AppImage /
macOS dmg / Windows nsis）→ 静态前端 + PyInstaller 后端 + Tauri 构建 →
产物上传 artifact 并附加到 GitHub Release → 用户下载即用。

## 开发调试

```bash
# 无头冒烟 (前后端健康检查, 不建窗口): 由 CI 冒烟使用
# 后端单独跑 (开发):
./venv/bin/python -m veya.server.app --host 127.0.0.1 --port 8767
# 静态前端单独预览:
cd apps/web && npx vite preview --outDir build-desktop --port 4173
```
