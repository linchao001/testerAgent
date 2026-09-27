"""WP-08 验收：caps 三态降级分支 + 镜像 TTL 刷新，另覆盖契约模型/FakeReader/工厂。

覆盖：
- plan_fallbacks：§8.4 表三能力 × 镜像空/非空全部分支与 degraded 留痕；
- local_entry_version：h-<sha256前12位> 确定性与 UTF-8 口径；
- FakeReMeReader：打分排序、caps 开关下的过滤/版本行为、故障注入、树派生；
- IndexMirror：首载/TTL/force/单飞/失败两分支（空镜像 vs 旧树 stale）/本地过滤原语；
- ReMeReaderFactory：(target,kb_id) 缓存、并发单飞、配置校验、builder 失败不缓存、
  probe 一次性探测。
"""

from __future__ import annotations

import asyncio
import hashlib

import pytest

from tester_agent.adapters.reme import (
    DEFAULT_MIRROR_TTL_SEC,
    CapsProbe,
    Entry,
    FallbackPlan,
    IndexEntryMeta,
    IndexMirror,
    IndexTree,
    ReMeCaps,
    ReMeReader,
    ReMeReaderFactory,
    local_entry_version,
    plan_fallbacks,
)
from tester_agent.domain import EntryType
from tester_agent.errors import KbUnreachable, NotFoundError, ValidationError

from fakes import FakeReMeReader

ALL_CAPS = ReMeCaps(metadata_filter=True, entry_version=True, passage_api=True)
NO_CAPS = ReMeCaps(metadata_filter=False, entry_version=False, passage_api=False)


def make_entry(
    entry_id: str,
    *,
    title: str = "",
    content: str = "",
    entry_type: EntryType = EntryType.BUSINESS,
    link_id: str | None = None,
    story_id: str | None = None,
    entry_version: str = "v1",
    updated_at: str | None = "2026-09-20T00:00:00Z",
    raw: dict | None = None,
) -> Entry:
    return Entry(
        entry_id=entry_id,
        entry_version=entry_version,
        title=title or entry_id,
        content=content,
        entry_type=entry_type,
        link_id=link_id,
        story_id=story_id,
        updated_at=updated_at,
        raw=raw if raw is not None else {},
    )


# ---------- §8.4 降级判定 ----------


class TestPlanFallbacks:
    def test_all_enabled_no_fallback(self):
        plan = plan_fallbacks(ALL_CAPS, mirror_empty=False)
        assert isinstance(plan, FallbackPlan)
        assert plan.use_mirror_filter is False
        assert plan.skip_meta_filter is False
        assert plan.local_entry_version is False
        assert plan.local_passage_split is False
        assert plan.degraded == []

    def test_all_enabled_mirror_empty_irrelevant(self):
        # caps 齐全时镜像状态不影响方案
        plan = plan_fallbacks(ALL_CAPS, mirror_empty=True)
        assert plan.degraded == []

    def test_no_metadata_filter_mirror_present(self):
        caps = ReMeCaps(metadata_filter=False, entry_version=True, passage_api=True)
        plan = plan_fallbacks(caps, mirror_empty=False)
        assert plan.use_mirror_filter is True
        assert plan.skip_meta_filter is False
        assert len(plan.degraded) == 1
        d = plan.degraded[0]
        assert d.step == "meta_filter"
        assert d.reason == "metadata_filter_unavailable"
        assert d.fallback == "index_mirror_local_filter"

    def test_no_metadata_filter_mirror_empty_skip(self):
        caps = ReMeCaps(metadata_filter=False, entry_version=True, passage_api=True)
        plan = plan_fallbacks(caps, mirror_empty=True)
        assert plan.use_mirror_filter is False
        assert plan.skip_meta_filter is True
        assert plan.degraded[0].fallback == "skip_filter_all_to_rerank"

    def test_no_entry_version_local_hash(self):
        caps = ReMeCaps(metadata_filter=True, entry_version=False, passage_api=True)
        plan = plan_fallbacks(caps, mirror_empty=False)
        assert plan.local_entry_version is True
        assert plan.use_mirror_filter is False
        (d,) = plan.degraded
        assert d.step == "entry_version"
        assert d.reason == "entry_version_unavailable"
        assert d.fallback == "local_content_hash"

    def test_no_passage_api_local_split(self):
        caps = ReMeCaps(metadata_filter=True, entry_version=True, passage_api=False)
        plan = plan_fallbacks(caps, mirror_empty=False)
        assert plan.local_passage_split is True
        (d,) = plan.degraded
        assert d.step == "passage_extract"
        assert d.reason == "passage_api_unavailable"
        assert d.fallback == "fetch_full_then_local_split"

    def test_all_missing_mirror_empty_three_branches(self):
        plan = plan_fallbacks(NO_CAPS, mirror_empty=True)
        assert plan.skip_meta_filter is True
        assert plan.use_mirror_filter is False
        assert plan.local_entry_version is True
        assert plan.local_passage_split is True
        assert [d.step for d in plan.degraded] == [
            "meta_filter",
            "entry_version",
            "passage_extract",
        ]
        assert [d.fallback for d in plan.degraded] == [
            "skip_filter_all_to_rerank",
            "local_content_hash",
            "fetch_full_then_local_split",
        ]

    def test_all_missing_mirror_present_uses_mirror(self):
        plan = plan_fallbacks(NO_CAPS, mirror_empty=False)
        assert plan.use_mirror_filter is True
        assert plan.skip_meta_filter is False
        assert len(plan.degraded) == 3


class TestLocalEntryVersion:
    def test_format_and_length(self):
        v = local_entry_version("abc")
        assert v.startswith("h-")
        assert len(v) == len("h-") + 12
        assert all(c in "0123456789abcdef" for c in v[2:])

    def test_deterministic_and_content_sensitive(self):
        assert local_entry_version("同一内容") == local_entry_version("同一内容")
        assert local_entry_version("内容甲") != local_entry_version("内容乙")

    def test_uses_utf8_sha256_first12(self):
        text = "退款超时规则与中文内容"
        expected = "h-" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
        assert local_entry_version(text) == expected

    def test_empty_content_still_valid(self):
        assert local_entry_version("") == "h-" + hashlib.sha256(b"").hexdigest()[:12]


# ---------- 契约模型 ----------


class TestModels:
    def test_entry_chinese_json_roundtrip(self):
        e = make_entry("ent_1", title="退款超时规则", content="超过 24 小时自动退款")
        restored = Entry.model_validate_json(e.model_dump_json())
        assert restored.title == "退款超时规则"
        assert restored.content == "超过 24 小时自动退款"
        assert restored.entry_type is EntryType.BUSINESS

    def test_entry_raw_preserves_extra_fields(self):
        e = make_entry("ent_1", raw={"score": 0.9, "source": "reme", "nested": {"k": "中文"}})
        restored = Entry.model_validate(e.model_dump())
        assert restored.raw["nested"] == {"k": "中文"}
        assert restored.raw["score"] == 0.9

    def test_index_tree_nested_defaults_and_copy(self):
        tree = IndexTree.model_validate(
            {
                "links": [
                    {
                        "link_id": "L1",
                        "entry_id": "e-l1",
                        "entry_version": "2026-09-20T03:11:00Z",
                        "title": "支付链路",
                        "summary": "支付相关索引",
                        "stories": [
                            {
                                "story_id": "S1",
                                "entry_id": "e-s1",
                                "entry_version": "v9",
                                "title": "退款故事",
                            }
                        ],
                    }
                ]
            }
        )
        assert tree.links[0].stories[0].summary == ""  # 缺省一句话
        assert tree.model_dump_json()  # 可 JSON 序列化

    def test_index_entry_meta_type_is_link_index(self):
        m = IndexEntryMeta(
            entry_id="e1", entry_version="v", title="t", kind="link", link_id="L1"
        )
        assert m.entry_type is EntryType.LINK_INDEX


# ---------- FakeReMeReader ----------


@pytest.fixture
def sample_entries():
    return [
        make_entry(
            "e-api-1",
            title="退款接口规范",
            content="POST /refund 创建退款，参数 amount 与 order_id",
            entry_type=EntryType.API,
            link_id="L-pay",
            story_id="S-refund",
        ),
        make_entry(
            "e-biz-1",
            title="超时规则",
            content="退款超过 24 小时未处理则自动退款并通知商户",
            entry_type=EntryType.BUSINESS,
            link_id="L-pay",
            story_id="S-timeout",
        ),
        make_entry(
            "e-db-1",
            title="订单表结构",
            content="orders 表含 status、amount 字段，与退款无关",
            entry_type=EntryType.DB,
            link_id="L-order",
            story_id="S-order",
        ),
        make_entry(
            "e-idx-link",
            title="支付链路索引",
            content="链路摘要正文",
            entry_type=EntryType.LINK_INDEX,
            link_id="L-pay",
            story_id=None,
            raw={"summary": "支付全链路索引摘要"},
        ),
        make_entry(
            "e-idx-story",
            title="退款故事索引",
            content="故事摘要正文",
            entry_type=EntryType.LINK_INDEX,
            link_id="L-pay",
            story_id="S-refund",
            raw={"summary": "退款处理的一句话简介"},
        ),
    ]


class TestFakeReader:
    async def test_protocol_structural_match(self, sample_entries):
        reader = FakeReMeReader(sample_entries)
        assert isinstance(reader, ReMeReader)

    async def test_search_scoring_title_weights_and_topk(self, sample_entries):
        reader = FakeReMeReader(sample_entries)
        # "退款" 命中：e-api-1 标题 1 + 正文 1；e-biz-1 正文 1；e-idx-story 标题 1
        hits = await reader.search("退款", top_k=2)
        assert [h.entry_id for h in hits] == ["e-api-1", "e-idx-story"]
        assert reader.search_calls == 1

    async def test_search_excludes_zero_hits_and_cuts_topk(self, sample_entries):
        reader = FakeReMeReader(sample_entries)
        hits = await reader.search("不存在的关键词xyz", top_k=10)
        assert hits == []

    async def test_search_types_filter_when_caps_on(self, sample_entries):
        reader = FakeReMeReader(sample_entries)  # caps 全开
        hits = await reader.search("退款 订单", top_k=10, types=[EntryType.DB.value])
        assert {h.entry_id for h in hits} == {"e-db-1"}

    async def test_search_scope_filter_when_caps_on(self, sample_entries):
        reader = FakeReMeReader(sample_entries)
        hits = await reader.search(
            "退款", top_k=10, scope={"link_ids": ["L-pay"], "story_ids": ["S-refund"]}
        )
        # S-timeout/S-order 故事被 scope 剔除；L-pay+S-refund 下的保留
        assert "e-biz-1" not in {h.entry_id for h in hits}
        assert "e-api-1" in {h.entry_id for h in hits}

    async def test_search_ignores_filters_when_caps_off(self, sample_entries):
        reader = FakeReMeReader(
            sample_entries,
            caps=ReMeCaps(metadata_filter=False, entry_version=True, passage_api=True),
        )
        hits = await reader.search(
            "退款 订单", top_k=10, types=[EntryType.API.value],
            scope={"link_ids": ["nope"]},
        )
        # 服务端不支持过滤：types/scope 被忽略，DB 条目照样返回
        assert "e-db-1" in {h.entry_id for h in hits}

    async def test_version_stripped_when_entry_version_caps_off(self, sample_entries):
        reader = FakeReMeReader(
            sample_entries,
            caps=ReMeCaps(metadata_filter=True, entry_version=False, passage_api=True),
        )
        (hit,) = await reader.search("订单表结构", top_k=5)
        assert hit.entry_version == ""
        assert hit.updated_at is None
        got = await reader.get_entry("e-db-1")
        assert got.entry_version == ""
        # 夹具内部数据未被污染
        assert reader._entries["e-db-1"].entry_version == "v1"

    async def test_get_entry_missing_raises_not_found(self, sample_entries):
        reader = FakeReMeReader(sample_entries)
        with pytest.raises(NotFoundError):
            await reader.get_entry("no-such-id")
        assert reader.get_calls == 1

    async def test_failure_injection(self, sample_entries):
        reader = FakeReMeReader(
            sample_entries, fail_search=True, fail_get=True, fail_tree=True
        )
        with pytest.raises(KbUnreachable):
            await reader.search("q", top_k=1)
        with pytest.raises(KbUnreachable):
            await reader.get_entry("e-api-1")
        with pytest.raises(KbUnreachable):
            await reader.list_index_tree()

    async def test_custom_exception_injection(self, sample_entries):
        reader = FakeReMeReader(sample_entries, fail_search=RuntimeError("boom"))
        with pytest.raises(RuntimeError, match="boom"):
            await reader.search("q", top_k=1)

    async def test_derive_tree_groups_links_and_stories(self, sample_entries):
        reader = FakeReMeReader(sample_entries)
        tree = await reader.list_index_tree()
        assert reader.tree_calls == 1
        assert len(tree.links) == 1
        link = tree.links[0]
        assert link.link_id == "L-pay"
        assert link.entry_id == "e-idx-link"
        assert link.summary == "支付全链路索引摘要"
        assert [s.story_id for s in link.stories] == ["S-refund"]
        assert link.stories[0].summary == "退款处理的一句话简介"

    async def test_explicit_tree_returned_as_is(self, sample_entries):
        from tester_agent.adapters.reme import IndexLink

        fixed = IndexTree(links=[])
        reader = FakeReMeReader(sample_entries, tree=fixed)
        tree = await reader.list_index_tree()
        assert tree.links == []
        # 返回的是深拷贝，调用方改不到夹具内部
        tree.links.append(
            IndexLink(link_id="LX", entry_id="e-x", entry_version="v", title="x")
        )
        assert (await reader.list_index_tree()).links == []

    async def test_update_content_reflected(self, sample_entries):
        reader = FakeReMeReader(sample_entries)
        reader.update_content("e-db-1", "全新内容", updated_at="2026-09-27T00:00:00Z")
        got = await reader.get_entry("e-db-1")
        assert got.content == "全新内容"
        assert got.updated_at == "2026-09-27T00:00:00Z"


# ---------- IndexMirror ----------


@pytest.fixture
def tree_reader():
    reader = FakeReMeReader(
        [
            make_entry(
                "e-l1", title="支付链路", entry_type=EntryType.LINK_INDEX,
                link_id="L1", story_id=None, raw={"summary": "支付"},
            ),
            make_entry(
                "e-l2", title="订单链路", entry_type=EntryType.LINK_INDEX,
                link_id="L2", story_id=None, raw={"summary": "订单"},
            ),
            make_entry(
                "e-s1", title="退款故事", entry_type=EntryType.LINK_INDEX,
                link_id="L1", story_id="S1", raw={"summary": "退款"},
            ),
            make_entry(
                "e-s2", title="对账故事", entry_type=EntryType.LINK_INDEX,
                link_id="L1", story_id="S2", raw={"summary": "对账"},
            ),
        ]
    )
    return reader


class TestIndexMirrorRefresh:
    async def test_first_load_and_lookup_primitives(self, tree_reader):
        mirror = IndexMirror(tree_reader, ttl_sec=60)
        assert mirror.is_empty is True
        assert mirror.last_refreshed_at is None

        refreshed = await mirror.ensure_fresh()
        assert refreshed is True
        assert tree_reader.tree_calls == 1
        assert mirror.is_empty is False
        assert mirror.stale is False
        assert mirror.last_error is None
        assert mirror.last_refreshed_at is not None

        link_meta = mirror.meta("e-l1")
        assert isinstance(link_meta, IndexEntryMeta)
        assert link_meta.kind == "link"
        assert link_meta.link_id == "L1"
        story_meta = mirror.meta("e-s1")
        assert story_meta.kind == "story"
        assert story_meta.story_id == "S1"
        assert mirror.known_link_ids() == ["L1", "L2"]
        assert mirror.known_story_ids() == ["S1", "S2"]
        assert mirror.meta("missing") is None

    async def test_within_ttl_no_refetch(self, tree_reader):
        clock = [100.0]
        mirror = IndexMirror(tree_reader, ttl_sec=60, clock=lambda: clock[0])
        await mirror.ensure_fresh()
        clock[0] += 59
        assert await mirror.ensure_fresh() is False
        assert tree_reader.tree_calls == 1

    async def test_ttl_expiry_refetch(self, tree_reader):
        clock = [0.0]
        mirror = IndexMirror(tree_reader, ttl_sec=60, clock=lambda: clock[0])
        await mirror.ensure_fresh()
        clock[0] = 59.99
        await mirror.ensure_fresh()
        assert tree_reader.tree_calls == 1
        clock[0] = 60.0  # 到期（边界：>= ttl 即陈旧）
        assert await mirror.ensure_fresh() is True
        assert tree_reader.tree_calls == 2

    async def test_force_refresh_ignores_ttl(self, tree_reader):
        mirror = IndexMirror(tree_reader, ttl_sec=3600)
        await mirror.ensure_fresh()
        assert await mirror.ensure_fresh(force=True) is True
        assert tree_reader.tree_calls == 2

    async def test_default_ttl_is_60_min(self, tree_reader):
        assert DEFAULT_MIRROR_TTL_SEC == 3600
        mirror = IndexMirror(tree_reader)
        assert mirror.ttl_sec == 3600

    async def test_concurrent_refresh_single_flight(self, tree_reader):
        clock = [0.0]
        mirror = IndexMirror(tree_reader, ttl_sec=60, clock=lambda: clock[0])
        await mirror.ensure_fresh()
        clock[0] = 61  # 集体到期
        results = await asyncio.gather(*[mirror.ensure_fresh() for _ in range(5)])
        # 恰好一次实际拉取；其余协程双检命中
        assert tree_reader.tree_calls == 2  # 首载 1 + 到期后 1
        assert sum(results) == 1

    async def test_empty_tree_counts_as_empty(self):
        reader = FakeReMeReader([], tree=IndexTree(links=[]))
        mirror = IndexMirror(reader)
        await mirror.ensure_fresh()
        assert mirror.is_empty is True
        assert mirror.tree is not None

    async def test_initial_load_failure_keeps_empty_no_raise(self, tree_reader):
        tree_reader.fail_tree = True
        mirror = IndexMirror(tree_reader)
        refreshed = await mirror.ensure_fresh()
        assert refreshed is True  # 发起过尝试
        assert mirror.is_empty is True
        assert mirror.stale is False
        assert mirror.last_error is not None
        # 恢复后下次刷新成功
        tree_reader.fail_tree = None
        await mirror.ensure_fresh(force=True)
        assert mirror.is_empty is False
        assert mirror.last_error is None

    async def test_refresh_failure_after_success_keeps_stale_tree(self, tree_reader):
        clock = [0.0]
        mirror = IndexMirror(tree_reader, ttl_sec=60, clock=lambda: clock[0])
        await mirror.ensure_fresh()
        tree_reader.fail_tree = True
        clock[0] = 61
        await mirror.ensure_fresh()  # best-effort：不抛
        assert mirror.is_empty is False  # 旧树继续服务
        assert mirror.stale is True
        assert mirror.last_error is not None
        assert mirror.meta("e-l1") is not None
        # 恢复
        tree_reader.fail_tree = None
        await mirror.ensure_fresh(force=True)
        assert mirror.stale is False
        assert mirror.last_error is None

    async def test_refresh_raises_on_failure(self, tree_reader):
        mirror = IndexMirror(tree_reader)
        tree_reader.fail_tree = True
        with pytest.raises(KbUnreachable):
            await mirror.refresh()
        assert mirror.tree is None

    async def test_refresh_returns_tree_on_success(self, tree_reader):
        mirror = IndexMirror(tree_reader)
        tree = await mirror.refresh()
        assert tree.links[0].link_id == "L1"
        assert tree_reader.tree_calls == 1


class TestIndexMirrorFilter:
    @pytest.fixture
    def mirror(self, tree_reader):
        return IndexMirror(tree_reader)

    async def test_type_match(self, mirror):
        await mirror.ensure_fresh()
        ok, reason = mirror.matches("e-l1", types=["link_index"])
        assert (ok, reason) == (True, None)
        ok, reason = mirror.matches("e-l1", types=["api"])
        assert (ok, reason) == (False, "filtered_type")

    async def test_scope_match(self, mirror):
        await mirror.ensure_fresh()
        # 故事 S1 属于 L1：在 L1 范围内保留
        ok, _ = mirror.matches("e-s1", link_ids={"L1"}, story_ids={"S1"})
        assert ok is True
        # 故事 S2 不在 story 白名单 → 剔除
        ok, reason = mirror.matches("e-s2", link_ids={"L1"}, story_ids={"S1"})
        assert (ok, reason) == (False, "filtered_scope")
        # 链路 L2 不在 link 白名单
        ok, reason = mirror.matches("e-l2", link_ids={"L1"})
        assert (ok, reason) == (False, "filtered_scope")
        # 链路节点本身 story_id=None，给 story 白名单不被误杀（先过 link 白名单）
        ok, _ = mirror.matches("e-l1", link_ids={"L1"}, story_ids={"S1"})
        assert ok is True

    async def test_unknown_entry_type_kept_scope_dropped(self, mirror):
        await mirror.ensure_fresh()
        # 无 meta 时类型不可判定 → 不误杀
        ok, reason = mirror.matches("unknown", types=["link_index"])
        assert (ok, reason) == (True, None)
        # 无 meta 时归属白名单不可证实 → 剔除
        ok, reason = mirror.matches("unknown", link_ids={"L1"})
        assert (ok, reason) == (False, "filtered_scope")


# ---------- ReMeReaderFactory ----------


class CountingBuilder:
    """默认每次构造返回新 reader（模拟真实 builder 按工作区建实例）；
    传 reader= 可固定返回同一实例。"""

    def __init__(self, *, fail: bool = False, reader: FakeReMeReader | None = None):
        self.calls = 0
        self.configs: list[dict] = []
        self._fail = fail
        self._fixed = reader
        self.last_reader: FakeReMeReader | None = reader

    async def __call__(self, kb_config: dict) -> ReMeReader:
        self.calls += 1
        self.configs.append(kb_config)
        if self._fail:
            raise KbUnreachable("builder 模拟连接失败")
        reader = self._fixed if self._fixed is not None else FakeReMeReader([])
        self.last_reader = reader
        return reader


class TestReaderFactory:
    async def test_cache_by_target_and_kb_id(self):
        factory = ReMeReaderFactory()
        builder = CountingBuilder()
        factory.register("sdk", builder)
        cfg = {"mode": "sdk", "target": "/data/reme", "kb_id": "kb-1", "options": {}}

        r1 = await factory.for_workspace(cfg)
        r2 = await factory.for_workspace(dict(cfg, options={"x": 1}))
        assert r1 is r2  # options 不进缓存键（dd §9.1：(target, kb_id)）
        assert builder.calls == 1
        # 不同 kb_id → 不同实例
        r3 = await factory.for_workspace(dict(cfg, kb_id="kb-2"))
        assert r3 is not r1
        assert builder.calls == 2
        assert builder.configs[1]["kb_id"] == "kb-2"

    async def test_concurrent_same_key_single_flight(self):
        factory = ReMeReaderFactory()
        builder = CountingBuilder()
        factory.register("sdk", builder)
        cfg = {"mode": "sdk", "target": "t", "kb_id": "kb"}
        results = await asyncio.gather(*[factory.for_workspace(cfg) for _ in range(4)])
        assert builder.calls == 1
        assert all(r is results[0] for r in results)

    @pytest.mark.parametrize(
        "cfg",
        [
            {},
            {"target": "t", "kb_id": "k"},
            {"mode": "sdk", "kb_id": "k"},
            {"mode": "sdk", "target": "t"},
            {"mode": "", "target": "t", "kb_id": "k"},
            {"mode": "sdk", "target": "  ", "kb_id": "k"},
            {"mode": 123, "target": "t", "kb_id": "k"},
        ],
    )
    async def test_invalid_config(self, cfg):
        factory = ReMeReaderFactory()
        factory.register("sdk", CountingBuilder())
        with pytest.raises(ValidationError):
            await factory.for_workspace(cfg)

    async def test_non_dict_config(self):
        factory = ReMeReaderFactory()
        with pytest.raises(ValidationError):
            await factory.for_workspace("not-a-dict")  # type: ignore[arg-type]

    async def test_unregistered_mode(self):
        factory = ReMeReaderFactory()
        with pytest.raises(ValidationError) as exc:
            await factory.for_workspace(
                {"mode": "service", "target": "http://x", "kb_id": "k"}
            )
        assert exc.value.details == {"mode": "service"}

    async def test_builder_failure_not_cached(self):
        factory = ReMeReaderFactory()
        builder = CountingBuilder(fail=True)
        factory.register("sdk", builder)
        cfg = {"mode": "sdk", "target": "t", "kb_id": "k"}
        with pytest.raises(KbUnreachable):
            await factory.for_workspace(cfg)
        with pytest.raises(KbUnreachable):
            await factory.for_workspace(cfg)
        assert builder.calls == 2  # 失败不落缓存，允许重试

    async def test_builder_failure_then_success(self):
        factory = ReMeReaderFactory()
        builder = CountingBuilder(fail=True)
        factory.register("sdk", builder)
        cfg = {"mode": "sdk", "target": "t", "kb_id": "k"}
        with pytest.raises(KbUnreachable):
            await factory.for_workspace(cfg)
        builder._fail = False
        reader = await factory.for_workspace(cfg)
        assert reader is builder.last_reader
        assert builder.calls == 2
        # 成功后缓存生效
        assert await factory.for_workspace(cfg) is reader
        assert builder.calls == 2


class TestFactoryProbe:
    async def test_probe_returns_caps_and_latency_uncached(self):
        factory = ReMeReaderFactory()
        builder = CountingBuilder()
        factory.register("sdk", builder)
        cfg = {"mode": "sdk", "target": "t", "kb_id": "k"}
        result = await factory.probe(cfg)
        assert isinstance(result, CapsProbe)
        assert result.caps == ALL_CAPS
        assert result.latency_ms >= 0
        await factory.probe(cfg)
        assert builder.calls == 2  # probe 不走缓存
        # probe 不填充 for_workspace 缓存：首次 for_workspace 仍需构造
        reader = await factory.for_workspace(cfg)
        assert builder.calls == 3
        assert await factory.for_workspace(cfg) is reader  # 之后命中缓存
        assert builder.calls == 3

    async def test_probe_failure_propagates(self):
        factory = ReMeReaderFactory()
        factory.register("sdk", CountingBuilder(fail=True))
        with pytest.raises(KbUnreachable):
            await factory.probe({"mode": "sdk", "target": "t", "kb_id": "k"})

    async def test_probe_unregistered_mode(self):
        factory = ReMeReaderFactory()
        with pytest.raises(ValidationError):
            await factory.probe({"mode": "service", "target": "t", "kb_id": "k"})
