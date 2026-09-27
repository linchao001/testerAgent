"""测试夹具级替身：FakeReMeReader（dd §9.1 ReMeReader）/ FakeLLM（dd §9.3 LLMClient）。

WP-08 落地，供 WP-10/11/12/17/28 等检索链路单测与 WP-13 eval smoke 复用：

- entries 内存存放；search 为确定性简单词面打分（标题命中权重 3 倍），
  只在 caps.metadata_filter=True 时执行 types/scope 服务端过滤——能力关闭时
  故意忽略过滤参数，以逼出 §8.4 的镜像本地过滤降级路径；
- caps.entry_version=False 时返回条目的 entry_version 置空、updated_at=None，
  模拟上游不回传版本，逼出本地 h- hash 降级；
- fail_search/fail_get/fail_tree 支持故障注入（True 抛默认 KbUnreachable，
  或直接给 Exception 实例）；所有外部调用带计数器。

scope 的测试约定（dd 只冻结为 dict，具体键由 WP-10 算子落型）：
``{"link_ids": [...], "story_ids": [...]}``。
"""

from __future__ import annotations

from typing import Any

from tester_agent.adapters.reme import (
    Entry,
    IndexLink,
    IndexStory,
    IndexTree,
    ReMeCaps,
)
from tester_agent.domain import EntryType
from tester_agent.errors import KbUnreachable, NotFoundError

_FAIL = KbUnreachable("FakeReMeReader injected failure")


class FakeReMeReader:
    """实现 adapters.reme.ReMeReader Protocol 的内存替身（非继承，结构对型）。"""

    def __init__(
        self,
        entries: list[Entry] | None = None,
        *,
        caps: ReMeCaps | None = None,
        tree: IndexTree | None = None,
        fail_search: Any = None,
        fail_get: Any = None,
        fail_tree: Any = None,
    ) -> None:
        self.caps: ReMeCaps = caps or ReMeCaps(
            metadata_filter=True, entry_version=True, passage_api=True
        )
        self._entries: dict[str, Entry] = {}
        for e in entries or []:
            self._entries[e.entry_id] = e.model_copy(deep=True)
        self._explicit_tree = tree.model_copy(deep=True) if tree is not None else None
        # 故障注入：None=不注入；True=默认 KbUnreachable；Exception 实例=直接抛
        self.fail_search = fail_search
        self.fail_get = fail_get
        self.fail_tree = fail_tree
        self.search_calls = 0
        self.get_calls = 0
        self.tree_calls = 0
        self.search_queries: list[str] = []

    # ---- 夹具装配辅助 ----

    def add_entry(self, entry: Entry) -> None:
        self._entries[entry.entry_id] = entry.model_copy(deep=True)

    def update_content(
        self, entry_id: str, content: str, *, updated_at: str | None = None
    ) -> None:
        """模拟 ReMe 侧条目更新（版本/更新时间变化），驱动缓存版本校验测试。"""
        if entry_id not in self._entries:
            raise NotFoundError(f"fake 中无此条目: {entry_id}")
        entry = self._entries[entry_id]
        entry.content = content
        if updated_at is not None:
            entry.updated_at = updated_at

    def set_caps(self, caps: ReMeCaps) -> None:
        self.caps = caps

    # ---- ReMeReader Protocol ----

    async def search(
        self,
        query: str,
        *,
        top_k: int,
        types: list[str] | None = None,
        scope: dict | None = None,
    ) -> list[Entry]:
        self.search_calls += 1
        self.search_queries.append(query)
        _maybe_raise(self.fail_search)

        hits: list[tuple[int, Entry]] = []
        for entry in self._entries.values():
            if self.caps.metadata_filter and not self._passes_filter(entry, types, scope):
                continue
            score = self._score(query, entry)
            if score > 0:
                hits.append((score, entry))
        hits.sort(key=lambda pair: (-pair[0], pair[1].entry_id))
        return [self._served(e) for _s, e in hits[:top_k]]

    async def get_entry(self, entry_id: str) -> Entry:
        self.get_calls += 1
        _maybe_raise(self.fail_get)
        entry = self._entries.get(entry_id)
        if entry is None:
            raise NotFoundError(f"fake 知识库无此条目: {entry_id}")
        return self._served(entry)

    async def list_index_tree(self) -> IndexTree:
        self.tree_calls += 1
        _maybe_raise(self.fail_tree)
        if self._explicit_tree is not None:
            return self._explicit_tree.model_copy(deep=True)
        return self._derive_tree()

    # ---- 内部 ----

    @staticmethod
    def _score(query: str, entry: Entry) -> int:
        """词面打分：空白分词，标题子串计数权重 3、正文权重 1（确定性，无 LLM）。"""
        terms = query.lower().split()
        title = entry.title.lower()
        content = entry.content.lower()
        return sum(title.count(t) * 3 + content.count(t) for t in terms if t)

    @staticmethod
    def _passes_filter(entry: Entry, types: list[str] | None, scope: dict | None) -> bool:
        if types is not None and entry.entry_type.value not in types:
            return False
        if scope:
            link_ids = scope.get("link_ids")
            story_ids = scope.get("story_ids")
            if link_ids is not None and entry.link_id not in link_ids:
                return False
            if story_ids is not None and entry.story_id not in story_ids:
                return False
        return True

    def _served(self, entry: Entry) -> Entry:
        out = entry.model_copy(deep=True)
        # 能力位关闭：模拟上游不回传版本字段（dd §8.4 → 本地 h- hash 降级）
        if not self.caps.entry_version:
            out.entry_version = ""
            out.updated_at = None
        return out

    def _derive_tree(self) -> IndexTree:
        """从 LINK_INDEX 条目派生两级树：story_id 为空=链路，否则按 link_id 归属。"""
        links: dict[str, IndexLink] = {}
        stories: dict[str, list[IndexStory]] = {}
        for e in self._entries.values():
            if e.entry_type is not EntryType.LINK_INDEX:
                continue
            if e.story_id is None:
                links[e.link_id or e.entry_id] = IndexLink(
                    link_id=e.link_id or e.entry_id,
                    entry_id=e.entry_id,
                    entry_version=e.entry_version,
                    title=e.title,
                    summary=e.raw.get("summary", ""),
                )
            else:
                stories.setdefault(e.link_id or "", []).append(
                    IndexStory(
                        story_id=e.story_id,
                        entry_id=e.entry_id,
                        entry_version=e.entry_version,
                        title=e.title,
                        summary=e.raw.get("summary", ""),
                    )
                )
        out_links: list[IndexLink] = []
        for link_id in sorted(links):
            link = links[link_id].model_copy()
            link.stories = sorted(stories.get(link_id, []), key=lambda s: s.entry_id)
            out_links.append(link)
        return IndexTree(links=out_links)


def _maybe_raise(spec: Any) -> None:
    if spec is None:
        return
    if isinstance(spec, BaseException):
        raise spec
    raise _FAIL


class FakeLLM:
    """实现 adapters.llm.LLMClient Protocol 的脚本化替身（非继承，结构对型）。

    - responses 为响应脚本队列：str（作为 content 返回）或 Exception 实例
      （抛出）；耗尽后复用最后一个 str（未提供则抛 LLMBadOutput）；
    - 记录每次请求的 messages 与 json_schema（chat_calls / calls 计数），
      usage 固定可断言 aux 累加口径。
    """

    def __init__(
        self,
        responses: list[Any],
        *,
        usage: dict[str, int] | None = None,
        model: str = "fake-model",
    ) -> None:
        from tester_agent.adapters.llm import LLMResult

        self._result_cls = LLMResult
        self._script = list(responses)
        self._usage = usage or {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
        self._model = model
        self.chat_calls = 0
        self.calls: list[dict[str, Any]] = []

    async def chat(
        self,
        messages: list[dict[str, str]],
        *,
        model: str | None = None,
        temperature: float | None = None,
        json_schema: dict | None = None,
        timeout: float | None = None,
        stream_writer: Any = None,
    ) -> Any:
        self.chat_calls += 1
        self.calls.append(
            {"messages": messages, "json_schema": json_schema, "model": model}
        )
        if self._script:
            item = self._script.pop(0)
        else:
            from tester_agent.errors import LLMBadOutput

            raise LLMBadOutput("FakeLLM 脚本耗尽")
        if isinstance(item, BaseException):
            raise item
        return self._result_cls(
            content=item,
            usage=dict(self._usage),
            model=model or self._model,
            finish_reason="stop",
            retries=0,
            latency_ms=1,
        )
