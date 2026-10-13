# Run ONCE on a Windows development machine with an existing WORKING demucs-env.
# Packages Python/PyTorch/Demucs into tools/demucs/ (no system Python on target PC).
# Do not confuse this with a native Rust port: CPython remains bundled inside.
param([switch]$SkipInstallerUpdate)
$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$python = Join-Path $root 'tools\demucs-env\Scripts\python.exe'
$entry = Join-Path $PSScriptRoot 'demucs_portable_entry.py'
$dist = Join-Path $root '.demucs-build\dist'
$work = Join-Path $root '.demucs-build\work'
$spec = Join-Path $root '.demucs-build\spec'
$target = Join-Path $root 'tools\demucs'

if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw '找不到 tools\demucs-env\Scripts\python.exe：請先在開發電腦準備可正常執行的 Demucs 環境。'
}
if (-not (Test-Path -LiteralPath $entry -PathType Leaf)) { throw '找不到 demucs_portable_entry.py' }
Write-Host '1/4 驗證既有 Demucs 與 PyTorch...' -ForegroundColor Cyan
# Windows PowerShell 5.1 can strip nested quote characters in Python -c arguments.
# Avoid any quoted Python string literals in this argument.
& $python -c 'import torch, demucs; from demucs.separate import main; print(torch.__version__); print(demucs.__file__)'
if ($LASTEXITCODE -ne 0) { throw '既有 Demucs 環境無法正常載入，請先修復，否則封裝也無法運作。' }

if (-not $SkipInstallerUpdate) {
    Write-Host '2/4 安裝或升級 PyInstaller（僅開發電腦需要）...' -ForegroundColor Cyan
    & $python -m pip install --upgrade pyinstaller
    if ($LASTEXITCODE -ne 0) { throw 'PyInstaller 安裝失敗。' }
} else {
    Write-Host '2/4 使用既有 PyInstaller...' -ForegroundColor Cyan
}

New-Item -ItemType Directory -Force -Path $dist,$work,$spec | Out-Null
$collect = @('demucs','torch','torchaudio','dora','julius','soundfile','lameenc')
$opts = @('--noconfirm','--clean','--onedir','--console','--name','demucs',
  '--distpath',$dist,'--workpath',$work,'--specpath',$spec)
foreach ($pkg in $collect) {
    & $python -c 'import importlib.util,sys;sys.exit(0 if importlib.util.find_spec(sys.argv[1]) is not None else 1)' $pkg
    if ($LASTEXITCODE -eq 0) { $opts += @('--collect-all',$pkg) }
}
Write-Host '3/4 封裝 Demucs 與依賴（體積可能很大）...' -ForegroundColor Cyan
& $python -m PyInstaller @opts $entry
if ($LASTEXITCODE -ne 0) { throw 'PyInstaller 封裝失敗。請保留上方錯誤資訊進行修正。' }
$built = Join-Path $dist 'demucs\demucs.exe'
if (-not (Test-Path -LiteralPath $built -PathType Leaf)) { throw '封裝完成但找不到 demucs.exe' }
& $built --help | Out-Null
if ($LASTEXITCODE -ne 0) { throw '封裝後 demucs.exe --help 無法執行。常見原因是 PyTorch DLL 或隱藏套件缺失。' }

Write-Host '4/4 複製到 tools\demucs...' -ForegroundColor Cyan
if (Test-Path -LiteralPath $target) { Remove-Item -LiteralPath $target -Recurse -Force }
Copy-Item -LiteralPath (Join-Path $dist 'demucs') -Destination $target -Recurse -Force
Write-Host '完成：tools\demucs\demucs.exe' -ForegroundColor Green
Write-Host '下一步：執行 scripts\move_model_to_tools.ps1 搬移既有模型；若沒有，OpenKTV 首次 AI 製作會自動下載。'
Write-Host '請連同 tools\demucs 內的 _internal/DLL 等內容一起搬移，不能只拿 demucs.exe！'
