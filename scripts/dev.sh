#!/usr/bin/env bash
# 一键开发/运行脚本（dd §19.3）
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

python3 -m venv .venv && source .venv/bin/activate
pip install ./lib/reme_ai-0.4.1.8-py3-none-any.whl    # vendored ReMe 0.4.1.8
pip install -e ./server                               # 含 server/pyproject 依赖
[ -f server/.env ] || cp server/.env.example server/.env
python -m tester_agent.cli init-db                    # 迁移+内置 agent 种子+config 单行（WP-02 接线）
(cd web && npm install && npm run build)              # 首次/前端变更时（web 目录就绪后生效）
exec uvicorn tester_agent.main:app --host 127.0.0.1 --port 8080 --workers 1
