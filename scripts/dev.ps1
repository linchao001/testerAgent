# 一键开发/运行脚本（Windows / PowerShell，对齐 scripts/dev.sh · dd §19.3）
$ErrorActionPreference = "Stop"

$Root = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $Root

if (-not (Test-Path .venv)) {
    python -m venv .venv
}
& .\.venv\Scripts\Activate.ps1

pip install .\lib\reme_ai-0.4.1.8-py3-none-any.whl
pip install -e .\server
if (-not (Test-Path server\.env)) {
    Copy-Item server\.env.example server\.env
}
python -m tester_agent.cli init-db

Push-Location web
try {
    npm install
    npm run build
} finally {
    Pop-Location
}

uvicorn tester_agent.main:app --host 127.0.0.1 --port 8080 --workers 1
