"""维护性端点（WP-29：对账触发 / tech-design §3.3③ / dd §11.3）。

``POST /workspaces/{id}/reconcile``：用户手动触发该工作区的 DB↔文件对账
（"检查文件一致性"按钮，dd §11.3 触发时机之一），返回
``ReconcileReport``（file_missing / hash_conflict / orphan_paths 计数与清单）。
对账副作用（mark_error）落库；冲突消解走普通 PUT /cases/{id}，不设后门。

进程启动全量对账在 main.py lifespan 内调用 Reconciler.reconcile_all（dd
§11.3），不走本端点。
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from ..runtime.reconciler import Reconciler

router = APIRouter(prefix="/api/v1", tags=["maintenance"])


@router.post("/workspaces/{workspace_id}/reconcile")
async def reconcile_workspace(workspace_id: str, request: Request) -> dict:
    db = request.app.state.db
    store = request.app.state.file_store
    report = await Reconciler(db, store).reconcile_workspace(workspace_id)
    return report.to_dict()
