# veya 一键安装脚本 (Windows PowerShell)
#
#   irm https://raw.githubusercontent.com/soffy88/Veya/main/install.ps1 | iex
#
# 行为: 探测 python (>=3.11) → venv 安装 → PATH 提示 → veya init 引导
$ErrorActionPreference = "Stop"

Write-Host "veya> Windows 一键安装" -ForegroundColor Green

# ── 1. python 探测 ────────────────────────────────────────────────────────
$py = $null
foreach ($cand in @("python", "py")) {
    try {
        $v = & $cand -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>$null
        if ($v -and [version]$v -ge [version]"3.11") { $py = $cand; break }
    } catch { }
}
if (-not $py) {
    Write-Host "veya ✗ 需要 Python >= 3.11。请先安装: https://www.python.org/downloads/ (安装时勾选 Add to PATH)" -ForegroundColor Red
    exit 1
}
Write-Host "veya> 检测到 Python $v"

# ── 2. venv 安装 ──────────────────────────────────────────────────────────
$prefix = Join-Path $env:USERPROFILE ".local\veya"
New-Item -ItemType Directory -Force -Path $prefix | Out-Null
& $py -m venv (Join-Path $prefix "venv")
$pip = Join-Path $prefix "venv\Scripts\pip.exe"
& $pip install --upgrade pip | Out-Null
& $pip install veya
if ($LASTEXITCODE -ne 0) {
    Write-Host "veya ✗ PyPI 安装失败" -ForegroundColor Red
    exit 1
}

# ── 3. PATH ───────────────────────────────────────────────────────────────
$bin = Join-Path $prefix "venv\Scripts"
$userPath = [Environment]::GetEnvironmentVariable("Path", "User")
if ($userPath -notlike "*$bin*") {
    [Environment]::SetEnvironmentVariable("Path", "$userPath;$bin", "User")
    Write-Host "veya! 已把 $bin 加入用户 PATH (新终端生效)" -ForegroundColor Yellow
}

Write-Host "veya> 安装完成" -ForegroundColor Green
Write-Host "  veya init      # 30 秒向导: 接模型 + 选工作目录"
Write-Host "  veya doctor    # 环境自检"
Write-Host "  veya start     # 一键启动本地服务"
