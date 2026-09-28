# Builtin Tools (bash + str_replace_editor) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the case_designer agent harness-aligned `bash` and `str_replace_editor` tools, callable from chat and (opt-in) pipeline nodes, via LangChain Tools + a LangGraph tool-agent subgraph.

**Architecture:** Shared `WorkspaceSandbox` + persistent shell + editor implementations wrapped as LangChain `BaseTool`s; compiled `tool_agent_graph` (`ChatOpenAI.bind_tools` ↔ `ToolNode`); chat `POST .../messages` runs the subgraph; selected graph nodes optionally run it before json_schema generation. ReMeWriter stays unreachable from tools.

**Tech Stack:** Python 3.11+, LangGraph, langchain-core tools, langchain-openai `ChatOpenAI`, FastAPI, existing SQLite/`FileStore`, Vitest/React for minimal UI.

**Spec:** [docs/superpowers/specs/2026-09-28-builtin-tools-design.md](../specs/2026-09-28-builtin-tools-design.md)

## Global Constraints

- Tool names/params/error semantics align with deepseek-harness (`bash`, `str_replace_editor` with `view|create|str_replace|insert`).
- Sandbox root: `data/workspaces/{workspace_id}/`; deny writes under `**/snapshots/**`.
- Owner isolation: `conversation:{id}` vs `task:{id}` for persistent bash.
- Tool path uses LangChain/LangGraph only (no bespoke OpenAI tool_calls parser as primary path).
- Do not replace the case_designer main `StateGraph` with `create_react_agent`.
- Pipeline tools default **off** (`agent.config.enable_tools_stages` empty); chat tools **on** for `kind=chat`.
- Windows shell backend: `auto` → bash → pwsh; tool name remains `bash`.
- Single worker / existing layering: `api → runtime/graph → adapters → store`; tools package may be imported by graph and api.
- TDD: failing test → implement → pass → commit per task.
- Commits only when the user/session allows; if commit steps are blocked, stop after green tests and note pending commit.

---

## File map

| Path | Responsibility |
|---|---|
| `server/tester_agent/tools/__init__.py` | Public exports |
| `server/tester_agent/tools/sandbox.py` | Path resolve, write-protect snapshots, workspace root |
| `server/tester_agent/tools/str_replace_editor.py` | Editor commands + LC tool factory |
| `server/tester_agent/tools/bash_persistent.py` | Persistent shell sessions + LC tool factory |
| `server/tester_agent/tools/shell_backend.py` | Detect bash / pwsh argv |
| `server/tester_agent/tools/registry.py` | `build_case_designer_tools(ctx) -> list[BaseTool]` |
| `server/tester_agent/tools/trace.py` | `ToolTraceEntry` / digest helpers |
| `server/tester_agent/adapters/llm.py` | Add `get_chat_model(...)` → `ChatOpenAI` |
| `server/tester_agent/graph/tool_agent.py` | Compile/run `tool_agent_graph` |
| `server/tester_agent/graph/tool_gather.py` | Optional pre-node tool gather for pipeline |
| `server/tester_agent/runtime/chat_agent.py` | Chat orchestration helper |
| `server/tester_agent/api/conversations.py` | Chat path invokes tool agent |
| `server/tester_agent/store/db.py` | Extend `DEFAULT_RUNTIME_CONFIG` + `BUILTIN_AGENT_CONFIG` |
| `server/tester_agent/cli.py` | `check` reports shell backend |
| `server/tests/test_tool_*.py` | Unit/API tests listed per task |
| `web/src/components/chat/*` | Minimal tool_trace display |
| Docs | PRD / tech-design / detailed-design / README backfill |

---

### Task 1: WorkspaceSandbox + runtime config keys

**Files:**
- Create: `server/tester_agent/tools/__init__.py`
- Create: `server/tester_agent/tools/sandbox.py`
- Modify: `server/tester_agent/store/db.py` (`DEFAULT_RUNTIME_CONFIG`, `BUILTIN_AGENT_CONFIG`)
- Test: `server/tests/test_tool_sandbox.py`

**Interfaces:**
- Produces:
  - `class WorkspaceSandbox` with `__init__(self, workspace_root: Path)`, `resolve(self, path: str) -> Path`, `assert_writable(self, path: Path) -> None`, `relpath(self, path: Path) -> str`
  - Raises `SandboxError` (subclass of `ValueError`) on escape / write-protect
  - Runtime defaults: `tool_bash_timeout_ms=300000`, `tool_max_output_chars=16000`, `tool_agent_max_steps=12`, `tool_shell_backend="auto"`
  - Agent default: `enable_tools_stages=[]`

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_tool_sandbox.py
from pathlib import Path
import pytest
from tester_agent.tools.sandbox import WorkspaceSandbox, SandboxError

def test_resolve_relative_under_root(tmp_path: Path):
    sb = WorkspaceSandbox(tmp_path)
    (tmp_path / "a").mkdir()
    p = sb.resolve("a/b.txt")
    assert p == (tmp_path / "a" / "b.txt").resolve()

def test_reject_escape(tmp_path: Path):
    sb = WorkspaceSandbox(tmp_path)
    with pytest.raises(SandboxError):
        sb.resolve("../outside.txt")

def test_snapshots_not_writable(tmp_path: Path):
    sb = WorkspaceSandbox(tmp_path)
    target = tmp_path / "t1" / "snapshots" / "x.jsonl"
    target.parent.mkdir(parents=True)
    target.write_text("x", encoding="utf-8")
    with pytest.raises(SandboxError):
        sb.assert_writable(sb.resolve("t1/snapshots/x.jsonl"))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd server && python -m pytest tests/test_tool_sandbox.py -v`  
Expected: FAIL (module not found)

- [ ] **Step 3: Implement sandbox + config defaults**

Implement `resolve` via `Path.resolve()` requiring workspace root is an ancestor; `assert_writable` raises if any path part equals `snapshots`. Extend `DEFAULT_RUNTIME_CONFIG` / `BUILTIN_AGENT_CONFIG` in `store/db.py` with the four tool keys and `enable_tools_stages: []`.

- [ ] **Step 4: Run tests**

Run: `cd server && python -m pytest tests/test_tool_sandbox.py -v`  
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add server/tester_agent/tools/sandbox.py server/tester_agent/tools/__init__.py server/tester_agent/store/db.py server/tests/test_tool_sandbox.py
git commit -m "$(cat <<'EOF'
feat(tools): add workspace sandbox and tool runtime defaults

EOF
)"
```

---

### Task 2: str_replace_editor (core + LangChain tool)

**Files:**
- Create: `server/tester_agent/tools/str_replace_editor.py`
- Create: `server/tester_agent/tools/trace.py`
- Test: `server/tests/test_tool_editor.py`

**Interfaces:**
- Consumes: `WorkspaceSandbox`, `tool_max_output_chars`
- Produces:
  - `async def run_str_replace_editor(sandbox, *, command, path, file_text=None, old_str=None, new_str=None, insert_line=None, view_range=None, max_output_chars=16000) -> str`
  - `def make_str_replace_editor_tool(sandbox: WorkspaceSandbox, *, max_output_chars: int) -> BaseTool` (name=`str_replace_editor`)
  - `ToolTraceEntry` TypedDict: `tool`, `ok`, `latency_ms`, optional `error_code`, `args_digest`
- After successful write under `**/cases/**/*.md`, caller may trigger workspace reconcile later; editor itself only writes bytes.

- [ ] **Step 1: Write failing tests** for view (file line numbers), view dir (2 levels), create, unique str_replace, ambiguous str_replace (message contains `Multiple occurrences`), insert, snapshots write denied, truncation containing `<response clipped>`

```python
@pytest.mark.asyncio
async def test_str_replace_requires_unique_match(tmp_path: Path):
    sb = WorkspaceSandbox(tmp_path)
    (tmp_path / "f.txt").write_text("aa\nbb\naa\n", encoding="utf-8")
    with pytest.raises(Exception) as ei:
        await run_str_replace_editor(
            sb, command="str_replace", path="f.txt", old_str="aa", new_str="cc"
        )
    assert "Multiple occurrences" in str(ei.value)
```

- [ ] **Step 2: Run tests — expect FAIL**

Run: `cd server && python -m pytest tests/test_tool_editor.py -v`

- [ ] **Step 3: Implement editor**

Port harness behavior from `D:/code/typescript/deepseek-harness/packages/fs/tool-str-replace-editor/src/index.ts`: numbered view, directory listing, create-if-absent, unique `old_str`, insert after line, truncate with harness NOTE text. Wrap with LangChain `StructuredTool` matching schema enums `view|create|str_replace|insert`.

- [ ] **Step 4: Run tests — expect PASS**

- [ ] **Step 5: Commit**

```bash
git add server/tester_agent/tools/str_replace_editor.py server/tester_agent/tools/trace.py server/tests/test_tool_editor.py
git commit -m "$(cat <<'EOF'
feat(tools): add str_replace_editor aligned with harness

EOF
)"
```

---

### Task 3: Persistent bash tool

**Files:**
- Create: `server/tester_agent/tools/bash_persistent.py`
- Create: `server/tester_agent/tools/shell_backend.py`
- Test: `server/tests/test_tool_bash.py`
- Modify: `server/tester_agent/cli.py` (`cmd_check` adds `shell_backend`)

**Interfaces:**
- Consumes: `WorkspaceSandbox`, timeout/max_output from runtime
- Produces:
  - `detect_shell_backend(preference: str = "auto") -> tuple[str, list[str]]` → `("bash"|"pwsh", argv)`
  - `class PersistentBashManager` with `kind: str` and `async def run(self, owner_id: str, command: str, *, cwd: Path, timeout_ms: int, max_output_chars: int, cancel: asyncio.Event | None = None) -> str`
  - `make_bash_tool(manager, *, owner_id, sandbox, timeout_ms, max_output_chars) -> BaseTool` (name=`bash`)
  - Process-global sessions keyed by `owner_id`; serialize per owner; reset on timeout/death

- [ ] **Step 1: Write failing tests**

```python
@pytest.mark.asyncio
async def test_bash_persists_cwd(tmp_path: Path):
    backend = detect_shell_backend("auto")
    mgr = PersistentBashManager(backend=backend)
    (tmp_path / "sub").mkdir()
    await mgr.run("o1", "cd sub", cwd=tmp_path, timeout_ms=30_000, max_output_chars=16_000)
    cmd = "pwd" if mgr.kind == "bash" else "(Get-Location).Path"
    out2 = await mgr.run("o1", cmd, cwd=tmp_path, timeout_ms=30_000, max_output_chars=16_000)
    assert "sub" in out2.replace("\\", "/")
```

Also cover: empty command error; two owners do not share cwd; timeout path resets session (short timeout + sleep).

- [ ] **Step 2: Run — expect FAIL**

- [ ] **Step 3: Implement**

Long-lived subprocess (`bash --noprofile --norc` or `pwsh -NoLogo -NoProfile`); wrap commands with UUID start/end markers and exit code; poll until end marker or timeout; truncate like harness. `cli check` adds `shell_backend` field; if `auto` finds nothing, `ok=false`.

- [ ] **Step 4: Run — expect PASS**

- [ ] **Step 5: Commit**

```bash
git add server/tester_agent/tools/bash_persistent.py server/tester_agent/tools/shell_backend.py server/tester_agent/cli.py server/tests/test_tool_bash.py
git commit -m "$(cat <<'EOF'
feat(tools): add persistent bash tool with Windows fallback

EOF
)"
```

---

### Task 4: Tool registry

**Files:**
- Create: `server/tester_agent/tools/registry.py`
- Modify: `server/tester_agent/tools/__init__.py`
- Test: `server/tests/test_tool_registry.py`

**Interfaces:**
- Produces:

```python
@dataclass
class ToolBuildContext:
    owner_id: str
    workspace_root: Path
    runtime_config: dict
    cancel_event: asyncio.Event | None = None
    bash_manager: PersistentBashManager | None = None

def build_case_designer_tools(ctx: ToolBuildContext) -> list[BaseTool]:
    """Returns tools named exactly 'bash' and 'str_replace_editor'."""
```

- [ ] **Step 1: Failing test** — ` {t.name for t in tools} == {"bash", "str_replace_editor"}`; `ainvoke` view on a temp file succeeds

- [ ] **Step 2: Run — expect FAIL**

- [ ] **Step 3: Implement registry wiring timeouts from `runtime_config`

- [ ] **Step 4: Run — expect PASS**

- [ ] **Step 5: Commit**

```bash
git add server/tester_agent/tools/registry.py server/tester_agent/tools/__init__.py server/tests/test_tool_registry.py
git commit -m "$(cat <<'EOF'
feat(tools): register case_designer LangChain tools

EOF
)"
```

---

### Task 5: ChatOpenAI helper + tool_agent_graph

**Files:**
- Modify: `server/tester_agent/adapters/llm.py` (add `get_chat_model`)
- Create: `server/tester_agent/graph/tool_agent.py`
- Test: `server/tests/test_tool_agent_graph.py`

**Interfaces:**
- Consumes: `build_case_designer_tools`, model/runtime config dicts
- Produces:

```python
def get_chat_model(*, model_config: dict, runtime_config: dict) -> BaseChatModel: ...

@dataclass
class ToolAgentResult:
    final_text: str
    tool_trace: list[ToolTraceEntry]
    messages: list

async def run_tool_agent(
    *,
    history: list,
    system_prompt: str,
    tools: list[BaseTool],
    model: BaseChatModel,
    max_steps: int = 12,
) -> ToolAgentResult: ...
```

Graph: model (`bind_tools`) → conditional → `ToolNode` → model; stop when no tool_calls; enforce `max_steps` via recursion_limit/counter; fill `tool_trace` via wrappers or listeners.

- [ ] **Step 1: Failing test** with a sequenced fake model: first AIMessage with `str_replace_editor` tool_call, then final text; assert trace contains editor and final_text non-empty

- [ ] **Step 2: Run — expect FAIL**

- [ ] **Step 3: Implement `get_chat_model` from existing `model_config` fields (same base_url/api_key/model as `OpenAICompatLLMClient.from_configs`). Leave `LLMClient.chat` intact.

- [ ] **Step 4: Run — expect PASS**

- [ ] **Step 5: Commit**

```bash
git add server/tester_agent/adapters/llm.py server/tester_agent/graph/tool_agent.py server/tests/test_tool_agent_graph.py
git commit -m "$(cat <<'EOF'
feat(graph): add LangGraph tool_agent subgraph

EOF
)"
```

---

### Task 6: Conversation chat API wires tools

**Files:**
- Create: `server/tester_agent/runtime/chat_agent.py`
- Modify: `server/tester_agent/api/conversations.py`
- Test: `server/tests/test_api_chat_tools.py`

**Interfaces:**
- Consumes: `run_tool_agent`, `build_case_designer_tools`, `MessageDAO`, `ConfigDAO`, `Settings.workspaces_dir`
- `kind=chat`: persist user → conversation lock → LC history → tools with `owner_id=f"conversation:{id}"` → `run_tool_agent` → persist assistant with `payload.tool_trace`
- `kind=change_request`: no tool loop (existing persist-only)
- Response model:

```python
class SendMessageOut(BaseModel):
    user: MessageOut
    assistant: MessageOut | None = None  # set for kind=chat
```

- [ ] **Step 1: API test** with mocked `run_tool_agent`; assert assistant row + `tool_trace` in DB and response

- [ ] **Step 2: Run — expect FAIL**

- [ ] **Step 3: Implement chat_agent + wire `send_message`**

- [ ] **Step 4: Run — expect PASS**

- [ ] **Step 5: Commit**

```bash
git add server/tester_agent/runtime/chat_agent.py server/tester_agent/api/conversations.py server/tests/test_api_chat_tools.py
git commit -m "$(cat <<'EOF'
feat(api): run tool agent on conversation chat messages

EOF
)"
```

---

### Task 7: Pipeline opt-in tool gather

**Files:**
- Create: `server/tester_agent/graph/tool_gather.py`
- Modify: `server/tester_agent/graph/nodes/intake.py`, `link_identify.py`, `point_write.py`, `case_generate.py`
- Test: `server/tests/test_tool_gather.py`

**Interfaces:**

```python
async def maybe_gather_with_tools(
    *,
    stage: str,
    agent_config: dict,
    task_id: str,
    workspace_id: str,
    workspace_root: Path,
    runtime_config: dict,
    model_config: dict,
    user_goal: str,
    cancel_event: asyncio.Event | None = None,
    event_sink: Callable[[str, dict], Awaitable[None]] | None = None,
) -> str:
    """Return '' if stage not in enable_tools_stages; else tool_agent final_text."""
```

When tools run, `event_sink("tool_call", {...})` if provided (Runner/node EventBus pattern). Inject returned text into LLM user prompt as `## Tool gather notes`. Default empty stages → existing tests unchanged.

- [ ] **Step 1: Test** empty stages → `""` and no model call; `enable_tools_stages=["intake"]` → mock `run_tool_agent` once

- [ ] **Step 2: Run — expect FAIL**

- [ ] **Step 3: Implement gather + wire four nodes (coverage_check untouched)**

- [ ] **Step 4: Run gather tests + a smoke subset of intake/link tests**

- [ ] **Step 5: Commit**

```bash
git add server/tester_agent/graph/tool_gather.py server/tester_agent/graph/nodes/*.py server/tests/test_tool_gather.py
git commit -m "$(cat <<'EOF'
feat(graph): optional tool gather before structured stage LLM

EOF
)"
```

---

### Task 8: Frontend tool_trace + docs backfill

**Files:**
- Modify: `web/src/api/domain.ts`, `web/src/api/endpoints.ts`, chat components / `ChatPage.tsx`
- Create: `web/src/components/chat/ToolTraceSummary.tsx` (+ test)
- Modify: `docs/PRD.md`, `docs/tech-design.md`, `docs/detailed-design.md`, `README.md`
- Update spec status field when feature lands

**Behavior:** `sendMessage` handles `SendMessageOut` with optional `assistant`. UI shows collapsible “调用了 N 次工具” from `payload.tool_trace` (name + ok + latency only).

Docs: PRD in-scope bullet; tech-design tool-agent subsection; detailed-design tools package + API shape; README Windows shell note.

- [ ] **Step 1: Vitest failing for ToolTraceSummary**

- [ ] **Step 2: Run — expect FAIL**

- [ ] **Step 3: Implement UI + docs**

- [ ] **Step 4: `cd web && npm test` PASS for new tests**

- [ ] **Step 5: Commit**

```bash
git add web docs README.md
git commit -m "$(cat <<'EOF'
feat(web,docs): show chat tool_trace and document builtin tools

EOF
)"
```

- [ ] **Step 6: Full regression**

Run: `cd server && python -m pytest -q`  
Run: `cd web && npm test`  
Expected: green (note any pre-existing failures separately)

---

## Spec coverage checklist

| Spec section | Task(s) |
|---|---|
| §1 goals / LC-first | 4–7 |
| §2 architecture / owners | 3, 5, 6 |
| §3.1 bash | 3 |
| §3.2 editor | 2 |
| §4 LC tools / messages / subgraph | 4, 5, 6 |
| §4.5 get_chat_model | 5 |
| §5 security sandbox | 1–3 |
| §6 runtime_config / cli check / SSE | 1, 3, 7 |
| §7 tests | each task |
| §8 docs | 8 |
| §9 out of scope | respected |
| Pipeline opt-in | 7 |
| Chat tools | 6, 8 |
