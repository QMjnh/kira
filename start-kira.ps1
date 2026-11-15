param(
    [string]$DataDir = (Join-Path $PSScriptRoot "kira-data"),
    [int]$Port = 8787
)

$ErrorActionPreference = "Stop"

Write-Host "Starting Kira..." -ForegroundColor Green
Write-Host "Kira working data: $DataDir (the photo library folder is chosen in the app)"
python (Join-Path $PSScriptRoot "server.py") --port $Port --data-dir $DataDir

