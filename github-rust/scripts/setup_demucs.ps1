# OpenKTV-Rust: install Demucs into its own Python 3.11 environment
# and build a small Rust launcher at tools/demucs.exe.
$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$tools = Join-Path $root 'tools'
$venv = Join-Path $tools 'demucs-env'
$python = Join-Path $venv 'Scripts\python.exe'
$launcherSource = Join-Path $PSScriptRoot 'demucs_launcher.rs'
$launcherExe = Join-Path $tools 'demucs.exe'
New-Item -ItemType Directory -Force -Path $tools | Out-Null

if (-not (Get-Command 'py' -ErrorAction SilentlyContinue)) {
    throw '找不到 Python 啟動器 py。請安裝 Python 3.11 (64-bit)，並勾選安裝 Python Launcher。'
}
& py -3.11 -c 'import sys; print(sys.version); assert sys.maxsize > 2**32'
if ($LASTEXITCODE -ne 0) { throw '請先安裝 Python 3.11 (64-bit)，然後重新執行。' }

if (-not (Test-Path $python)) {
    Write-Host '[1/4] Creating Demucs Python virtual environment...' -ForegroundColor Cyan
    & py -3.11 -m venv $venv
    if ($LASTEXITCODE -ne 0) { throw '建立虛擬環境失敗。' }
}

Write-Host '[2/4] Installing Demucs and PyTorch (may be a large download)...' -ForegroundColor Cyan
& $python -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) { throw 'pip 更新失敗。' }
& $python -m pip install demucs
if ($LASTEXITCODE -ne 0) { throw 'Demucs 安裝失敗，請查看上面的 pip 錯誤。' }

if (-not (Get-Command 'rustc' -ErrorAction SilentlyContinue)) {
    throw '找不到 rustc。請在能執行 cargo run 的 Rust PowerShell 環境執行本腳本。'
}
Write-Host '[3/4] Building tools/demucs.exe wrapper...' -ForegroundColor Cyan
& rustc --edition=2021 -O $launcherSource -o $launcherExe
if ($LASTEXITCODE -ne 0) { throw 'Rust 啟動器編譯失敗。' }

Write-Host '[4/4] Checking Demucs...' -ForegroundColor Cyan
& $launcherExe --help
if ($LASTEXITCODE -ne 0) { throw 'Demucs 啟動器測試失敗。' }
Write-Host "`nReady: $launcherExe" -ForegroundColor Green
Write-Host '首次進行 AI 分離時，Demucs 將下載 htdemucs 模型至 tools/demucs-models。' -ForegroundColor Yellow
Write-Warning '此 demucs.exe 仍需要 tools/demucs-env 及其 Python/PyTorch 環境；不是獨立單檔推論程式，也尚不能視為免安裝可攜發行版。'
