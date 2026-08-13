param(
    [string]$DataDir = (Join-Path $PSScriptRoot "kira-data"),
    [int]$Port = 8787
)

$ErrorActionPreference = "Stop"

Write-Host "Starting Kira..." -ForegroundColor Green
Write-Host "Files and returned edits will be stored in: $DataDir"
python (Join-Path $PSScriptRoot "server.py") --port $Port --data-dir $DataDir

