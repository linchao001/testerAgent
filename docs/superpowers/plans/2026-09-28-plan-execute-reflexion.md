# Plan-Execute + Reflexion Runtime Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the fixed five-stage case_designer main graph with a control-loop agent (dynamic Plan-Execute + Reflexion), serial in-process subtasks for review, evolved artifacts/APIs, and a conversation-first Session UI.

**Architecture:** A LangGraph control graph (`plan → dispatch → execute_step ↔ tools/subtask → await_human? → reflect → replan|next|end`) owns orchestration. Old stage node functions become capability tools. Review runs as nested subgraphs under `{task_thread}::sub::{id}` (serial, no nested spawn). Frontend SessionPage is the primary interaction surface.

**Tech Stack:** Python 3.11+, LangGraph + LangChain tools, Pydantic domain models, SQLite migrations, FastAPI, React/Vitest (existing web app).

**Spec:** [docs/superpowers/specs/2026-09-28-plan-execute-reflexion-design.md](../specs/2026-09-28-plan-execute-reflexion-design.md)

## Global Constraints

- Single machine, single user, single process, single worker (`TESTER_AGENT_SINGLE_WORKER=1`).
- Human gates configurable; defaults `human_gate_link=true`, `human_gate_point=true`, `human_gate_review=true`.
- Subtasks: serial only; no nested `spawn_subtask`; thread id `{task_thread}::sub::{subtask_id}`.
- Reuse task status `waiting_confirm` + confirm body `gate_kind` (no new `waiting_review` state).
- `AgentPlan` stored as `artifact.kind=agent_plan`; task holds `current_plan_artifact_id`.
- Large text stays out of LangGraph checkpoints (files + artifact payload refs).
- Tools must not reach `ReMeWriter`.
- No hot-migration of old task runs; new runs use the control graph.
- TDD per task: failing test → implement → pass → commit (commits only when the user/session allows; if blocked, stop after green tests and note pending commit).
- Prefer wrapping existing node pure functions over rewriting generation logic in v1.

**Execution waves (same plan, sequential):**

| Wave | Tasks | Testable outcome |
|---|---|---|
| A Runtime core | 1–4 | Control graph compiles; plan/reflect/gates unit-tested with stub execute |
| B Capabilities + review | 5–7 | Real generate/review subtasks; serial spawn; proposals |
| C API + UI + docs | 8–11 | Confirm/SSE/SessionPage end-to-end; docs aligned |

---

## File map

| Path | Responsibility |
|---|---|
| `server/tester_agent/domain.py` | Add `AgentPlan`, `PlanStep`, `PlanStepKind`, `SubtaskResult`, `ReviewProposal`, `HumanDecision`, extend `MessageKind` |
| `server/tester_agent/store/migrations/004_plan_execute.sql` | `artifact.kind`, `task.current_plan_artifact_id`, `subtask` table |
| `server/tester_agent/store/db.py` | Runtime config defaults for gates/reflect/replan/subtask timeout |
| `server/tester_agent/store/models.py` | ArtifactDAO kind support; SubtaskDAO; TaskDAO plan pointer |
| `server/tester_agent/graph/state.py` | New `TaskState` fields |
| `server/tester_agent/graph/control/` | Control graph package (plan/dispatch/execute/reflect/gates) |
| `server/tester_agent/graph/main_graph.py` | Switch to build control graph (keep old builder as `_legacy` only if tests need it briefly, then delete) |
| `server/tester_agent/graph/subtasks/` | Subtask runner + review graph templates |
| `server/tester_agent/tools/capabilities.py` | Capability tools wrapping old node functions |
| `server/tester_agent/tools/orchestration.py` | `spawn_subtask`, `await_human_confirm` (main agent only) |
| `server/tester_agent/tools/registry.py` | Compose sandbox + capability + orchestration tool sets |
| `server/tester_agent/runtime/runner.py` | Interrupt classification for `gate_kind`; subtask cancel |
| `server/tester_agent/api/tasks.py` | Plan/subtasks/confirm `gate_kind`; SSE event names |
| `web/src/pages/SessionPage.tsx` (evolve ChatPage) | Conversation-first UI |
| `web/src/components/session/*` | PlanCard, StepProgress, ReviewProposalCard, GateConfirmCard |
| Docs | PRD / tech-design / detailed-design / handoff / builtin-tools spec principle update |

---

### Task 1: Domain models for Plan / Subtask / Review

**Files:**
- Modify: `server/tester_agent/domain.py`
- Test: `server/tests/test_domain_plan_execute.py`

**Interfaces:**
- Produces:
  - `class PlanStepKind(StrEnum)` with values: `intake_parse`, `coverage_design`, `point_design`, `case_generate`, `review_coverage`, `review_quality`, `review_adoption`, `repair`, `await_human`
  - `class PlanStep(BaseModel)`: `step_id: str`, `kind: PlanStepKind`, `goal: str`, `input_refs: list[str] = []`, `output_ref: str | None = None`, `status: Literal["pending","running","done","failed","skipped"] = "pending"`, `requires_confirm: bool = False`, `max_reflect: int = 2`
  - `class AgentPlan(BaseModel)`: `plan_id: str`, `version: int`, `goal: str`, `steps: list[PlanStep]`, `status: Literal["draft","active","completed","failed"] = "active"`, `replan_count: int = 0`
  - `class SubtaskResult(BaseModel)`: `subtask_id: str`, `kind: str`, `thread_id: str`, `status: Literal["running","done","failed","cancelled"]`, `summary: str = ""`, `output_ref: str | None = None`
  - `class ReviewProposalItem(BaseModel)`: `target_id: str`, `action: Literal["adopt","edit_adopt","reject","add_point","add_case","repair"]`, `rationale: str`, `confidence: float = 0.5`, `patch: dict | None = None`
  - `class ReviewProposal(BaseModel)`: `scope: str`, `items: list[ReviewProposalItem]`, `matrix_ref: str | None = None`, `degraded: bool = False`
  - `class HumanDecision(BaseModel)`: `gate_kind: Literal["plan_confirm","review_decision"]`, `action: Literal["confirm","modify","reject_rerun"]`, `artifact_id: str`, `payload: dict | None = None`
  - Extend `MessageKind` with `PLAN_REVISION`, `REVIEW_DECISION`, `GATE_CONFIRM`

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_domain_plan_execute.py
from tester_agent.domain import AgentPlan, PlanStep, PlanStepKind, ReviewProposal, MessageKind

def test_agent_plan_roundtrip():
    plan = AgentPlan(
        plan_id="p1",
        version=1,
        goal="cover login",
        steps=[
            PlanStep(step_id="s1", kind=PlanStepKind.COVERAGE_DESIGN, goal="links", requires_confirm=True),
            PlanStep(step_id="s2", kind=PlanStepKind.CASE_GENERATE, goal="cases"),
        ],
    )
    data = plan.model_dump()
    assert AgentPlan.model_validate(data).steps[0].kind == PlanStepKind.COVERAGE_DESIGN

def test_message_kind_extensions():
    assert MessageKind.PLAN_REVISION.value == "plan_revision"
    assert MessageKind.REVIEW_DECISION.value == "review_decision"
    assert MessageKind.GATE_CONFIRM.value == "gate_confirm"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd server && pytest -p no:zframe tests/test_domain_plan_execute.py -v`
Expected: FAIL (import / missing types)

- [ ] **Step 3: Implement domain types in `domain.py`**

Add enums/models as listed in Interfaces. Keep existing `LinkPlan`/`PointPlan`/`CoverageMatrix` unchanged for payload reuse.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd server && pytest -p no:zframe tests/test_domain_plan_execute.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add server/tester_agent/domain.py server/tests/test_domain_plan_execute.py
git commit -m "feat: add AgentPlan/Subtask/Review domain models"
```

---

### Task 2: Schema + runtime config for plan/subtask/gates

**Files:**
- Create: `server/tester_agent/store/migrations/004_plan_execute.sql`
- Modify: `server/tester_agent/store/db.py` (`DEFAULT_RUNTIME_CONFIG`)
- Modify: `server/tester_agent/store/models.py` (TaskDAO / ArtifactDAO / new SubtaskDAO)
- Test: `server/tests/test_migration_004_plan_execute.py`

**Interfaces:**
- Produces:
  - Migration adds: `task.current_plan_artifact_id TEXT`, `stage_artifact.kind TEXT` (nullable initially; app writes kind going forward), table `subtask (id, task_id, thread_id, kind, status, result_artifact_id, created_at, updated_at)`
  - Runtime defaults: `human_gate_link=True`, `human_gate_point=True`, `human_gate_review=True`, `reflect_max_per_step=2`, `replan_max=3`, `subtask_timeout_sec=600`
  - `SubtaskDAO.create/get/list_by_task/update_status`

- [ ] **Step 1: Write the failing migration test**

```python
# server/tests/test_migration_004_plan_execute.py
from pathlib import Path
from tester_agent.store.db import run_migrations, DEFAULT_RUNTIME_CONFIG, _connect

def test_004_adds_plan_columns_and_subtask(tmp_path: Path):
    db = tmp_path / "app.db"
    run_migrations(db)
    conn = _connect(db)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(task)").fetchall()}
    assert "current_plan_artifact_id" in cols
    kinds = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='subtask'"
    ).fetchone()
    assert kinds is not None
    art_cols = {r[1] for r in conn.execute("PRAGMA table_info(stage_artifact)").fetchall()}
    assert "kind" in art_cols

def test_runtime_config_gate_defaults():
    assert DEFAULT_RUNTIME_CONFIG["human_gate_link"] is True
    assert DEFAULT_RUNTIME_CONFIG["reflect_max_per_step"] == 2
    assert DEFAULT_RUNTIME_CONFIG["replan_max"] == 3
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd server && pytest -p no:zframe tests/test_migration_004_plan_execute.py -v`
Expected: FAIL

- [ ] **Step 3: Add migration SQL + config + minimal DAO**

```sql
-- 004_plan_execute.sql
ALTER TABLE task ADD COLUMN current_plan_artifact_id TEXT;
ALTER TABLE stage_artifact ADD COLUMN kind TEXT;
CREATE TABLE IF NOT EXISTS subtask (
  id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL,
  thread_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  status TEXT NOT NULL,
  result_artifact_id TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_subtask_task ON subtask(task_id, created_at);
```

Update `DEFAULT_RUNTIME_CONFIG` keys. Implement `SubtaskDAO` with create/get/list/update_status. Extend Artifact insert to accept optional `kind` (default from legacy `stage` when absent).

- [ ] **Step 4: Run tests**

Run: `cd server && pytest -p no:zframe tests/test_migration_004_plan_execute.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add server/tester_agent/store/migrations/004_plan_execute.sql server/tester_agent/store/db.py server/tester_agent/store/models.py server/tests/test_migration_004_plan_execute.py
git commit -m "feat: migrate schema for AgentPlan pointer and subtasks"
```

---

### Task 3: Control graph skeleton (plan / dispatch / execute stub / reflect pass-through)

**Files:**
- Create: `server/tester_agent/graph/control/__init__.py`
- Create: `server/tester_agent/graph/control/graph.py`
- Create: `server/tester_agent/graph/control/nodes.py`
- Modify: `server/tester_agent/graph/state.py`
- Modify: `server/tester_agent/graph/main_graph.py` (delegate `build_graph` → control graph)
- Modify: `server/tester_agent/graph/registry.py` (register control graph; drop STAGE_NODES wiring)
- Test: `server/tests/test_control_graph_topology.py`

**Interfaces:**
- Consumes: `AgentPlan`, `PlanStep`, `PlanStepKind` from Task 1
- Produces:
  - `build_control_graph(checkpointer=None, *, caps: ControlCaps | None = None) -> CompiledStateGraph`
  - `TaskState` fields: `agent_plan`, `plan_cursor`, `artifacts`, `subtask`, `reflection_log`, `human_gates`, `clarification_questions`
  - Stub `plan_node` writes a deterministic 4-step plan when `agent_plan` missing
  - Stub `execute_step` marks current step `done` without LLM
  - `reflect_node` returns `pass` when step done (real rules in Task 4)

- [ ] **Step 1: Write failing topology test**

```python
# server/tests/test_control_graph_topology.py
from tester_agent.graph.control.graph import build_control_graph

def test_control_graph_compiles_and_has_nodes():
    g = build_control_graph(checkpointer=None)
    names = set(g.get_graph().nodes)
    for n in ("plan", "dispatch", "execute_step", "reflect"):
        assert n in names
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd server && pytest -p no:zframe tests/test_control_graph_topology.py -v`
Expected: FAIL (module missing)

- [ ] **Step 3: Implement state + stub control graph**

```python
# graph/control/graph.py (sketch)
from langgraph.graph import END, START, StateGraph
from ..state import TaskState
from .nodes import plan_node, dispatch_node, execute_step_node, reflect_node, route_after_dispatch, route_after_reflect

def build_control_graph(checkpointer=None, *, caps=None):
    g = StateGraph(TaskState)
    g.add_node("plan", plan_node)
    g.add_node("dispatch", dispatch_node)
    g.add_node("execute_step", execute_step_node)
    g.add_node("reflect", reflect_node)
    g.add_edge(START, "plan")
    g.add_edge("plan", "dispatch")
    g.add_conditional_edges("dispatch", route_after_dispatch, {"execute": "execute_step", "end": END})
    g.add_edge("execute_step", "reflect")
    g.add_conditional_edges("reflect", route_after_reflect, {"dispatch": "dispatch", "plan": "plan", "execute": "execute_step", "end": END})
    kwargs = {}
    if checkpointer is not None:
        kwargs["checkpointer"] = checkpointer
    return g.compile(**kwargs)
```

Stub plan steps: `intake_parse` → `coverage_design(requires_confirm)` → `point_design(requires_confirm)` → `case_generate` → `review_coverage` → `review_quality` → `review_adoption`.

Update `main_graph.build_graph` to call `build_control_graph`. Update registry accordingly. Leave old stage node modules in place for Task 5 wrapping.

- [ ] **Step 4: Run topology + a smoke invoke without checkpointer**

```python
async def test_stub_run_completes():
    g = build_control_graph()
    out = await g.ainvoke({"task_id": "t1", "graph_run_id": "r1", "workspace_id": "w1", "human_gates": {"link": False, "point": False, "review": False}})
    assert out["agent_plan"]["status"] == "completed"
```

Run: `cd server && pytest -p no:zframe tests/test_control_graph_topology.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add server/tester_agent/graph/state.py server/tester_agent/graph/control server/tester_agent/graph/main_graph.py server/tester_agent/graph/registry.py server/tests/test_control_graph_topology.py
git commit -m "feat: add Plan-Execute control graph skeleton"
```

---

### Task 4: Reflexion rules + human gate interrupt

**Files:**
- Create: `server/tester_agent/graph/control/reflect.py`
- Create: `server/tester_agent/graph/control/gates.py`
- Modify: `server/tester_agent/graph/control/nodes.py`
- Test: `server/tests/test_control_reflect_gates.py`

**Interfaces:**
- Consumes: `AgentPlan`, `human_gates` from state; `reflect_max_per_step`, `replan_max` from caps/config injected via `configurable.ctx`
- Produces:
  - `decide_reflection(plan, step, *, reflection_count, replan_count, human_gates) -> Literal["pass","repair","replan"]`
  - Rules: (1) if `requires_confirm` and matching gate enabled and artifact not `confirmed_by=user` → `replan` inserting/ensuring confirm; (2) if step `failed` and reflection_count < max → `repair`; (3) if replan_count >= max → surface fail via plan.status; (4) else `pass`
  - `await_human` via `langgraph.types.interrupt({"gate_kind": "plan_confirm", "artifact_id": ..., "step_id": ...})` when execute finishes a confirming step and gate on

- [ ] **Step 1: Write failing decision-table tests**

```python
# server/tests/test_control_reflect_gates.py
from tester_agent.domain import AgentPlan, PlanStep, PlanStepKind
from tester_agent.graph.control.reflect import decide_reflection

def _plan(confirm=True, confirmed=False):
    return AgentPlan(
        plan_id="p", version=1, goal="g",
        steps=[PlanStep(step_id="s1", kind=PlanStepKind.COVERAGE_DESIGN, goal="x",
                        requires_confirm=confirm, status="done",
                        output_ref="art1" if confirmed else "art1")],
    )

def test_skipping_link_gate_forces_replan():
    d = decide_reflection(
        _plan(confirm=True), _plan().steps[0],
        reflection_count=0, replan_count=0,
        human_gates={"link": True, "point": True, "review": True},
        artifact_confirmed_by=None,
    )
    assert d == "replan"

def test_confirmed_gate_passes():
    d = decide_reflection(
        _plan(), _plan().steps[0],
        reflection_count=0, replan_count=0,
        human_gates={"link": True, "point": True, "review": True},
        artifact_confirmed_by="user",
    )
    assert d == "pass"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd server && pytest -p no:zframe tests/test_control_reflect_gates.py -v`
Expected: FAIL

- [ ] **Step 3: Implement `decide_reflection` + wire interrupt in execute/reflect path**

When gate required: call `interrupt(...)` **before** marking step fully done / before reflect pass, so Runner enters `waiting_confirm`. Map `coverage_design`→link gate, `point_design`→point gate, `review_*`→review gate.

- [ ] **Step 4: Run tests**

Run: `cd server && pytest -p no:zframe tests/test_control_reflect_gates.py tests/test_control_graph_topology.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add server/tester_agent/graph/control server/tests/test_control_reflect_gates.py
git commit -m "feat: Reflexion rules and configurable human gates"
```

---

### Task 5: Capability tools wrapping legacy stage functions

**Files:**
- Create: `server/tester_agent/tools/capabilities.py`
- Modify: `server/tester_agent/tools/registry.py`
- Modify: `server/tester_agent/graph/control/nodes.py` (`execute_step` calls capabilities / tool_agent for tool-enabled steps)
- Test: `server/tests/test_tool_capabilities.py`

**Interfaces:**
- Consumes: existing `intake`/`link_identify`/`point_write`/`generate_case_batch`/`build coverage matrix` helpers
- Produces LangChain tools (main agent set):
  - `retrieve_kb(query: str) -> str` (thin wrapper over existing retrieval ops; may stub in unit test)
  - `run_intake_parse() -> str` (artifact id)
  - `run_coverage_design() -> str`
  - `run_point_design() -> str`
  - `generate_cases_batch(point_ids: list[str]) -> str`
  - `build_coverage_matrix() -> str`
  - `write_artifact(kind: str, payload_json: str) -> str`
- `build_case_designer_tools` gains `include_capabilities: bool = False` / `include_orchestration: bool = False` flags so chat stays sandbox-only by default

- [ ] **Step 1: Write failing test for registry flags**

```python
def test_capabilities_not_in_default_chat_tools(tmp_path):
    from tester_agent.tools.registry import build_case_designer_tools, ToolBuildContext
    tools = build_case_designer_tools(ToolBuildContext(owner_id="c:1", workspace_root=tmp_path, runtime_config={}))
    names = {t.name for t in tools}
    assert "bash" in names
    assert "run_coverage_design" not in names

def test_capabilities_included_when_flagged(tmp_path):
    from tester_agent.tools.registry import build_case_designer_tools, ToolBuildContext
    tools = build_case_designer_tools(
        ToolBuildContext(owner_id="t:1", workspace_root=tmp_path, runtime_config={}),
        include_capabilities=True,
    )
    assert "generate_cases_batch" in {t.name for t in tools}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd server && pytest -p no:zframe tests/test_tool_capabilities.py -v`
Expected: FAIL

- [ ] **Step 3: Implement capability factories; execute_step dispatches by `PlanStep.kind`**

For v1, `execute_step` may call capability functions directly (not only via LLM tool loop) for deterministic kinds (`intake_parse`, `case_generate`, …), and optionally run `tool_agent` gather first when `enable_tools_stages` equivalent config is on. Prefer direct dispatch for reliability; keep tool wrappers for agent-driven repair steps.

- [ ] **Step 4: Run tests**

Run: `cd server && pytest -p no:zframe tests/test_tool_capabilities.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add server/tester_agent/tools/capabilities.py server/tester_agent/tools/registry.py server/tester_agent/graph/control/nodes.py server/tests/test_tool_capabilities.py
git commit -m "feat: capability tools wrapping legacy stage logic"
```

---

### Task 6: Serial `spawn_subtask` runtime

**Files:**
- Create: `server/tester_agent/graph/subtasks/__init__.py`
- Create: `server/tester_agent/graph/subtasks/runner.py`
- Create: `server/tester_agent/tools/orchestration.py`
- Modify: `server/tester_agent/tools/registry.py`
- Test: `server/tests/test_subtask_runner.py`

**Interfaces:**
- Consumes: SubtaskDAO, checkpointer from AppContext, review templates (stub ok until Task 7)
- Produces:
  - `async def run_subtask(*, ctx, parent_thread_id: str, kind: str, goal: str, input_refs: list[str]) -> SubtaskResult`
  - thread id: `f"{parent_thread_id}::sub::{subtask_id}"`
  - Raises `SubtaskBusyError` if task already has `status=running` subtask
  - Orchestration tool `spawn_subtask` only when `include_orchestration=True`; child tool sets must omit it

- [ ] **Step 1: Write failing tests**

```python
import pytest
from tester_agent.graph.subtasks.runner import run_subtask, SubtaskBusyError

@pytest.mark.asyncio
async def test_serial_subtask_rejects_second(monkeypatch, fake_ctx):
    # first starts running; second raises
    ...

@pytest.mark.asyncio
async def test_thread_id_suffix(fake_ctx):
    result = await run_subtask(ctx=fake_ctx, parent_thread_id="thr-1", kind="review_coverage", goal="g", input_refs=[])
    assert result.thread_id.startswith("thr-1::sub::")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd server && pytest -p no:zframe tests/test_subtask_runner.py -v`
Expected: FAIL

- [ ] **Step 3: Implement runner + orchestration tool**

Persist subtask row before invoke; on completion write `SubtaskResult` artifact and update row. Honor `subtask_timeout_sec` with `asyncio.wait_for`. Propagate `cancel_requested` by aborting invoke when ctx flag set.

- [ ] **Step 4: Run tests**

Run: `cd server && pytest -p no:zframe tests/test_subtask_runner.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add server/tester_agent/graph/subtasks server/tester_agent/tools/orchestration.py server/tester_agent/tools/registry.py server/tests/test_subtask_runner.py
git commit -m "feat: serial in-process subtask runner"
```

---

### Task 7: Review subtask graphs + proposal → repair/adoption

**Files:**
- Create: `server/tester_agent/graph/subtasks/review_coverage.py`
- Create: `server/tester_agent/graph/subtasks/review_quality.py`
- Create: `server/tester_agent/graph/subtasks/review_adoption.py`
- Create: `server/tester_agent/graph/control/apply_decision.py`
- Test: `server/tests/test_review_subtasks.py`

**Interfaces:**
- Produces:
  - Each review graph returns `ReviewProposal` artifact id via `SubtaskResult.output_ref`
  - `review_coverage` calls `build_coverage_matrix` tool/helper then LLM (or heuristic stub in tests) to fill items
  - `apply_human_decision(ctx, decision: HumanDecision) -> None`: confirm/modify artifact; on review accept insert `repair`/`case_generate` steps or apply `review_status` updates; on `reject_rerun` re-queue same review step

- [ ] **Step 1: Write failing tests with stub LLM**

```python
def test_coverage_review_emits_proposal(stub_matrix, stub_llm):
    ...

def test_adoption_decision_updates_review_status(db_cases):
    ...
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd server && pytest -p no:zframe tests/test_review_subtasks.py -v`
Expected: FAIL

- [ ] **Step 3: Implement three review templates + apply_decision**

Wire `execute_step` for `review_*` kinds to `run_subtask`. After human confirm of review proposal, `apply_human_decision` mutates plan/cases.

- [ ] **Step 4: Run tests**

Run: `cd server && pytest -p no:zframe tests/test_review_subtasks.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add server/tester_agent/graph/subtasks server/tester_agent/graph/control/apply_decision.py server/tests/test_review_subtasks.py
git commit -m "feat: review subtasks and human decision application"
```

---

### Task 8: Runner + Tasks API (plan, subtasks, confirm gate_kind, SSE)

**Files:**
- Modify: `server/tester_agent/runtime/runner.py`
- Modify: `server/tester_agent/api/tasks.py`
- Modify: `server/tester_agent/api/conversations.py` (message kinds)
- Test: `server/tests/test_api_plan_execute.py`

**Interfaces:**
- Produces HTTP:
  - `GET /api/v1/tasks/{id}/plan` → `AgentPlan`
  - `GET /api/v1/tasks/{id}/subtasks` → list
  - `GET /api/v1/tasks/{id}/review-proposals/{artifact_id}` → `ReviewProposal`
  - `POST /api/v1/tasks/{id}/confirm` body includes `gate_kind: plan_confirm|review_decision`, `action`, `artifact_id`, optional `payload`
- SSE emits: `plan_updated`, `step_started`, `step_finished`, `subtask_started`, `subtask_finished`, `reflection_result`, `review_proposal_ready`, `human_gate_waiting` (keep `clarification_needed`; map or alias old `checkpoint_waiting` → `human_gate_waiting` for one release)
- Runner: on interrupt payload with `gate_kind`, transition `waiting_confirm` and emit `human_gate_waiting`

- [ ] **Step 1: Write failing API tests (httpx/ASGI as existing suite)**

```python
async def test_confirm_requires_gate_kind(client, task_waiting):
    r = await client.post(f"/api/v1/tasks/{task_waiting}/confirm", json={"action": "confirm", "artifact_id": "a1"})
    assert r.status_code == 422

async def test_get_plan_returns_agent_plan(client, task_with_plan):
    r = await client.get(f"/api/v1/tasks/{task_with_plan}/plan")
    assert r.status_code == 200
    assert "steps" in r.json()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd server && pytest -p no:zframe tests/test_api_plan_execute.py -v`
Expected: FAIL

- [ ] **Step 3: Implement routes + runner interrupt mapping + apply_decision on confirm**

Update existing confirm handler rather than duplicating. Ensure modify writes new artifact version + plan revision message kind `gate_confirm` / `plan_revision`.

- [ ] **Step 4: Run API tests + adjust broken legacy API tests that assumed stage gates**

Run: `cd server && pytest -p no:zframe tests/test_api_plan_execute.py tests/test_api_a.py -v`
Expected: plan_execute PASS; update `test_api_a.py` assertions that hard-code old stage names as needed in this same task.

- [ ] **Step 5: Commit**

```bash
git add server/tester_agent/runtime/runner.py server/tester_agent/api/tasks.py server/tester_agent/api/conversations.py server/tests/test_api_plan_execute.py server/tests/test_api_a.py
git commit -m "feat: plan/subtask APIs and gate_kind confirm"
```

---

### Task 9: Frontend SessionPage (conversation-first)

**Files:**
- Create/Modify: `web/src/pages/SessionPage.tsx` (evolve from `ChatPage.tsx`; re-export route)
- Create: `web/src/components/session/PlanCard.tsx`
- Create: `web/src/components/session/GateConfirmCard.tsx`
- Create: `web/src/components/session/ReviewProposalCard.tsx`
- Create: `web/src/components/session/StepProgress.tsx`
- Modify: `web/src/api/domain.ts`, `web/src/api/endpoints.ts`, `web/src/stores/taskStream.ts`
- Modify: `web/src/router.tsx` (primary `/`; soft-deprecate `/confirm` `/workbench` to redirects or secondary)
- Test: `web/src/pages/SessionPage.test.tsx` (+ component tests)

**Interfaces:**
- Consumes new SSE events and confirm/plan endpoints
- Produces UI: timeline with inline gate confirm (embed Link/Point editors), review proposal list with editable actions, plan/step side panel

- [ ] **Step 1: Write failing Vitest for GateConfirmCard submit payload**

```ts
// GateConfirmCard.test.tsx
it('posts gate_kind plan_confirm on confirm', async () => {
  // render with artifact; click confirm; expect confirmTask called with gate_kind: 'plan_confirm'
})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd web && npm test -- GateConfirmCard`
Expected: FAIL

- [ ] **Step 3: Implement components + SessionPage wiring + taskStream event normalization**

Port ClarificationCard behavior. Checkpoint banner becomes inline `GateConfirmCard` when `human_gate_waiting` arrives. Keep case drawer for single-case edit.

- [ ] **Step 4: Run frontend tests**

Run: `cd web && npm test`
Expected: PASS (update obsolete ChatPage-only tests)

- [ ] **Step 5: Commit**

```bash
git add web/src
git commit -m "feat: conversation-first SessionPage with inline gates"
```

---

### Task 10: Scenario integration tests (gates on/off, review reject, cancel in subtask)

**Files:**
- Create: `server/tests/test_scenario_plan_execute.py`
- Modify: retire or skip obsolete five-stage graph scenarios; note in handoff

**Interfaces:**
- Produces scenario coverage from spec §8/§11:
  1. Default gates on: interrupt at coverage_design and point_design
  2. All gates off: completes with optional degraded proposal
  3. Review `reject_rerun` re-queues review step
  4. Cancel while subtask running → subtask cancelled, task aborted

- [ ] **Step 1: Write failing scenario tests with fake LLM/caps**

```python
@pytest.mark.asyncio
async def test_default_gates_interrupt_twice(app_client, monkeypatch):
    ...

@pytest.mark.asyncio
async def test_cancel_during_subtask(app_client, monkeypatch):
    ...
```

- [ ] **Step 2: Run to verify fail / implement fixtures / pass**

Run: `cd server && pytest -p no:zframe tests/test_scenario_plan_execute.py -v`
Expected: PASS

- [ ] **Step 3: Commit**

```bash
git add server/tests/test_scenario_plan_execute.py
git commit -m "test: plan-execute scenarios for gates review and cancel"
```

---

### Task 11: Docs sync

**Files:**
- Modify: `docs/PRD.md` (conversation-first interaction)
- Modify: `docs/tech-design.md` (control graph decision replaces fixed stages)
- Modify: `docs/detailed-design.md` (state, API, events, artifact kind)
- Modify: `docs/plan/handoff.md` (paradigm switch progress)
- Modify: `docs/superpowers/specs/2026-09-28-builtin-tools-design.md` (replace “不替换用例主图” with control-graph + tool subgraph)
- Modify: `README.md` (short paradigm blurb)

- [ ] **Step 1: Update each doc section referenced in spec §10** with concrete wording (no TBD)

- [ ] **Step 2: Commit**

```bash
git add docs README.md
git commit -m "docs: align PRD/design with Plan-Execute paradigm"
```

---

## Spec coverage checklist (self-review)

| Spec section | Tasks |
|---|---|
| §0 decisions / §1 goals | Global constraints + all waves |
| §3 control loop + TaskState + step kinds | 1, 3, 4 |
| §4 tools / subtasks / review / SSE | 5, 6, 7, 8 |
| §5 domain / DB / API / config | 1, 2, 8 |
| §6 Session UI | 9 |
| §7 errors / cancel / degraded | 4, 6, 7, 10 |
| §8 tests | per-task + 10 |
| §9 non-goals | Global constraints |
| §10 docs | 11 |
| §11 success criteria | 10 + 9 |

**Placeholder scan:** none intentional. Fake LLM / stub caps allowed only in tests.

**Type consistency:** `gate_kind` values `plan_confirm|review_decision`; step kinds from `PlanStepKind`; subtask thread suffix `::sub::`.
