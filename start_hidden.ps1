$ErrorActionPreference = 'Stop'

$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$pythonw = Join-Path $projectRoot '.venv\Scripts\pythonw.exe'
$mainPy = Join-Path $projectRoot 'main.py'

if (-not (Test-Path $pythonw)) {
    throw "pythonw.exe not found: $pythonw"
}

if (-not (Test-Path $mainPy)) {
    throw "main.py not found: $mainPy"
}

$existing = Get-NetTCPConnection -LocalPort 5000 -State Listen -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host "KTV server already running on port 5000 (PID: $($existing[0].OwningProcess))."
    exit 0
}

$env:KTV_HEADLESS = '1'
Start-Process -FilePath $pythonw -ArgumentList 'main.py' -WorkingDirectory $projectRoot -WindowStyle Hidden

for ($i = 0; $i -lt 20; $i++) {
    Start-Sleep -Milliseconds 300
    $listener = Get-NetTCPConnection -LocalPort 5000 -State Listen -ErrorAction SilentlyContinue
    if ($listener) {
        Write-Host "KTV server started in background (PID: $($listener[0].OwningProcess))."
        exit 0
    }
}

Write-Host "KTV server start command was sent, but port 5000 is not listening yet."
