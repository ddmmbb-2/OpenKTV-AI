# Run in PowerShell on Windows. Requires Rust stable MSVC toolchain and VS C++ build tools.
$ErrorActionPreference = 'Stop'
Set-Location (Join-Path $PSScriptRoot '..')
Write-Host 'Building OpenKTV-Rust Windows release...' -ForegroundColor Cyan
cargo build --release
if ($LASTEXITCODE -ne 0) { throw 'cargo build failed' }
$dist = Join-Path (Get-Location) 'dist/OpenKTV-Rust'
New-Item -ItemType Directory -Force -Path $dist | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $dist 'tools') | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $dist 'data/songs') | Out-Null
Copy-Item 'target/release/openktv-rust.exe' (Join-Path $dist 'OpenKTV.exe') -Force
foreach ($tool in @('mpv.exe','yt-dlp.exe','ffmpeg.exe','ffprobe.exe','deno.exe','demucs.exe')) {
  if (Test-Path "tools/$tool") { Copy-Item "tools/$tool" (Join-Path $dist 'tools') -Force }
}
# Some distributions of mpv/demucs require adjacent DLLs and model files.
Get-ChildItem 'tools' -File -Filter '*.dll' -ErrorAction SilentlyContinue | ForEach-Object { Copy-Item $_.FullName (Join-Path $dist 'tools') -Force }
if (Test-Path 'tools/models') { Copy-Item 'tools/models' (Join-Path $dist 'tools/models') -Recurse -Force }
Copy-Item 'README.md' (Join-Path $dist 'README.md')
$zip = Join-Path (Get-Location) 'dist/OpenKTV-Rust-Windows.zip'
if (Test-Path $zip) { Remove-Item $zip }
Compress-Archive -Path "$dist/*" -DestinationPath $zip
Write-Host "Build finished: $zip" -ForegroundColor Green
Write-Warning 'mpv / FFmpeg / yt-dlp and optional Demucs are NOT downloaded automatically. Copy the required tools into tools/ before packaging.'
