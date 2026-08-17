# Veya 工具链安装手册（免 root / 用户级）

> 实战沉淀 2026-08 · 目标：让 `veya doctor` 工具链段全绿，且 `oskill` 的
> `typst_author` / `drawio_diagram` / `verity_check` 在本机可跑真实端到端。
> 适用场景：**无 sudo 密码的服务器 / 共享开发机**（Ubuntu 24.04+，x86_64）。
> 若你有 root，直接 `apt install` 对照包更省事（见文末对照表）。

---

## 1. 背景

`veya doctor`（`cli/product.py`）的工具链段复用 `oskill.env_doctor` 检查：

| 依赖 | required | 用途 | 来源 |
|---|---|---|---|
| python3 / numpy / pandas / matplotlib | ✅ | 科学计算栈 | 系统/venv |
| **typst** | 可选 | Typst 论文编译（`typst_author` / `verity_check`） | GitHub release |
| **xelatex** | 可选 | LaTeX 中文论文编译（`verity_check`，需跑两遍） | TeX Live 用户级 |
| **drawio** | 可选 | DrawIO 图示导出 PDF（`drawio_diagram`） | AppImage 解压 |
| **pdftoppm** | 可选 | PDF→PNG 视觉检查（`verity_check`） | pymupdf 兼容实现 |

全部安装到 `~/.local/`（bin / lib / venv / texlive），不写系统目录。

---

## 2. 安装

### 2.1 typst（GitHub 预编译二进制）

```bash
cd /tmp
curl -sL -o typst.tar.xz \
  https://github.com/typst/typst/releases/download/v0.13.1/typst-x86_64-unknown-linux-musl.tar.xz
tar -xf typst.tar.xz
cp typst-x86_64-unknown-linux-musl/typst ~/.local/bin/
typst --version   # typst 0.13.1
```

### 2.2 vtracer（高保真 SVG 描摹，vision_trace 主引擎）

`pip install vtracer` 的 Python 扩展在 CPython 3.14 下会段错误（0.6.15 旧 ABI），
veya 改用独立 CLI 二进制 + 子进程隔离（崩了也只丢子进程）：

```bash
mkdir -p ~/.veya/bin
cd /tmp
curl -sL -o vtracer.tar.gz \
  https://github.com/visioncortex/vtracer/releases/download/0.6.4/vtracer-x86_64-unknown-linux-musl.tar.gz
tar -xzf vtracer.tar.gz
cp vtracer ~/.veya/bin/vtracer && chmod +x ~/.veya/bin/vtracer
~/.veya/bin/vtracer --version   # visioncortex VTracer 0.6.4
```

解析顺序: `VEYA_VTRACER_BIN` → `~/.veya/bin/vtracer` → PATH。缺二进制时
`vision_trace` 自动降级 PIL 描摹（结果 `geometry.engine` 标注 `pil-fallback`）。

### 2.3 drawio（AppImage 解压 + 用户级 GTK3 依赖链）

服务器常缺 FUSE（AppImage 起不来）与 GTK3（Electron 起不来），两步都绕开：

```bash
cd /tmp
VER=31.1.8
curl -sL -o drawio.AppImage \
  "https://github.com/jgraph/drawio-desktop/releases/download/v${VER}/drawio-x86_64-${VER}.AppImage"
chmod +x drawio.AppImage
./drawio.AppImage --appimage-extract        # 解压, 绕过 FUSE
mv squashfs-root ~/.local/lib/drawio

# 用户级 GTK3 依赖链 (ldd 递归补齐, Ubuntu 26.04 实测这 14 个)
mkdir -p /tmp/dl && cd /tmp/dl
for pkg in libgtk-3-0t64 libcairo-gobject2 libgdk-pixbuf-2.0-0 libpango-1.0-0 \
           libpangocairo-1.0-0 libpangoft2-1.0-0 libwayland-cursor0 libwayland-egl1 \
           libxcursor1 libxcb-cursor0 libxinerama1 glycin-loaders libglycin-2-0 liblcms2-2; do
  apt-get download "$pkg" 2>/dev/null
done
mkdir -p ~/.local/lib/gtk3
for deb in *.deb; do dpkg-deb -x "$deb" ~/.local/lib/gtk3/; done

# wrapper: LD_LIBRARY_PATH + --no-sandbox (chrome-sandbox 需要 root:4755, 绕开)
cat > ~/.local/bin/drawio << 'EOF'
#!/usr/bin/env bash
export LD_LIBRARY_PATH="$HOME/.local/lib/gtk3/usr/lib/x86_64-linux-gnu${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
exec "$HOME/.local/lib/drawio/drawio" --no-sandbox "$@"
EOF
chmod +x ~/.local/bin/drawio
```

> **依赖补齐迭代法**（新发行版缺库不同时用）：
> `LD_LIBRARY_PATH=~/.local/lib/gtk3/usr/lib/x86_64-linux-gnu ldd ~/.local/lib/drawio/drawio | grep "not found"`
> 对每个缺失库查 `apt-cache search` 对应包，`apt-get download` + `dpkg-deb -x` 解压进 `~/.local/lib/gtk3/`，重复直到无 missing。
> 常见库名→包名映射：`libpangocairo-1.0.so.0`→`libpangocairo-1.0-0`、`libglycin-2.so.0`→`libglycin-2-0`、`liblcms2.so.2`→`liblcms2-2`。

### 2.3 pdftoppm（pymupdf 兼容 wrapper，免系统 poppler）

```bash
# 独立 venv, 不污染系统 python
uv venv ~/.local/venv/pdftools
uv pip install --python ~/.local/venv/pdftools/bin/python pymupdf

mkdir -p ~/.local/lib/pdftools
cat > ~/.local/lib/pdftools/pdftoppm_compat.py << 'PYEOF'
#!/usr/bin/env python3
"""pdftoppm 兼容实现 (pymupdf 后端) — 支持 -png/-jpeg/-r <dpi>/-f/-l/-singlefile。"""
from __future__ import annotations
import sys
from pathlib import Path

def main(argv: list[str]) -> int:
    fmt, dpi, first, last, single = "png", 150, 1, None, False
    args: list[str] = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "-png": fmt = "png"
        elif a in ("-jpeg", "-jpg"): fmt = "jpg"
        elif a == "-r": i += 1; dpi = int(argv[i])
        elif a == "-f": i += 1; first = int(argv[i])
        elif a == "-l": i += 1; last = int(argv[i])
        elif a == "-singlefile": single = True
        elif a.startswith("-"): print(f"pdftoppm: ignore {a}", file=sys.stderr)
        else: args.append(a)
        i += 1
    if not args: return 1
    pdf = Path(args[0]); prefix = Path(args[1]) if len(args) > 1 else pdf.stem
    import pymupdf
    doc = pymupdf.open(pdf)
    last = min(last or doc.page_count, doc.page_count)
    ext = "jpg" if fmt == "jpg" else "png"
    for n in range(first, last + 1):
        out = prefix if single else prefix.with_name(f"{prefix.name}-{n}")
        doc[n - 1].get_pixmap(dpi=dpi).save(out.with_suffix(f".{ext}"))
    return 0

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
PYEOF

cat > ~/.local/bin/pdftoppm << 'EOF'
#!/usr/bin/env bash
exec "$HOME/.local/venv/pdftools/bin/python" "$HOME/.local/lib/pdftools/pdftoppm_compat.py" "$@"
EOF
chmod +x ~/.local/bin/pdftoppm
```

> 为什么不用真 poppler：系统 poppler 需 root；conda-forge 包依赖链长。pymupdf
> 渲染质量 ≥ poppler，且与 `verity_check.pdf_pages` 的调用（`-png -r 160`）完全兼容。
> **功能缺口**：仅实现栅格化子集，不支持文本提取类参数（verity 用不到）。

### 2.4 xelatex（TeX Live 2026 用户级 + 中文支持）

```bash
cd /tmp
curl -sL -o install-tl.tar.gz https://mirror.ctan.org/systems/texlive/tlnet/install-tl-unx.tar.gz
tar -xzf install-tl.tar.gz
cd install-tl-*

# 精简 profile: scheme-basic (150MB 级), 不含 doc/src
cat > /tmp/tlprofile.txt << 'EOF'
selected_scheme scheme-basic
TEXDIR /home/soffy/.local/texlive/2026
TEXMFLOCAL /home/soffy/.local/texlive/texmf-local
TEXMFHOME /home/soffy/texmf
option_doc 0
option_src 0
EOF
./install-tl --profile /tmp/tlprofile.txt

export PATH="$HOME/.local/texlive/2026/bin/x86_64-linux:$PATH"
tlmgr install xetex xeCJK ctex          # scheme-basic 不含 xetex 引擎!
tlmgr install collection-langchinese    # 中文断行/字体配置

# 中文字体 (无 root: 用户级 ~/.fonts)
mkdir -p ~/.fonts
cp /path/to/simhei.ttf ~/.fonts/        # 或下载 Noto Sans CJK
fc-cache -f ~/.fonts

ln -sf "$HOME/.local/texlive/2026/bin/x86_64-linux/xelatex" ~/.local/bin/xelatex
```

> **坑**：
> - 2026 版 install-tl 不认 `option_texlruntime`（旧文档残留），profile 里删掉。
> - `scheme-basic` **不含 xetex**，必须先 `tlmgr install xetex` 再装 xeCJK/ctex，
>   否则 `xeCJK.sty not found`。
> - 无中文字体时 xelatex 报 "No pages of output"（字体缺失静默失败）——装字体后重跑。

---

## 3. 验证

```bash
veya doctor                       # 工具链段 8/8 ✔
veya doctor --json | jq '.checks[] | select(.name|startswith("工具链"))'

# 端到端抽查
typst compile doc.typ             # 或: echo '#set text(font: "SimHei"); 你好' | typst compile - out.pdf
xvfb-run -a drawio --export --format pdf --crop --output out.pdf in.drawio
pdftoppm -png -r 100 out.pdf page  # → page-1.png
xelatex -interaction=nonstopmode main.tex   # 中文文档需 xeCJK + 中文字体
```

`veya doctor` 期望输出：

```
✔ 工具链 python3 / numpy / pandas / matplotlib
✔ 工具链 typst / xelatex / drawio / pdftoppm
✔ 工具链就绪: 必须项全部就绪
```

---

## 4. 有 root 时的对照（apt 直装）

```bash
sudo apt install -y typst poppler-utils   # typst 也在 Ubuntu 源
sudo apt install -y drawio                # Ubuntu 24.04+ 有 drawio 包
sudo apt install -y texlive-xetex texlive-lang-chinese fonts-noto-cjk
# drawio headless 导出: sudo apt install -y xvfb
```

---

## 5. 排障速查

| 症状 | 原因 | 处理 |
|---|---|---|
| `error loading shared libraries: libfuse.so.2` | AppImage 需 FUSE | `--appimage-extract` 解压后直跑 |
| `chrome-sandbox ... owned by root and has mode 4755` | Electron sandbox | wrapper 加 `--no-sandbox` |
| `libgtk-3.so.0 ... not found` | 服务器无 GTK3 | §2.2 依赖链 |
| `File xeCJK.sty not found` | xetex/xeCJK 未装 | `tlmgr install xetex xeCJK ctex` |
| `No pages of output` (xelatex 中文) | 缺中文字体 | `~/.fonts` + `fc-cache -f` |
| drawio 导出无声无息没 PDF | headless 无显示 | `xvfb-run -a drawio --export ...` |
| 新发行版缺其他 .so | 依赖映射不同 | §2.2 迭代法 ldd 补齐 |

---

## 6. 已装清单（2026-08 本机基准）

| 组件 | 位置 | 版本 |
|---|---|---|
| typst | `~/.local/bin/typst` | 0.13.1 |
| **vtracer** | `~/.veya/bin/vtracer` | 0.6.4 |
| drawio | `~/.local/bin/drawio` → `~/.local/lib/drawio` | 31.1.8 |
| GTK3 依赖 | `~/.local/lib/gtk3/` | 14 个 deb 解压 |
| pdftoppm | `~/.local/bin/pdftoppm` → `~/.local/venv/pdftools` | pymupdf 1.28.2 |
| xelatex | `~/.local/bin/xelatex` → `~/.local/texlive/2026` | TeX Live 2026 |
| 中文字体 | `~/.fonts/simhei.ttf` | SimHei |
