# Optional one-time copy of existing htdemucs model. No Python required.
$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$dest = Join-Path $root 'tools\demucs-models\hub\checkpoints'
$filename = '955717e8-8726e21a.th'
New-Item -ItemType Directory -Force -Path $dest | Out-Null
$target = Join-Path $dest $filename
if (Test-Path -LiteralPath $target -PathType Leaf) {
    Write-Host "htdemucs 模型已存在：$target" -ForegroundColor Green
    exit 0
}
$homes = @()
if ($env:TORCH_HOME) { $homes += $env:TORCH_HOME }
if ($env:XDG_CACHE_HOME) { $homes += (Join-Path $env:XDG_CACHE_HOME 'torch') }
if ($env:USERPROFILE) { $homes += (Join-Path $env:USERPROFILE '.cache\torch') }
if ($env:HOME) { $homes += (Join-Path $env:HOME '.cache\torch') }
foreach ($cacheHome in ($homes | Select-Object -Unique)) {
    $source = Join-Path $cacheHome "hub\checkpoints\$filename"
    if (Test-Path -LiteralPath $source -PathType Leaf) {
        if ((Get-Item -LiteralPath $source).Length -lt 1MB) { continue }
        Copy-Item -LiteralPath $source -Destination $target -ErrorAction Stop
        Write-Host "已複製模型：$source -> $target" -ForegroundColor Green
        Write-Host '原本的快取仍保留，不會刪除。'
        exit 0
    }
}
Write-Host '舊快取沒有找到 htdemucs 權重。這不是錯誤；OpenKTV 第一次 AI 製作時會自行下載。' -ForegroundColor Yellow
Write-Host "模型預定下載路徑：$target"
