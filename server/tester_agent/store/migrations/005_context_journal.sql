-- 005_context_journal.sql — 上下文淘汰 journal（仅元数据，spec §7 / plan §5.1）
CREATE TABLE IF NOT EXISTS context_journal (
  id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL,
  owner_type TEXT NOT NULL,
  owner_id TEXT NOT NULL,
  task_id TEXT,
  conversation_id TEXT,
  partition TEXT NOT NULL,
  entry_id TEXT NOT NULL,
  entry_kind TEXT NOT NULL,
  action TEXT NOT NULL,
  reason TEXT NOT NULL,
  policy_version TEXT NOT NULL,
  tokens_est INTEGER NOT NULL DEFAULT 0,
  digest TEXT NOT NULL DEFAULT '',
  refs TEXT NOT NULL DEFAULT '{}',
  scope_level TEXT NOT NULL DEFAULT 'task',
  phase TEXT NOT NULL DEFAULT 'shared',
  step_id TEXT,
  batch_id TEXT,
  item_key TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ctx_journal_owner
  ON context_journal(owner_type, owner_id, created_at, id);
CREATE INDEX IF NOT EXISTS idx_ctx_journal_ws
  ON context_journal(workspace_id, created_at, id);
CREATE INDEX IF NOT EXISTS idx_ctx_journal_entry
  ON context_journal(owner_type, owner_id, entry_id, created_at);
