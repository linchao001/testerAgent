-- 001_init.sql — TesterAgent 首版 schema（dd §3.1，schema_version = 1）
-- 迁移文件只增不改；改表请新增 002_xxx.sql（dd §3.2）。
-- 下列 PRAGMA 由连接层统一施加（journal_mode=WAL 不能在事务内切换），
-- 迁移执行器会跳过这三条 PRAGMA，保留于此仅作事实声明。
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
PRAGMA busy_timeout=5000;

CREATE TABLE IF NOT EXISTS schema_meta (
  schema_version INTEGER PRIMARY KEY,
  applied_at     TEXT NOT NULL
);

-- ---------- workspace ----------
CREATE TABLE workspace (
  id          TEXT PRIMARY KEY,
  name        TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  kb_config   TEXT NOT NULL DEFAULT '{}',   -- JSON: {mode:'sdk'|'service', target, kb_id, options}
  created_at  TEXT NOT NULL,
  deleted_at  TEXT
);

-- ---------- agent ----------
CREATE TABLE agent (
  id         TEXT PRIMARY KEY,
  name       TEXT NOT NULL,
  agent_type TEXT NOT NULL,                 -- 'case_designer'
  config     TEXT NOT NULL DEFAULT '{}',    -- JSON: strategies/prompts/snapshot_level
  builtin    INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);

CREATE TABLE agent_workspace (
  agent_id     TEXT NOT NULL REFERENCES agent(id) ON DELETE CASCADE,
  workspace_id TEXT NOT NULL REFERENCES workspace(id) ON DELETE CASCADE,
  PRIMARY KEY (agent_id, workspace_id)
);

-- ---------- conversation / message ----------
CREATE TABLE conversation (
  id           TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL REFERENCES workspace(id),
  title        TEXT NOT NULL DEFAULT '',
  created_at   TEXT NOT NULL,
  updated_at   TEXT NOT NULL
);

CREATE TABLE message (
  id              TEXT PRIMARY KEY,
  conversation_id TEXT NOT NULL REFERENCES conversation(id),
  task_id         TEXT,                       -- 任务前自由对话可空
  role            TEXT NOT NULL,              -- user/assistant/system
  kind            TEXT NOT NULL,              -- 见 MessageKind
  content         TEXT NOT NULL,
  ref_artifact_id TEXT,
  payload         TEXT NOT NULL DEFAULT '{}',
  created_at      TEXT NOT NULL
);
CREATE INDEX idx_message_conv ON message(conversation_id, created_at);
CREATE INDEX idx_message_task ON message(task_id);

-- ---------- task ----------
CREATE TABLE task (
  id                  TEXT PRIMARY KEY,
  conversation_id     TEXT NOT NULL REFERENCES conversation(id),
  workspace_id        TEXT NOT NULL REFERENCES workspace(id),
  status              TEXT NOT NULL,
  current_stage       TEXT NOT NULL,
  requirement_ref     TEXT NOT NULL DEFAULT '{}',  -- {path,content_hash,clause_count}
  clauses             TEXT NOT NULL DEFAULT '[]',  -- list[ClauseRef]
  langgraph_thread_id TEXT NOT NULL,
  graph_run_id        TEXT NOT NULL,
  runner_heartbeat    TEXT,
  cancel_requested    INTEGER NOT NULL DEFAULT 0,
  snapshot_level      TEXT NOT NULL DEFAULT 'meta',
  error_info          TEXT,                        -- {code,message,retryable,node,details}
  created_at          TEXT NOT NULL,
  updated_at          TEXT NOT NULL
);
CREATE INDEX idx_task_ws_status ON task(workspace_id, status);
CREATE INDEX idx_task_conv ON task(conversation_id);
CREATE INDEX idx_task_heartbeat ON task(runner_heartbeat);

-- ---------- stage_artifact ----------
CREATE TABLE stage_artifact (
  id            TEXT PRIMARY KEY,
  task_id       TEXT NOT NULL REFERENCES task(id),
  stage         TEXT NOT NULL,
  graph_run_id  TEXT NOT NULL,
  stage_version INTEGER NOT NULL,
  origin        TEXT NOT NULL DEFAULT 'system',
  status        TEXT NOT NULL DEFAULT 'active',
  payload       TEXT NOT NULL DEFAULT '{}',
  progress      TEXT,                              -- list[BatchResult]（仅批处理节点）
  confirmed_by  TEXT,                              -- user / null(auto)
  created_at    TEXT NOT NULL,
  UNIQUE (task_id, stage, stage_version)
);
CREATE INDEX idx_artifact_active ON stage_artifact(task_id, stage, status);

-- ---------- testcase ----------
CREATE TABLE testcase (
  id            TEXT PRIMARY KEY,
  task_id       TEXT NOT NULL REFERENCES task(id),
  point_id      TEXT NOT NULL,
  stage_version INTEGER NOT NULL,
  lineage       TEXT NOT NULL DEFAULT '{}',
  status        TEXT NOT NULL DEFAULT 'active',
  review_status TEXT NOT NULL DEFAULT 'pending',
  file_path     TEXT NOT NULL,
  content_hash  TEXT NOT NULL,
  title         TEXT NOT NULL,
  trace_refs    TEXT NOT NULL DEFAULT '{}',
  error_info    TEXT,                              -- file_missing / hash_conflict 等对账标记
  updated_at    TEXT NOT NULL
);
CREATE INDEX idx_case_task_review ON testcase(task_id, status, review_status);
CREATE INDEX idx_case_point ON testcase(point_id);

-- ---------- retrieval_trace ----------
CREATE TABLE retrieval_trace (
  id            TEXT PRIMARY KEY,
  task_id       TEXT NOT NULL REFERENCES task(id),
  graph_run_id  TEXT NOT NULL,
  stage         TEXT NOT NULL,
  stage_version INTEGER NOT NULL,
  node          TEXT NOT NULL,
  batch_id      TEXT,
  query_variant TEXT NOT NULL DEFAULT '{}',        -- QueryVariant JSON
  candidates    TEXT NOT NULL DEFAULT '[]',        -- list[Candidate]
  injected_ids  TEXT NOT NULL DEFAULT '[]',
  referenced_ids TEXT NOT NULL DEFAULT '[]',   -- 白名单内实际引用
  hallucinated_ids TEXT NOT NULL DEFAULT '[]', -- 引用了白名单外 ID
  weak_ref_ids  TEXT NOT NULL DEFAULT '[]',    -- 疑似贴标签（Jaccard 低）
  degraded      TEXT NOT NULL DEFAULT '[]',
  created_at    TEXT NOT NULL
);
CREATE INDEX idx_trace_task ON retrieval_trace(task_id, stage, stage_version);

-- ---------- context_snapshot ----------
CREATE TABLE context_snapshot (
  id                  TEXT PRIMARY KEY,
  task_id             TEXT NOT NULL REFERENCES task(id),
  graph_run_id        TEXT NOT NULL,
  stage               TEXT NOT NULL,
  stage_version       INTEGER NOT NULL,
  node                TEXT NOT NULL,
  batch_id            TEXT,
  items               TEXT NOT NULL DEFAULT '[]',  -- list[InjectedItem 元数据]
  snapshot_path       TEXT,
  total_tokens_est    INTEGER NOT NULL DEFAULT 0,
  budget              INTEGER,
  truncated           INTEGER NOT NULL DEFAULT 0,
  prompt_template_ver TEXT NOT NULL,
  model_ref           TEXT NOT NULL DEFAULT '{}',
  usage               TEXT NOT NULL DEFAULT '{}',  -- {prompt_tokens,completion_tokens,total,aux:{...}}
  latencies           TEXT NOT NULL DEFAULT '{}',
  created_at          TEXT NOT NULL
);
CREATE INDEX idx_snapshot_task ON context_snapshot(task_id, stage, stage_version);

-- ---------- task_event ----------
CREATE TABLE task_event (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id    TEXT NOT NULL REFERENCES task(id),
  type       TEXT NOT NULL,
  payload    TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL
);
CREATE INDEX idx_event_task ON task_event(task_id, id);

-- ---------- review_record ----------
CREATE TABLE review_record (
  id          TEXT PRIMARY KEY,
  task_id     TEXT NOT NULL REFERENCES task(id),
  testcase_id TEXT NOT NULL REFERENCES testcase(id),
  action      TEXT NOT NULL,
  detail      TEXT NOT NULL DEFAULT '{}',
  created_at  TEXT NOT NULL
);
CREATE INDEX idx_review_task ON review_record(task_id, created_at);

-- ---------- kb_proposal ----------
CREATE TABLE kb_proposal (
  id                 TEXT PRIMARY KEY,
  workspace_id       TEXT NOT NULL REFERENCES workspace(id),
  task_id            TEXT REFERENCES task(id),
  payload            TEXT NOT NULL DEFAULT '{}',
  status             TEXT NOT NULL DEFAULT 'pending',
  confirm_token_hash TEXT,
  idempotency_key    TEXT,
  fail_count         INTEGER NOT NULL DEFAULT 0,  -- ReMe 写失败次数（§18.4）
  confirmed_at       TEXT,
  expires_at         TEXT NOT NULL,
  write_result       TEXT,                          -- ReMeWriter 回查结果
  created_at         TEXT NOT NULL,
  UNIQUE (idempotency_key)
);
CREATE INDEX idx_proposal_ws ON kb_proposal(workspace_id, status);

-- ---------- config（单行 id=1） ----------
CREATE TABLE config (
  id             INTEGER PRIMARY KEY CHECK (id = 1),
  model_config   TEXT NOT NULL DEFAULT '{}',  -- {base_url,api_key,model,temperature,top_p,timeout}
  runtime_config TEXT NOT NULL DEFAULT '{}'
);
INSERT OR IGNORE INTO config (id, model_config, runtime_config) VALUES (1, '{}', '{}');
