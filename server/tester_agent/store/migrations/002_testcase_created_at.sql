-- 002_testcase_created_at.sql — testcase 增加 created_at（dd §3.1，schema_version = 2）
-- 背景：用例列表游标时间列原借 updated_at，编辑/评审会改变翻页位置。
-- 单列 ALTER 属 SQLite 支持范围（dd §3.2）；NOT NULL 新列必须带非空默认值，
-- 故先以 '' 落列，再把存量行回填为 updated_at；应用侧新行始终显式写真实时间。
ALTER TABLE testcase ADD COLUMN created_at TEXT NOT NULL DEFAULT '';
UPDATE testcase SET created_at = updated_at WHERE created_at = '';
