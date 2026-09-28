"""检索调试路由（WP-28 API-D；tech-design §5.5/§5.6 / dd §10.2 §8.7 §4.4）。

端点：

- ``GET /tasks/{id}/traces``：检索轨迹列表（键集分页 + ``?stage=&version=``
  过滤，dd §10.3⑦）；列表项为摘要（计数维度），候选全集走详情端点；
- ``GET /traces/{id}``：单条轨迹详情（候选 kept/drop_reason、注入、引用闭环
  referenced/hallucinated/weak、降级记录，dd §10.2 retrieval debug 段）；
- ``GET /tasks/{id}/snapshots``：上下文快照元数据列表（node/batch/items 数/
  tokens/截断/模板版本）；
- ``GET /snapshots/{id}``：单快照详情（items 含 JSONL 偏移、latencies、
  usage、model_ref）；
- ``GET /snapshots/{id}/items/{position}``：full 档按 items[position] 的
  byte 偏移单行读注入知识全文（dd §4.4/§5 read_snapshot_line，R13）；
  非 full 档（snapshot_path 为空）或 position 越界 → 404；
- ``POST /workspaces/{id}/retrieval/playground``：ad-hoc 检索试验
  （dd §8.7）——临时 TaskContext（``task_id=playground-{uuid}``，不落任何
  业务表）、snapshot_level 强制 off，trace 维度数据（漏斗/候选/注入/
  degraded/latencies）直接随响应返回；``overrides`` 仅支持
  top_k/query_paths/types 三个键（dd §10.2），未知键 400；
- ``GET /workspaces/{id}/kb/tree``：工作区知识库链路→故事索引树（只读，
  dd §9.1 list_index_tree 与 IndexMirror 共用契约）。

任务维度端点先经 TaskDAO.get 守卫（404 不暴露存在性）；按主键查单行
（traces/{id}、snapshots/{id}）遵循 dd §3.3 豁免口径（与 /cases/{id} 同）。
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, Field

from ..adapters.reme import IndexMirror
from ..domain import Candidate, EntryType, InjectedItem
from ..errors import NotFoundError, TaskStateConflict, ValidationError
from ..graph.constants import RETRIEVAL_PRESETS
from ..graph.retrieval.pipeline import funnel_counts, retrieve_pipeline
from ..runtime.context import AppContext, TaskContext
from ..store.models import (
    SnapshotDAO,
    SnapshotRow,
    TaskDAO,
    TaskRow,
    TraceDAO,
    TraceRow,
    WorkspaceDAO,
)
from .common import DEFAULT_LIMIT, checked_cursor, page_response

router = APIRouter(prefix="/api/v1", tags=["retrieval-debug"])

# playground overrides 白名单键 → RetrievalConfig 字段（dd §10.2 注释：
# top_k/query_paths/types 临时覆盖）
_OVERRIDE_KEYS = {
    "top_k": "recall_topk",
    "query_paths": "query_paths",
    "types": "allowed_types",
}


# ---- 响应模型（dd §10.2 retrieval debug / snapshot 段） ----


class TraceSummaryOut(BaseModel):
    id: str
    task_id: str
    graph_run_id: str
    stage: str
    stage_version: int
    node: str
    batch_id: str | None
    query_count: int
    candidate_count: int
    kept_count: int
    injected_count: int
    referenced_count: int
    degraded_count: int
    created_at: str


class TraceDetailOut(BaseModel):
    id: str
    task_id: str
    graph_run_id: str
    stage: str
    stage_version: int
    node: str
    batch_id: str | None
    query_variant: dict
    candidates: list[dict]
    injected_ids: list[str]
    referenced_ids: list[str]
    hallucinated_ids: list[str]
    weak_ref_ids: list[str]
    degraded: list[dict]
    created_at: str


class SnapshotSummaryOut(BaseModel):
    id: str
    task_id: str
    graph_run_id: str
    stage: str
    stage_version: int
    node: str
    batch_id: str | None
    item_count: int
    total_tokens_est: int
    budget: int | None
    truncated: bool
    has_full: bool
    prompt_template_ver: str
    created_at: str


class SnapshotDetailOut(SnapshotSummaryOut):
    items: list[dict]
    model_ref: dict
    usage: dict
    latencies: dict


class SnapshotLineOut(BaseModel):
    position: int
    entry_id: str
    entry_version: str
    title: str
    content: str


class PlaygroundIn(BaseModel):
    query: str = Field(min_length=1)
    stage: str = Field(min_length=1)
    overrides: dict[str, Any] | None = None


class PlaygroundOut(BaseModel):
    funnel: dict
    candidates: list[dict]
    injected: list[dict]
    degraded: list[dict]
    latencies: dict
    token_est: int
    truncated: bool


# ---- 组装 ----


def _trace_summary(row: TraceRow) -> TraceSummaryOut:
    cands = row.candidates_obj()
    q = row.query_variant  # 原文 JSON（query_count 只取 queries 长度，不全量解析）
    try:
        q_count = len(json.loads(q).get("queries", []))
    except Exception:  # noqa: BLE001 - 防御：历史脏数据不阻断列表
        q_count = 0
    return TraceSummaryOut(
        id=row.id,
        task_id=row.task_id,
        graph_run_id=row.graph_run_id,
        stage=row.stage,
        stage_version=row.stage_version,
        node=row.node,
        batch_id=row.batch_id,
        query_count=q_count,
        candidate_count=len(cands),
        kept_count=sum(1 for c in cands if c.kept),
        injected_count=len(row.injected_ids_obj()),
        referenced_count=len(row.referenced_ids_obj()),
        degraded_count=len(row.degraded_obj()),
        created_at=row.created_at,
    )


def _trace_detail(row: TraceRow) -> TraceDetailOut:
    return TraceDetailOut(
        id=row.id,
        task_id=row.task_id,
        graph_run_id=row.graph_run_id,
        stage=row.stage,
        stage_version=row.stage_version,
        node=row.node,
        batch_id=row.batch_id,
        query_variant=json.loads(row.query_variant or "{}"),
        candidates=[c.model_dump() for c in row.candidates_obj()],
        injected_ids=row.injected_ids_obj(),
        referenced_ids=row.referenced_ids_obj(),
        hallucinated_ids=row.hallucinated_ids_obj(),
        weak_ref_ids=row.weak_ref_ids_obj(),
        degraded=[d.model_dump() for d in row.degraded_obj()],
        created_at=row.created_at,
    )


def _snapshot_summary(row: SnapshotRow) -> SnapshotSummaryOut:
    return SnapshotSummaryOut(
        id=row.id,
        task_id=row.task_id,
        graph_run_id=row.graph_run_id,
        stage=row.stage,
        stage_version=row.stage_version,
        node=row.node,
        batch_id=row.batch_id,
        item_count=len(row.items_obj()),
        total_tokens_est=row.total_tokens_est,
        budget=row.budget,
        truncated=bool(row.truncated),
        has_full=row.snapshot_path is not None,
        prompt_template_ver=row.prompt_template_ver,
        created_at=row.created_at,
    )


# ---- traces ----


@router.get("/tasks/{task_id}/traces")
async def list_traces(
    task_id: str,
    request: Request,
    stage: str | None = Query(None),
    version: int | None = Query(None),
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=200),
    cursor: str | None = Query(None),
) -> dict:
    db = request.app.state.db
    await TaskDAO(db).get(task_id)  # 404 不暴露存在性
    page = await TraceDAO(db).list_by_task(
        task_id,
        stage=stage,
        version=version,
        cursor=checked_cursor(cursor),
        limit=limit,
    )
    return page_response(page, _trace_summary)


@router.get("/traces/{trace_id}")
async def get_trace(trace_id: str, request: Request) -> TraceDetailOut:
    db = request.app.state.db
    row = await TraceDAO(db).get(trace_id)  # 404
    return _trace_detail(row)


# ---- snapshots ----


@router.get("/tasks/{task_id}/snapshots")
async def list_snapshots(
    task_id: str,
    request: Request,
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=200),
    cursor: str | None = Query(None),
) -> dict:
    db = request.app.state.db
    await TaskDAO(db).get(task_id)  # 404
    page = await SnapshotDAO(db).list_by_task(
        task_id, cursor=checked_cursor(cursor), limit=limit
    )
    return page_response(page, _snapshot_summary)


@router.get("/snapshots/{snapshot_id}")
async def get_snapshot(snapshot_id: str, request: Request) -> SnapshotDetailOut:
    db = request.app.state.db
    row = await SnapshotDAO(db).get(snapshot_id)  # 404
    summary = _snapshot_summary(row)
    return SnapshotDetailOut(
        **summary.model_dump(),
        items=[it.model_dump() for it in row.items_obj()],
        model_ref=row.model_ref_obj(),
        usage=row.usage_obj(),
        latencies=row.latencies_obj(),
    )


@router.get("/snapshots/{snapshot_id}/items/{position}")
async def get_snapshot_item(
    snapshot_id: str, position: int, request: Request
) -> SnapshotLineOut:
    """full 档按偏移读单行（dd §4.4/§5.5；meta/off 无全文 → 404）。"""
    db = request.app.state.db
    store = request.app.state.file_store
    row = await SnapshotDAO(db).get(snapshot_id)  # 404
    if row.snapshot_path is None:
        raise NotFoundError(
            "该快照无全文文件（snapshot_level 非 full，仅元数据档）",
            details={"snapshot_id": snapshot_id},
        )
    item = next((it for it in row.items_obj() if it.position == position), None)
    if item is None or item.char_offset is None or item.byte_length is None:
        raise NotFoundError(
            f"快照内不存在 position={position} 的注入条目",
            details={"snapshot_id": snapshot_id, "position": position},
        )
    task = await TaskDAO(db).get(row.task_id)
    line = await store.read_snapshot_line(
        task.workspace_id, row.task_id, row.snapshot_path,
        item.char_offset, item.byte_length,
    )
    data = json.loads(line)
    return SnapshotLineOut(
        position=position,
        entry_id=data.get("entry_id", item.entry_id),
        entry_version=data.get("entry_version", item.entry_version),
        title=data.get("title", item.title),
        content=data.get("content", ""),
    )


# ---- playground（dd §8.7） ----


@router.post("/workspaces/{workspace_id}/retrieval/playground")
async def playground(
    workspace_id: str, body: PlaygroundIn, request: Request
) -> PlaygroundOut:
    app_ctx: AppContext | None = getattr(request.app.state, "app_ctx", None)
    if app_ctx is None:
        raise TaskStateConflict(
            "任务执行组件未就绪（模型未配置），playground 不可用",
            details={"reason": "app_ctx_not_ready"},
        )
    db = request.app.state.db
    ws = await WorkspaceDAO(db).get(workspace_id)  # 404

    preset = RETRIEVAL_PRESETS.get(body.stage)
    if preset is None:
        raise ValidationError(
            f"未知检索阶段：{body.stage}",
            details={"stage": body.stage, "allowed": sorted(RETRIEVAL_PRESETS)},
        )
    cfg = _apply_overrides(preset, body.overrides or {})

    reader = await app_ctx.reme_factory.for_workspace(
        workspace_id, ws.kb_config_obj()
    )
    temp_task = TaskRow.create(
        id=f"playground-{uuid.uuid4().hex}",
        conversation_id="playground",
        workspace_id=workspace_id,
        status="running",
        current_stage=body.stage,
        langgraph_thread_id="playground",
        graph_run_id="playground",
        snapshot_level="off",  # dd §8.7 强制 off
    )
    ctx = TaskContext(
        app=app_ctx,
        task=temp_task,
        run_id=temp_task.id,
        files=app_ctx.file_store,
        reader=reader,
        snapshot_level="off",
        mirror=IndexMirror(reader),
    )
    outcome = await retrieve_pipeline(ctx, cfg, body.query, persist=False)
    return PlaygroundOut(
        funnel=funnel_counts(outcome.candidates),
        candidates=[
            c.model_dump() if isinstance(c, Candidate) else dict(c)
            for c in outcome.candidates
        ],
        injected=[
            it.model_dump() if isinstance(it, InjectedItem) else dict(it)
            for it in outcome.items
        ],
        degraded=[
            d.model_dump() if hasattr(d, "model_dump") else dict(d)
            for d in outcome.degraded
        ],
        latencies=outcome.latencies,
        token_est=outcome.token_est,
        truncated=outcome.truncated,
    )


def _apply_overrides(preset, overrides: dict):
    """白名单键覆盖阶段预设（dd §10.2：top_k/query_paths/types）。"""
    unknown = sorted(set(overrides) - set(_OVERRIDE_KEYS))
    if unknown:
        raise ValidationError(
            "playground overrides 含未支持键",
            details={"unknown": unknown, "allowed": sorted(_OVERRIDE_KEYS)},
        )
    patch: dict[str, Any] = {}
    for key, val in overrides.items():
        field = _OVERRIDE_KEYS[key]
        if field == "allowed_types":
            try:
                patch[field] = [EntryType(str(t)) for t in val]
            except ValueError as exc:
                raise ValidationError(
                    "types 覆盖含非法条目类型",
                    details={"got": val,
                             "allowed": [t.value for t in EntryType]},
                ) from exc
        elif field in ("recall_topk", "query_paths"):
            if not isinstance(val, int) or val < 1:
                raise ValidationError(
                    f"{key} 必须为正整数", details={"got": val}
                )
            patch[field] = val
    return preset.model_copy(update=patch)


# ---- kb/tree（tech-design §5.6；只读索引树） ----


@router.get("/workspaces/{workspace_id}/kb/tree")
async def kb_tree(workspace_id: str, request: Request) -> dict:
    db = request.app.state.db
    ws = await WorkspaceDAO(db).get(workspace_id)  # 404
    reader = await request.app.state.reme_factory.for_workspace(
        workspace_id, ws.kb_config_obj()
    )
    tree = await reader.list_index_tree()
    return tree.model_dump()
