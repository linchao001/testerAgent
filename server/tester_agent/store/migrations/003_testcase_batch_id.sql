-- 003_testcase_batch_id.sql — testcase 增加 batch_id（dd §11.1 非确定性防护维度）
-- 背景：sweep_stale_idem 需按 (task_id, stage_version, batch_id) 定位上一轮
-- 失败重做后遗留的孤儿行并置 obsolete；case_id 虽由 (task|version|batch_id|point|seq)
-- 确定性派生，但 uuid5 不可逆，故需显式 batch_id 列做范围清理。
-- 单列 ALTER 属 SQLite 支持范围；NOT NULL 新列带默认 ''，应用侧新行始终显式写。
ALTER TABLE testcase ADD COLUMN batch_id TEXT NOT NULL DEFAULT '';
CREATE INDEX IF NOT EXISTS idx_case_batch ON testcase(task_id, stage_version, batch_id);
