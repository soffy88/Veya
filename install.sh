#!/usr/bin/env bash
# veya 一键安装脚本 — curl | bash 即可用
#
#   curl -fsSL https://raw.githubusercontent.com/soffy88/Veya/main/install.sh | bash
#
# 行为:
#   1. 探测 python3 (>=3.11)
#   2. 优先 pipx (隔离 + 全局命令), 否则 venv + ~/.local/bin 软链, 否则 --user
#   3. 安装 veya (PyPI 优先, 可 VEYA_VERSION=0.6.0 钉版本)
#   4. 提示 `veya init` 完成首次配置
#
# 环境变量:
#   VEYA_VERSION     钉版本 (默认最新)
#   VEYA_PREFIX      自定义安装前缀 (默认 ~/.local)
#   VEYA_SOURCE      pypi | github (默认 pypi, github 从 Release wheel 安装)
set -euo pipefail

# ── 0. 平台/参数 ──────────────────────────────────────────────────────────
VEYA_VERSION="${VEYA_VERSION:-}"
VEYA_PREFIX="${VEYA_PREFIX:-$HOME/.local}"
VEYA_SOURCE="${VEYA_SOURCE:-pypi}"
GITHUB_REPO="soffy88/Veya"

say()  { printf '\033[1;32mveya>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mveya!\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31mveya ✗\033[0m %s\n' "$*" >&2; exit 1; }

# ── 1. python 探测 ────────────────────────────────────────────────────────
PY=""
for cand in python3 python; do
  if command -v "$cand" >/dev/null 2>&1; then
    if "$cand" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
      PY="$cand"; break
    fi
  fi
done
if [ -z "$PY" ]; then
  die "需要 Python >= 3.11。请先安装: https://www.python.org/downloads/  (或 apt install python3)"
fi
say "检测到 $("$PY" --version 2>&1)"

# ── 2. 安装方式 ───────────────────────────────────────────────────────────
install_via_pipx() {
  command -v pipx >/dev/null 2>&1 || return 1
  say "通过 pipx 安装 (全局隔离)"
  local spec="veya"
  [ -n "$VEYA_VERSION" ] && spec="veya==$VEYA_VERSION"
  pipx install "$spec" "$@" || die "pipx install 失败"
  return 0
}

install_via_venv() {
  say "创建虚拟环境 $VEYA_PREFIX/veya/venv"
  "$PY" -m venv "$VEYA_PREFIX/veya/venv"
  local pip="$VEYA_PREFIX/veya/venv/bin/pip"
  local spec="veya"
  [ -n "$VEYA_VERSION" ] && spec="veya==$VEYA_VERSION"
  if [ "$VEYA_SOURCE" = "github" ]; then
    local url="https://github.com/$GITHUB_REPO/releases/latest/download/veya-${VEYA_VERSION:-0.6.0}-py3-none-any.whl"
    "$pip" install --force-reinstall "$url" || die "GitHub Release 下载失败: $url"
  else
    "$pip" install --upgrade "$spec" || die "PyPI 安装失败 (可重试: VEYA_SOURCE=github 从 Release 装)"
  fi
  mkdir -p "$VEYA_PREFIX/bin"
  ln -sf "$VEYA_PREFIX/veya/venv/bin/veya" "$VEYA_PREFIX/bin/veya"
  ln -sf "$VEYA_PREFIX/veya/venv/bin/veya-headless" "$VEYA_PREFIX/bin/veya-headless" 2>/dev/null || true
  ln -sf "$VEYA_PREFIX/veya/venv/bin/veya-simple" "$VEYA_PREFIX/bin/veya-simple" 2>/dev/null || true
}

PATH_HINT=""
if ! install_via_pipx; then
  install_via_venv
  case ":$PATH:" in
    *":$VEYA_PREFIX/bin:"*) ;;
    *) PATH_HINT="请把 $VEYA_PREFIX/bin 加入 PATH (或重启终端)" ;;
  esac
fi

# ── 3. 完成 ───────────────────────────────────────────────────────────────
say "安装完成: $(veya --version 2>/dev/null || true)"
if [ -n "$PATH_HINT" ]; then
  warn "$PATH_HINT"
fi
say "下一步:"
say "  veya init      # 30 秒向导: 接模型 (OpenAI/Anthropic/DashScope/Ollama 本地) + 选工作目录"
say "  veya doctor    # 环境自检"
say "  veya start     # 一键启动本地服务 + 浏览器"
say "  veya \"帮我审查这个目录的代码\"   # 直接派任务"
