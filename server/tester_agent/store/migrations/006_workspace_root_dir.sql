-- 006_workspace_root_dir.sql — 工作区可指定数据根目录（schema_version = 6）
-- 空字符串 = 默认 {data_dir}/workspaces/{id}/；非空 = 绝对路径。
ALTER TABLE workspace ADD COLUMN root_dir TEXT NOT NULL DEFAULT '';
