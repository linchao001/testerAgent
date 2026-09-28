# Embedded ReMe Memory Module Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Embed ReMe in-process for personal memory + shared KB; replace HTTP adapter with SDK adapters; wire chat `memory_search` while keeping KB write behind confirm.

**Architecture:** Per-workspace `ReMeMemoryManager` in `WorkspaceMemoryPool`; `SdkReMeReader`/`SdkReMeWriter` adapt `run_job`; delete `reme_http`; Factory keys by `workspace_id`; chat tools call manager only for search/auto_memory.

**Tech Stack:** Python 3.11+, FastAPI, existing `ReMeReader`/`ReMeWriter` Protocols, `reme-ai` (runtime; CI mocks), pytest.

**Spec:** [docs/superpowers/specs/2026-09-28-reme-memory-module-design.md](../specs/2026-09-28-reme-memory-module-design.md)

## Global Constraints

- Single uvicorn worker retained; ReMe same-process embed (not sidecar).
- Delete HTTP path (`reme_http.py`, `mode=service`); no fallback.
- L3/graph/chat must not import or call `ReMeWriter` / `save_to_knowledge` directly.
- Factory cache key = `workspace_id`; vault = `data/workspaces/{id}/reme/`.
- CI uses mock ReMe (`run_job` fake); real `reme-ai` optional integration only.
- TDD per task; commits when user allows (or after each green slice if session continues).
- Caps default SP-1: `(metadata_filter=False, entry_version=False, passage_api=True)`.

---

## File map

| Path | Responsibility |
|---|---|
| `server/tester_agent/memory/__init__.py` | Exports |
| `server/tester_agent/memory/reme_config.py` | `build_reme_app_config(...)` |
| `server/tester_agent/memory/manager.py` | `ReMeMemoryManager` lifecycle + `run_job` + lease |
| `server/tester_agent/memory/pool.py` | `WorkspaceMemoryPool` |
| `server/tester_agent/memory/prompts.py` | Memory guidance string |
| `server/tester_agent/memory/tools.py` | LangChain `memory_search` tool |
| `server/tester_agent/adapters/reme_sdk.py` | `SdkReMeReader` / `SdkReMeWriter` / register builder |
| `server/tester_agent/adapters/reme.py` | New kb_config validate; Factory by workspace_id |
| `server/tester_agent/adapters/reme_http.py` | **DELETE** |
| `server/tester_agent/main.py` | Pool + sdk register + shutdown |
| `server/tester_agent/runtime/context.py` | `memory_pool` on AppContext |
| `server/tester_agent/runtime/chat_agent.py` | Tools + guidance + auto_memory |
| `server/tester_agent/tools/registry.py` | Optional memory tools |
| `server/tester_agent/api/workspaces.py` | kb_config schema / probe via pool |
| `web/src/api/domain.ts` | KbConfig without service |
| `server/pyproject.toml` | `reme-ai` dependency |
| `server/tests/test_memory_*.py` | M1–M3 tests |
| Docs | handoff / tech-design / dd / README |

---

### Task 1: reme_config + FakeReMeApp + manager/pool (M1)

**Files:**
- Create: `memory/__init__.py`, `reme_config.py`, `manager.py`, `pool.py`
- Test: `tests/test_memory_pool.py`

**Interfaces:**
- `build_reme_app_config(*, workspace_dir, kb_config, model_config=None, language="zh") -> dict`
- `ReMeMemoryManager(workspace_id, vault_dir, kb_config, *, reme_factory=None, model_config=None)` with injectable fake ReMe
- `async start/close/run_job`; job lease
- `WorkspaceMemoryPool(data_root, *, manager_factory=None)`: `get_or_start`, `invalidate`, `shutdown_all`

- [x] **Step 1: Write failing tests** for config shape (kb_id → knowledge_base_id), pool get_or_start same instance, invalidate recreates, shutdown closes, run_job via fake.

- [x] **Step 2: Run** `pytest tests/test_memory_pool.py -v` → FAIL import

- [x] **Step 3: Implement** minimal config/manager/pool with injectable `FakeReMeApp` (start/close/run_job/is_started)

- [x] **Step 4: Run tests → green**

- [x] **Step 5: Commit** (if allowed): `feat(memory): add ReMe manager pool and config`

---

### Task 2: Sdk adapters + Factory workspace_id + delete HTTP (M2)

**Files:**
- Create: `adapters/reme_sdk.py`
- Modify: `adapters/reme.py` (`_validate_kb_config`, `for_workspace(workspace_id, kb_config)`, probe)
- Delete: `reme_http.py`, `tests/test_reme_http.py`
- Modify: all call sites of `for_workspace(kb_config)` → pass `workspace_id`
- Modify: `main.py`, workspaces kb/test, kb writer
- Test: `tests/test_reme_sdk.py`; update `test_reme.py`, API tests KB_CONFIG

**KB_CONFIG fixture shape:**
```python
{"kb_id": "kb-1", "options": {}}
```

- [x] **Step 1: Failing tests** SdkReader search/get/tree via fake manager; Writer save_to_knowledge; Factory rejects mode=service; register_sdk_builder

- [x] **Step 2: Implement sdk + factory + delete HTTP + wire main**

- [x] **Step 3: Fix callers / fixtures; run** `pytest tests/test_reme.py tests/test_reme_sdk.py tests/test_api_d.py tests/test_api_a.py -q`

- [x] **Step 4: Confirm import-linter scene 12 still green**

- [x] **Step 5: Commit:** `feat(reme): embed SDK adapters and remove HTTP`

---

### Task 3: Chat memory_search + guidance + auto_memory (M3)

**Files:**
- Create: `memory/prompts.py`, `memory/tools.py`
- Modify: `tools/registry.py`, `runtime/chat_agent.py`
- Test: `tests/test_memory_tools.py`, extend chat tests if present

- [x] **Step 1: Failing tests** tool registration when enabled; memory_search calls search job; no save_to_knowledge tool; auto_memory fires when interval>0

- [x] **Step 2: Implement tools + chat wiring**

- [x] **Step 3: Green tests**

- [x] **Step 4: Commit:** `feat(memory): wire chat memory_search and auto_memory`

---

### Task 4: Dependency + docs backfill (M4)

**Files:**
- Modify: `server/pyproject.toml` (`reme-ai>=0.4.1.5,<0.5`)
- Modify: `web/src/api/domain.ts`, mocks
- Modify: `docs/plan/handoff.md`, brief notes in tech-design/README
- Update spec status to implemented-in-progress

- [x] **Step 1: Update KbConfig types + mocks**

- [x] **Step 2: Docs + handoff M1–M4 record**

- [x] **Step 3: Full** `pytest tests/ -q` (or targeted if full too slow)

- [x] **Step 4: Commit:** `docs: backfill embedded ReMe memory module`

---

## Execution note

User requested immediate implementation after design approval. Execute Tasks 1→4 in order with TDD; do not start Task N+1 until Task N tests are green.
