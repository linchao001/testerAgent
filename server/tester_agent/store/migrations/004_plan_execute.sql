-- 004_plan_execute.sql — AgentPlan 指针、artifact.kind、subtask 表
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
