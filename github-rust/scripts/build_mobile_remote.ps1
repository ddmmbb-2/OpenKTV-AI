# OpenKTV 手機點歌／MIC 整合版 Windows 建置
# 在 Windows PowerShell 執行：powershell -ExecutionPolicy Bypass -File .\scripts\build_mobile_remote.ps1
$ErrorActionPreference = 'Stop'
$project = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
Set-Location $project

if (-not (Get-Command cargo -ErrorAction SilentlyContinue)) {
    throw '找不到 cargo。請先安裝 Rust stable MSVC (rustup)，並安裝 Visual Studio Build Tools C++。'
}
Write-Host '正在以 Rust release 模式建置 OpenKTV（包含新的手機點歌／MIC 頁面）...' -ForegroundColor Cyan
& cargo build --release
if ($LASTEXITCODE -ne 0) { throw 'Rust 編譯失敗；舊 EXE 未被覆寫。' }

$built = Join-Path $project 'target\release\openktv-rust.exe'
$target = Join-Path $project 'openktv-rust.exe'
if (-not (Test-Path $built)) { throw "找不到新建置的 EXE：$built" }
try {
    Copy-Item -LiteralPath $built -Destination $target -Force -ErrorAction Stop
} catch {
    throw '無法覆寫 EXE。請先將 OpenKTV 主程式關閉，再重新執行此腳本。原始錯誤：' + $_
}
Write-Host "新版 EXE 已更新：$target" -ForegroundColor Green
Write-Host '請保留原本 tools 及 data 資料夾；不需要重新製作 Demucs 模型或刪除現有曲庫。' -ForegroundColor Green
