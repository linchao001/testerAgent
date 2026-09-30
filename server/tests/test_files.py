"""WP-05 验收：FileStore（dd §4 §5）。

覆盖 WBS 验收口径"提交崩溃文件侧行为；hash 对拍；单行偏移读"：
- slug/路径形态、MD 渲染↔解析（两种预期写法/缺段宽容/畸形 front-matter 拒绝）；
- content_hash 规范化（与 FM 中 hash 值无关、CRLF 等价、末尾空行不敏感、篡改必变）；
- requirement LF 落盘/字节偏移条款读取；write_case 原子落位/幂等重放不改 mtime；
- overwrite_case 乐观锁 FILE_CONFLICT；外部篡改 hash 对拍；
- SnapshotWriter 偏移连续/单行 JSON 精确读；
- rename 失败崩溃现场：目标缺失、tmp 留存、soft_cleanup 回收；
- list_case_files 版本分目录；路径越界（坏段/../ 绝对拼接）一律 PathEscapeError。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tester_agent.domain import (
    CaseFileContent,
    CaseStep,
    TraceRefs,
)
from tester_agent.errors import FileConflict, NotFoundError
from tester_agent.store import workspace_files as wf
from tester_agent.store.workspace_files import (
    CleanupReport,
    FileStore,
    PathEscapeError,
    PathInfo,
    WrittenCase,
    canonical_case_text,
    case_rel_path,
    hash_case_text,
    parse_case_markdown,
    render_case_markdown,
    slugify,
)

WS, TASK = "ws1", "t1"


# ---------- 夹具与工厂 ----------


@pytest.fixture()
def store(tmp_path) -> FileStore:
    return FileStore(tmp_path / "data")


def _case(
    *,
    case_id: str = "9f3a1c2b7e4dabcdef0123456789abcd",
    point_id: str = "pt-1-3",
    version: int = 2,
    title: str = "退款超时后状态回滚校验",
    preconditions: list[str] | None = None,
    steps: list[CaseStep] | None = None,
    test_data: str | None = "订单金额 0.01 元（沙箱）",
    priority: str = "P1",
) -> CaseFileContent:
    return CaseFileContent(
        case_id=case_id,
        point_id=point_id,
        stage_version=version,
        title=title,
        priority=priority,  # type: ignore[arg-type]
        preconditions=["条件甲", "条件乙"] if preconditions is None else preconditions,
        steps=[
            CaseStep(seq=1, action="用户发起退款，mock 超时", expect="提示处理中，状态不变"),
            CaseStep(seq=2, action="通道回调失败", expect="状态回滚，退款单标记失败"),
        ]
        if steps is None
        else steps,
        test_data=test_data,
        trace_refs=TraceRefs(
            clause_ids=["h2-1-h3-2"], entry_ids=["ent_8812"], point_ids=[point_id]
        ),
    )


def _render_with_hash(content: CaseFileContent) -> tuple[str, str]:
    """模拟 write_case 的两遍渲染，返回 (final_md, hash)。"""
    placeholder = render_case_markdown(content, content_hash="sha256:pending")
    digest = hash_case_text(placeholder)
    return render_case_markdown(content, content_hash=digest), digest


# ---------- slug / 路径形态 ----------


class TestSlugAndPath:
    def test_slug_nfkc_lowercase_alnum_kept(self):
        # NFKC 全角→半角、大写转小写、中文保留
        assert slugify("ＡＢＣ Refund Flow") == "abc-refund-flow"
        assert slugify("退款超时校验") == "退款超时校验"

    def test_slug_symbols_to_hyphen_collapsed_and_trimmed(self):
        assert slugify("a!!!b   c///d") == "a-b-c-d"
        assert slugify("--- leading") == "leading"

    def test_slug_truncate_40(self):
        s = slugify("x" * 100)
        assert len(s) == 40
        long_title = "退" * 100
        assert len(slugify(long_title)) == 40

    def test_slug_empty_fallback(self):
        assert slugify("!!!") == "case"
        assert slugify("") == "case"

    def test_case_rel_path_shape(self):
        rel = case_rel_path("abcdef1234567890", "pt-9-9", "标题 X", 3)
        assert rel == "cases/v3/abcdef12--pt-9-9--标题-x.md"


# ---------- MD 渲染 / 解析 / hash ----------


class TestCaseMarkdown:
    def test_render_parse_roundtrip(self):
        content = _case()
        md, digest = _render_with_hash(content)
        parsed = parse_case_markdown(md)
        assert parsed.case_id == content.case_id
        assert parsed.point_id == content.point_id
        assert parsed.stage_version == content.stage_version
        assert parsed.title == content.title
        assert parsed.priority == "P1"
        assert parsed.preconditions == content.preconditions
        assert [s.model_dump() for s in parsed.steps] == [
            s.model_dump() for s in content.steps
        ]
        assert parsed.test_data == content.test_data
        assert parsed.trace_refs == content.trace_refs
        # 文件中的 FM hash 与算法一致
        assert f"content_hash: {digest}" in md
        assert hash_case_text(md) == digest

    def test_render_omits_optional_sections(self):
        content = _case(preconditions=[], test_data=None)
        md, _ = _render_with_hash(content)
        assert "## 前置条件" not in md
        assert "## 测试数据" not in md
        parsed = parse_case_markdown(md)
        assert parsed.preconditions == []
        assert parsed.test_data is None
        assert len(parsed.steps) == 2

    def test_parse_accepts_inline_soft_break_expect(self):
        """dd §4.2 规则 2 形态二：预期作为步骤段落的缩进续行（软换行）。"""
        md = (
            "---\ncase_id: c1\npoint_id: pt-1\nstage_version: 1\n"
            "priority: P1\ntrace_refs: {}\n---\n\n"
            "# 标题\n\n## 步骤\n"
            "1. 第一步动作  \n"
            "   预期：第一个预期\n"
            "2. 第二步动作\n"
            "   - 预期：第二个预期\n"
        )
        parsed = parse_case_markdown(md)
        assert [s.action for s in parsed.steps] == ["第一步动作", "第二步动作"]
        assert [s.expect for s in parsed.steps] == ["第一个预期", "第二个预期"]

    def test_parse_lenient_missing_sections(self):
        md = (
            "---\ncase_id: c1\npoint_id: p\nstage_version: 1\n"
            "priority: P2\ntrace_refs: {}\n---\n\n# 只有标题\n"
        )
        parsed = parse_case_markdown(md)
        assert parsed.steps == []
        assert parsed.preconditions == []
        assert parsed.priority == "P2"

    @pytest.mark.parametrize(
        "bad",
        [
            "没有 front matter\n# 标题\n",
            "---\nnot: yaml: : :\n---\n\n# x\n",
            "---\npoint_id: p\n---\n\n# 缺 case_id\n",
            "---\ncase_id: c1\n---\n\n没有标题，只有段落\n",
        ],
    )
    def test_parse_rejects_malformed(self, bad):
        with pytest.raises(ValueError):
            parse_case_markdown(bad)

    def test_hash_independent_of_fm_hash_value(self):
        content = _case()
        md_a = render_case_markdown(content, content_hash="sha256:aaa")
        md_b = render_case_markdown(content, content_hash="sha256:bbb")
        # FM hash 值不参与自身计算
        assert canonical_case_text(md_a) == canonical_case_text(md_b)
        assert hash_case_text(md_a) == hash_case_text(md_b)

    def test_hash_normalizes_crlf_and_trailing_blanks(self):
        content = _case()
        md_lf, digest = _render_with_hash(content)
        assert hash_case_text(md_lf.replace("\n", "\r\n")) == digest
        assert hash_case_text(md_lf + "\n\n\n") == digest
        # 正文一个字的变化必然改 hash（对拍基础）
        changed = md_lf.replace("0.01", "0.02")
        assert hash_case_text(changed) != digest


# ---------- requirement ----------


class TestRequirementFiles:
    async def test_save_ref_shape_and_lf_normalization(self, store):
        ref = await store.save_requirement(WS, TASK, "第一行\n第二行\r\n第三行\r")
        assert ref.path == "workspaces/ws1/t1/requirement.md"
        assert ref.content_hash.startswith("sha256:")
        assert ref.size_bytes and ref.size_bytes > 0
        text = await store.read_requirement(WS, TASK)
        assert "\r" not in text
        assert text == "第一行\n第二行\n第三行\n"
        # 物理文件在期望布局下
        assert (store.root / ref.path).is_file()

    async def test_read_clause_by_byte_span(self, store):
        await store.save_requirement(WS, TASK, "第一条\n第二条\n第三行")
        # UTF-8 每汉字 3 字节：第二条 = bytes[10:19)
        assert await store.read_clause(WS, TASK, (10, 19)) == "第二条"
        assert await store.read_clause(WS, TASK, (9, 10)) == "\n"

    async def test_read_clause_rejects_bad_span(self, store):
        await store.save_requirement(WS, TASK, "abc")
        with pytest.raises(PathEscapeError):
            await store.read_clause(WS, TASK, (-1, 2))
        with pytest.raises(PathEscapeError):
            await store.read_clause(WS, TASK, (2, 1))

    async def test_missing_requirement_raises(self, store):
        with pytest.raises(NotFoundError):
            await store.read_requirement(WS, "ghost")
        with pytest.raises(NotFoundError):
            await store.read_clause(WS, "ghost", (0, 1))

    async def test_bad_segments_rejected(self, store):
        for bad_ws, bad_task in [("../etc", TASK), (WS, "a/b"), ("", TASK)]:
            with pytest.raises(PathEscapeError):
                await store.save_requirement(bad_ws, bad_task, "x")


# ---------- write_case / 幂等 / 乐观锁 / 对拍 ----------


class TestCaseFiles:
    async def test_write_atomic_and_refs(self, store):
        content = _case()
        written = await store.write_case(WS, TASK, 2, content)
        assert isinstance(written, WrittenCase)
        expected_rel = case_rel_path(
            content.case_id, content.point_id, content.title, 2
        )
        assert written.file_path == expected_rel
        target = store.root / "workspaces" / WS / TASK / expected_rel
        assert target.is_file()
        raw = target.read_text(encoding="utf-8")
        assert f"content_hash: {written.content_hash}" in raw
        # read_case/hash_of 与写入 hash 三方一致
        assert await store.read_case(WS, TASK, expected_rel) == raw
        assert await store.hash_of(WS, TASK, expected_rel) == written.content_hash
        # 同目录无残留 tmp
        assert not list(target.parent.glob("*.tmp.*"))

    async def test_idempotent_replay_does_not_rewrite(self, store):
        content = _case()
        first = await store.write_case(WS, TASK, 2, content)
        target = store.root / "workspaces" / WS / TASK / first.file_path
        os.utime(target, (1_000_000_000, 1_000_000_000))
        before = target.stat().st_mtime_ns
        time.sleep(0.02)

        replay = await store.write_case(WS, TASK, 2, content)
        assert replay == first
        assert target.stat().st_mtime_ns == before  # 同 hash 不重写（崩溃点 2/3 安全）

    async def test_read_missing_and_escape(self, store):
        with pytest.raises(NotFoundError):
            await store.read_case(WS, TASK, "cases/v2/nope.md")
        with pytest.raises(PathEscapeError):
            await store.read_case(WS, TASK, "../../../../etc/hosts")
        with pytest.raises(PathEscapeError):
            await store.read_case(WS, TASK, "/etc/hosts")

    async def test_overwrite_with_expected_hash(self, store):
        written = await store.write_case(WS, TASK, 2, _case())
        raw = await store.read_case(WS, TASK, written.file_path)
        edited = raw.replace("0.01", "0.02")

        result = await store.overwrite_case(
            WS, TASK, written.file_path, edited, written.content_hash
        )
        assert result.file_path == written.file_path
        assert result.content_hash != written.content_hash
        # 新文件自证 hash 一致；hash_of 对拍
        assert await store.hash_of(WS, TASK, written.file_path) == result.content_hash
        saved = await store.read_case(WS, TASK, written.file_path)
        assert f"content_hash: {result.content_hash}" in saved
        # 不带前缀的 hex 形态 expected_hash 同样接受
        raw2 = saved.replace("0.02", "0.03")
        hex_hash = result.content_hash.removeprefix("sha256:")
        result2 = await store.overwrite_case(
            WS, TASK, written.file_path, raw2, hex_hash
        )
        assert await store.hash_of(WS, TASK, written.file_path) == result2.content_hash
        # 连续第三次覆盖：防 FM/正文边界换行逐次丢失回归
        saved2 = await store.read_case(WS, TASK, written.file_path)
        result3 = await store.overwrite_case(
            WS, TASK, written.file_path, saved2.replace("0.03", "0.04"),
            result2.content_hash,
        )
        assert await store.hash_of(WS, TASK, written.file_path) == result3.content_hash
        assert "0.04" in await store.read_case(WS, TASK, written.file_path)

    async def test_overwrite_conflict_returns_current_hash(self, store):
        written = await store.write_case(WS, TASK, 2, _case())
        raw = await store.read_case(WS, TASK, written.file_path)
        # 模拟外部改动：盘上 hash 已不是编辑基准
        await store.overwrite_case(
            WS, TASK, written.file_path, raw.replace("0.01", "9.99"), None
        )
        with pytest.raises(FileConflict) as exc_info:
            await store.overwrite_case(
                WS, TASK, written.file_path, raw, written.content_hash
            )
        assert exc_info.value.code == "FILE_CONFLICT"
        assert exc_info.value.http_status == 409
        assert exc_info.value.details["expected"] == written.content_hash
        assert exc_info.value.details["current"] != written.content_hash

    async def test_overwrite_missing_file_and_bad_md(self, store):
        with pytest.raises(NotFoundError):
            await store.overwrite_case(WS, TASK, "cases/v2/x.md", "# 无 FM", None)
        written = await store.write_case(WS, TASK, 2, _case())
        with pytest.raises(ValueError):
            await store.overwrite_case(
                WS, TASK, written.file_path, "# 没有 front matter", None
            )

    async def test_external_tamper_detected_by_hash(self, store):
        """dd §11.3：外部改文件 → hash_of 与 DB hash 不符（hash_conflict 对账基础）。"""
        written = await store.write_case(WS, TASK, 2, _case())
        target = store.root / "workspaces" / WS / TASK / written.file_path
        # 外部直接在文件尾部加内容
        with target.open("a", encoding="utf-8") as fp:
            fp.write("\n被人手工加了一行\n")
        assert await store.hash_of(WS, TASK, written.file_path) != written.content_hash

        # 外部删文件 → read/hash NotFound（file_missing 对账基础）
        target.unlink()
        with pytest.raises(NotFoundError):
            await store.hash_of(WS, TASK, written.file_path)

    async def test_list_case_files_grouped_by_version_sorted(self, store):
        c1 = _case(case_id="a1111111111111111111111111111111x", title="用例甲")
        c2 = _case(case_id="a2222222222222222222222222222222x", title="用例乙")
        c3 = _case(case_id="a3333333333333333333333333333333x", title="用例丙", version=3)
        w1 = await store.write_case(WS, TASK, 2, c1)
        w2 = await store.write_case(WS, TASK, 2, c2)
        w3 = await store.write_case(WS, TASK, 3, c3)

        files = await store.list_case_files(WS, TASK)
        rels = [f.rel_path for f in files]
        assert rels == sorted(rels)
        assert set(rels) == {w1.file_path, w2.file_path, w3.file_path}
        assert all(isinstance(f, PathInfo) and f.size_bytes > 0 for f in files)
        # 无任务目录时返回空
        assert await store.list_case_files(WS, "no-task") == []
        # 快照/tmp 不被当作用例
        sw = store.open_snapshot_writer(WS, TASK, "point_write", 1, "point_write", "b0")
        sw.append({"x": 1})
        sw.close()
        assert {f.rel_path for f in await store.list_case_files(WS, TASK)} == {
            w1.file_path, w2.file_path, w3.file_path
        }


# ---------- SnapshotWriter ----------


class TestSnapshotWriter:
    async def test_append_offsets_and_single_line_read(self, store):
        writer = store.open_snapshot_writer(
            WS, TASK, "point_write", 1, "point_write", "b0"
        )
        try:
            assert len(writer.snapshot_id) == 32
            assert writer.rel_path == (
                f"snapshots/point_write/v1/b0-point_write-{writer.snapshot_id[:8]}.jsonl"
            )
            o1, l1 = writer.append({"entry_id": "e1", "title": "中文条目", "position": 0})
            o2, l2 = writer.append({"entry_id": "e2", "title": "two", "position": 1})
        finally:
            writer.close()

        # 偏移连续：第二行紧接第一行 + 换行符
        assert o2 == o1 + l1 + 1
        line1 = await store.read_snapshot_line(WS, TASK, writer.rel_path, o1, l1)
        line2 = await store.read_snapshot_line(WS, TASK, writer.rel_path, o2, l2)
        assert json.loads(line1)["title"] == "中文条目"
        assert json.loads(line2)["entry_id"] == "e2"
        # 长度按字节：中文行 UTF-8 字节数 > 字符数
        assert l1 == len(
            (json.dumps({"entry_id": "e1", "title": "中文条目", "position": 0},
                        ensure_ascii=False)).encode("utf-8")
        )
        path = store.root / "workspaces" / WS / TASK / writer.rel_path
        # 恰好两行，逐行读回与文件物理行一致
        physical = path.read_bytes().decode("utf-8").splitlines()
        assert physical == [line1, line2]

    async def test_batch_none_uses_na_prefix(self, store):
        writer = store.open_snapshot_writer(
            WS, TASK, "link_identify", 3, "link_identify", None
        )
        try:
            assert writer.rel_path.startswith("snapshots/link_identify/v3/na-link_identify-")
        finally:
            writer.close()

    async def test_read_line_missing_escape_bad_offset(self, store):
        with pytest.raises(NotFoundError):
            await store.read_snapshot_line(WS, TASK, "snapshots/x/v1/na-n-12345678.jsonl", 0, 1)
        writer = store.open_snapshot_writer(WS, TASK, "point_write", 1, "node", "b0")
        writer.append({"k": "v"})
        writer.close()
        with pytest.raises(PathEscapeError):
            await store.read_snapshot_line(
                WS, TASK, "../../../../app.db", 0, 1
            )
        with pytest.raises(PathEscapeError):
            await store.read_snapshot_line(
                WS, TASK, writer.rel_path, -1, 1
            )

    async def test_context_manager_closes(self, store):
        with store.open_snapshot_writer(WS, TASK, "point_write", 1, "node", "b1") as w:
            w.append({"closed": True})
        assert w._fp.closed


# ---------- 崩溃现场与清理 ----------


class TestAtomicCrashAndCleanup:
    async def test_rename_failure_leaves_no_target_but_tmp_remains(
        self, store, monkeypatch
    ):
        content = _case()
        rel = case_rel_path(content.case_id, content.point_id, content.title, 2)
        target = store.root / "workspaces" / WS / TASK / rel
        tmp_dir = target.parent

        real_replace = os.replace

        def boom(src, dst):
            raise OSError("simulated crash at rename")

        monkeypatch.setattr(wf.os, "replace", boom)
        with pytest.raises(OSError, match="simulated crash"):
            await store.write_case(WS, TASK, 2, content)
        monkeypatch.setattr(wf.os, "replace", real_replace)

        # 崩溃点 1：目标文件不存在；同目录留下 .tmp.{uuid}
        assert not target.exists()
        leftovers = list(tmp_dir.glob("*.tmp.*"))
        assert len(leftovers) == 1

        # 恢复后重提：成功落位（tmp 不影响）
        written = await store.write_case(WS, TASK, 2, content)
        assert target.is_file()
        assert await store.hash_of(WS, TASK, written.file_path) == written.content_hash

        # 保留期内（7 天）不清理；retention_days=0 清理崩溃遗留
        report_fresh = await store.soft_cleanup(7)
        assert report_fresh == CleanupReport(0, 0)
        assert leftovers[0].exists()
        leftover_size = leftovers[0].stat().st_size
        report = await store.soft_cleanup(0)
        assert report.tmp_files_removed == 1
        assert report.bytes_freed == leftover_size
        assert not leftovers[0].exists()
        # 再跑幂等
        assert await store.soft_cleanup(0) == CleanupReport(0, 0)

    async def test_cleanup_ignores_fresh_tmp_and_non_tmp(self, store):
        # 新 tmp（mtime 当前）在保留期内不删；正式用例永不被 cleanup 触碰
        written = await store.write_case(WS, TASK, 2, _case())
        target = store.root / "workspaces" / WS / TASK / written.file_path
        fresh_tmp = target.parent / ".case.md.tmp.freshuuid"
        fresh_tmp.write_text("x", encoding="utf-8")
        report = await store.soft_cleanup(7)
        assert report == CleanupReport(0, 0)
        assert fresh_tmp.exists() and target.exists()
        # 拨旧后清理（留足余量，避免 mtime≈cutoff 边界）
        old_ts = time.time() - 10 * 86400
        os.utime(fresh_tmp, (old_ts, old_ts))
        report = await store.soft_cleanup(8)
        assert report.tmp_files_removed == 1
        assert target.exists()

    async def test_cleanup_without_workspaces_dir(self, tmp_path):
        empty_store = FileStore(tmp_path / "does-not-exist")
        report = await empty_store.soft_cleanup(0)
        assert report == CleanupReport(0, 0)
