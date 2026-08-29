param(
    [switch]$SkipInstall,
    [switch]$NoZip
)

$ErrorActionPreference = 'Stop'

$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$venvPython = Join-Path $projectRoot '.venv\Scripts\python.exe'
$distDir = Join-Path $projectRoot 'dist'
$buildDir = Join-Path $projectRoot 'build'
$specFile = Join-Path $projectRoot 'OpenKTV-AI.spec'
$outDir = Join-Path $distDir 'OpenKTV-AI'
$releaseZip = Join-Path $distDir 'OpenKTV-AI-portable.zip'

if (-not (Test-Path $venvPython)) {
    throw "Python virtual environment not found: $venvPython"
}

Push-Location $projectRoot
try {
    if (-not $SkipInstall) {
        & $venvPython -m pip install --upgrade pip
        & $venvPython -m pip install pyinstaller
    }

    if (Test-Path $distDir) { Remove-Item $distDir -Recurse -Force }
    if (Test-Path $buildDir) { Remove-Item $buildDir -Recurse -Force }
    if (Test-Path $specFile) { Remove-Item $specFile -Force }

    $pyiArgs = @(
        '-m', 'PyInstaller',
        '--noconfirm',
        '--clean',
        '--windowed',
        '--onedir',
        '--name', 'OpenKTV-AI',
        '--add-data', 'templates;templates',
        '--hidden-import', 'engineio.async_drivers.threading',
        '--hidden-import', 'eventlet.hubs.epolls',
        'main.py'
    )

    & $venvPython @pyiArgs

    if (-not (Test-Path $outDir)) {
        throw "Build output not found: $outDir"
    }

    $optionalYtDlp = Join-Path $projectRoot 'yt-dlp.exe'
    if (Test-Path $optionalYtDlp) {
        Copy-Item $optionalYtDlp -Destination (Join-Path $outDir 'yt-dlp.exe') -Force
    }

    $optionalFfmpeg = Join-Path $projectRoot 'ffmpeg'
    if (Test-Path $optionalFfmpeg) {
        Copy-Item $optionalFfmpeg -Destination (Join-Path $outDir 'ffmpeg') -Recurse -Force
    }

    $optionalStart = Join-Path $projectRoot 'start_hidden.ps1'
    if (Test-Path $optionalStart) {
        Copy-Item $optionalStart -Destination (Join-Path $outDir 'start_hidden.ps1') -Force
    }

    # Copy runtime DLLs explicitly to reduce target-machine dependency issues.
    $runtimeDlls = @(
        'vcruntime140.dll',
        'vcruntime140_1.dll',
        'msvcp140.dll',
        'python3.dll'
    )

    $venvScripts = Join-Path $projectRoot '.venv\Scripts'
    foreach ($dll in $runtimeDlls) {
        $candidate = Join-Path $venvScripts $dll
        if (Test-Path $candidate) {
            Copy-Item $candidate -Destination (Join-Path $outDir $dll) -Force
        }
    }

    # Also copy pythonXY.dll from venv root if available.
    Get-ChildItem -Path (Join-Path $projectRoot '.venv') -Filter 'python*.dll' -File -ErrorAction SilentlyContinue |
        ForEach-Object {
            Copy-Item $_.FullName -Destination (Join-Path $outDir $_.Name) -Force
        }

    if (-not $NoZip) {
        if (Test-Path $releaseZip) {
            Remove-Item $releaseZip -Force
        }
        Compress-Archive -Path (Join-Path $outDir '*') -DestinationPath $releaseZip -CompressionLevel Optimal
        Write-Host "Portable zip: $releaseZip" -ForegroundColor Yellow
    }

    Write-Host "Build complete: $outDir" -ForegroundColor Green
    Write-Host "Run: $outDir\OpenKTV-AI.exe" -ForegroundColor Cyan
}
finally {
    Pop-Location
}
