"""eval 执行器（WP-13，dd §15.3）：单用例跑 retrieve_pipeline + 脚本化生成 + 引用闭环。

确定性来源（dd §15.3 "eval 离线、确定性"）：知识库走 tests/fakes.FakeReMeReader
（词面打分），辅助/生成 LLM 均为 FakeLLM 脚本化回放（生成文本 = fixture 期望产物）。
因此指标度量的是"检索/注入/引用闭环接线 + fixture 期望"，不度量真实模型质量
（黄金集到位后替换生成侧，S7）。

每用例独立 tmp 目录建库建任务（snapshot_level=meta），跑完即清；
aux usage 从 snapshot 行读回（顺手验证 §8.3 落库链路）。
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from ..adapters.reme import Entry, IndexMirror, ReMeReaderFactory
from ..domain import RetrievalConfig
from ..graph.retrieval.pipeline import (
    close_retrieval_trace,
    funnel_counts,
    retrieve_pipeline,
)
from ..runtime.context import AppContext, TaskContext
from ..store.db import Database, run_migrations
from ..store.models import (
    ConfigDAO,
    SnapshotDAO,
    TaskDAO,
    TaskRow,
    TraceDAO,
    WorkspaceDAO,
    WorkspaceRow,
)
from ..store.workspace_files import FileStore
from .fixtures import EvalCase

# server/tester_agent/eval/runner.py → parents[2] = server/
_TESTS_DIR = Path(__file__).resolve().parents[2] / "tests"

_WS = "eval-ws"
_CONV = "eval-conv"
# 生成主调用的占位 user 消息（eval 只消费脚本化 content 与固定 usage）
_GEN_USER = "eval：按期望产物生成"


def _import_fakes():
    """tests/fakes.py 非包模块：把 server/tests 挂上 sys.path 后导入（幂等）。

    dd §15.3 "eval 与单测共享 FakeReader"；pytest 场景下 tests 目录已在
    sys.path（pytest 自动插入），CLI 直跑时由本函数兜底。
    """
    if str(_TESTS_DIR) not in sys.path:
        sys.path.insert(0, str(_TESTS_DIR))
    from fakes import FakeLLM, FakeReMeReader

    return FakeLLM, FakeReMeReader


@dataclass
class CaseReport:
    """单用例指标（dd §15.3 五列）+ 对账细节。"""

    name: str
    stage: str
    recall_hit: float | None  # 期望条目 ∩ candidates / 期望条目
    inject_hit: float | None  # 期望条目 ∩ injected / 期望条目
    reference_rate: float | None  # injected ∩ referenced / injected
    clause_coverage: float | None  # 被注入条目覆盖的条款 / 全部条款
    aux_calls: int
    aux_tokens: int
    main_tokens: int
    wall_ms: int
    degraded_steps: int
    funnel: dict = field(default_factory=dict)
    injected: list[str] = field(default_factory=list)
    referenced: list[str] = field(default_factory=list)
    hallucinated: list[str] = field(default_factory=list)
    weak_refs: list[str] = field(default_factory=list)


def _ratio(hit: int, total: int) -> float | None:
    return None if total == 0 else hit / total


def _aux_script(case: EvalCase, cfg: RetrievalConfig) -> list[str]:
    """管线辅助 LLM 脚本：multi_query（query_paths>=2 时）+ rerank 各一次。

    小冒烟集候选恒 ≤30，rerank 单桶单调用；rerank 零候选时不发起调用，
    脚本多余项随 FakeLLM 实例丢弃，无害。空白 intent 不在本口径内
    （fixture 约束 intent 非空）。
    """
    script: list[str] = []
    if cfg.query_paths >= 2:
        mq = case.llm.multi_query
        script.append(
            json.dumps(
                {
                    "keyword_queries": mq.keyword_queries if mq else [],
                    "rewrite_queries": mq.rewrite_queries if mq else [],
                },
                ensure_ascii=False,
            )
        )
    script.append(
        json.dumps(
            {
                "scores": [
                    {"entry_id": eid, "score": score, "reason": "fixture"}
                    for eid, score in case.llm.rerank_scores.items()
                ]
            },
            ensure_ascii=False,
        )
    )
    return script


async def run_case(
    case: EvalCase, entries: list[Entry], cfg: RetrievalConfig
) -> CaseReport:
    """单用例执行：检索管线 → 脚本化生成 → close_retrieval_trace → 指标。"""
    FakeLLM, FakeReMeReader = _import_fakes()
    tmp = Path(tempfile.mkdtemp(prefix=f"eval-{case.name}-"))
    db: Database | None = None
    try:
        db_path = tmp / "app.db"
        run_migrations(db_path)
        db = Database(db_path)
        await WorkspaceDAO(db).create(
            WorkspaceRow.create(
                id=_WS, name="eval", kb_config={"kb_id": "EVAL-KB", "mode": "sdk"}
            )
        )
        await db.aexecute(
            "INSERT INTO conversation (id, workspace_id, title, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (_CONV, _WS, "eval", "2026-09-27T00:00:00.000Z", "2026-09-27T00:00:00.000Z"),
        )
        task_id = f"eval-{case.name}"
        run_id = f"evalrun-{case.name}"
        await TaskDAO(db).create(
            TaskRow.create(
                id=task_id,
                conversation_id=_CONV,
                workspace_id=_WS,
                status="running",
                current_stage=case.stage,
                langgraph_thread_id=f"thread-{case.name}",
                graph_run_id=run_id,
                snapshot_level="meta",
            )
        )

        reader = FakeReMeReader(entries)  # 全能力 caps
        mirror = IndexMirror(reader)
        store = FileStore(tmp / "data")
        llm_aux = FakeLLM(_aux_script(case, cfg))
        llm_main = FakeLLM(list(case.expected.generated_texts))
        app = AppContext(
            db=db,
            file_store=store,
            llm=llm_aux,
            reme_factory=ReMeReaderFactory(),
            config=ConfigDAO(db),
        )
        ctx = TaskContext(
            app=app,
            task=await TaskDAO(db).get(task_id),
            run_id=run_id,
            files=store,
            reader=reader,
            snapshot_level="meta",
            mirror=mirror,
        )

        t0 = time.perf_counter()
        outcome = await retrieve_pipeline(
            ctx, cfg, case.intent, scope=case.scope, stage_version=1
        )
        texts: list[str] = []
        main_tokens = 0
        for _ in case.expected.generated_texts:
            res = await llm_main.chat([{"role": "user", "content": _GEN_USER}])
            texts.append(res.content)
            main_tokens += int((res.usage or {}).get("total_tokens", 0))
        closed = await close_retrieval_trace(ctx, outcome, texts)
        wall_ms = int((time.perf_counter() - t0) * 1000)

        trace = await TraceDAO(db).get(outcome.trace_id)
        cands = trace.candidates_obj()
        cand_ids = {c.entry_id for c in cands if c.entry_id}  # error 行 entry_id=""
        injected = list(outcome.injected)
        injected_set = set(injected)
        expected = set(case.expected.entries)

        aux_calls = aux_tokens = 0
        if outcome.snapshot_id:
            usage = (await SnapshotDAO(db).get(outcome.snapshot_id)).usage_obj()
            aux = usage.get("aux", {}).get("retrieval", {})
            aux_calls = int(aux.get("calls", 0))
            aux_tokens = int(aux.get("prompt_tokens", 0)) + int(
                aux.get("completion_tokens", 0)
            )

        covered: set[str] = set()
        for eid in injected:
            covered |= set(case.expected.entry_clauses.get(eid, ()))
        clause_total = set(case.clauses)

        return CaseReport(
            name=case.name,
            stage=case.stage,
            recall_hit=_ratio(len(expected & cand_ids), len(expected)),
            inject_hit=_ratio(len(expected & injected_set), len(expected)),
            reference_rate=_ratio(len(closed.referenced), len(injected_set)),
            clause_coverage=_ratio(len(covered & clause_total), len(clause_total)),
            aux_calls=aux_calls,
            aux_tokens=aux_tokens,
            main_tokens=main_tokens,
            wall_ms=wall_ms,
            degraded_steps=len(outcome.degraded),
            funnel=funnel_counts(cands),
            injected=injected,
            referenced=closed.referenced,
            hallucinated=closed.hallucinated,
            weak_refs=closed.weak_refs,
        )
    finally:
        if db is not None:
            db.close()
        shutil.rmtree(tmp, ignore_errors=True)
