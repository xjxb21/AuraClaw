# Windows 本机启动 AuraClaw 全套拓扑（12 入口 + Ingress :8080）。
# 不依赖 uv / Docker / Kafka。请在仓库根目录执行，或直接运行本脚本。
#
#   powershell -ExecutionPolicy Bypass -File scripts\serve-local.ps1

$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")

$python = Join-Path (Get-Location) ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    throw "missing .venv at $python. Create it with: py -3 -m venv .venv"
}

$env:PYTHONPATH = Join-Path (Get-Location) "src"
$env:AURACLAW_ENV_FILE = ".env.debug"
$env:PYTHONUNBUFFERED = "1"
$env:PYTHONUTF8 = "1"

Write-Host "Starting auraclaw serve on 127.0.0.1 (Ingress http://127.0.0.1:8080)"
& $python -m auraclaw serve --host 127.0.0.1
