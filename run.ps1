# WeatherGPT - SIH26068  |  run:  powershell -ExecutionPolicy Bypass -File .\run.ps1
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location (Join-Path $root "backend")

$venvPy = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPy)) {
    Write-Host "Creating virtual environment..."
    $base = (Get-Command py -ErrorAction SilentlyContinue) ? "py" : "python"
    & $base -3 -m venv (Join-Path $root ".venv")
}
if (-not (Test-Path $venvPy)) { throw "Python 3.10+ not found. Install it from python.org." }

Write-Host "Installing dependencies (first run only)..."
& $venvPy -m pip install --quiet --disable-pip-version-check --upgrade pip
& $venvPy -m pip install --quiet --disable-pip-version-check -r requirements.txt

Write-Host ""
Write-Host "  WeatherGPT is starting."
Write-Host "  UI    http://localhost:8000"
Write-Host "  API   http://localhost:8000/docs"
Write-Host ""
Start-Process "http://localhost:8000"
& $venvPy -m uvicorn app.main:app --host 127.0.0.1 --port 8000
