"""ReMe 只读接入契约层（dd §8.4 能力位与降级 / §9.1 ReMeReader）。

WP-08 范围（本模块不包含任何真实 ReMe 调用，WP-09 落地 reme_sdk/reme_http）：

- 冻结契约：``ReMeCaps`` / ``Entry`` / ``IndexTree`` / ``ReMeReader`` Protocol
  （Reader 不持有任何写方法，从类型上消灭图内写库路径，dd §9.1）；
- 降级判定：``plan_fallbacks`` 按 caps 三态产出 FallbackPlan + DegradedStep 列表，
  ``local_entry_version`` 提供 entry_version 能力缺失时的 ``h-<sha256前12位>``；
- ``IndexMirror``：链路→故事两级只读索引树缓存，启动刷新 + TTL（runtime_config
  ``index_mirror_ttl_min``，默认 60min），单飞刷新、失败保留旧镜像/空镜像降级；
- ``ReMeReaderFactory``：按 ``(target, kb_id)`` 缓存 reader 实例，mode→builder
  注册表（WP-09 注册 sdk/service 真实 builder），另提供连接测试用的一次性 probe。

dd 缺口补型（交接单已登记，不碰冻结字段）：§9.1 引用了 ``IndexTree`` 但未给类
定义，按其 docstring（title/一句话/entry_id/version/归属）与 tech-design §4.3
（仅标题+一句话+类型+归属）补 IndexLink/IndexStory/IndexTree/IndexEntryMeta。

§8.4 文字写 ``capabilities()`` 方法、§9.1 冻结 Protocol 为 ``caps`` 属性：以
§9.1 冻结签名为准——探测由工厂 builder 在构造期完成（async 构造不能进
__init__），reader 只暴露探测后缓存的 caps。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from typing import (
    Awaitable,
    Callable,
    Literal,
    Mapping,
    Protocol,
    runtime_checkable,
)

from pydantic import BaseModel

from ..domain import DegradedStep, EntryType
from ..errors import KbUnreachable, ValidationError

logger = logging.getLogger(__name__)

# dd §13.2 runtime_config.index_mirror_ttl_min = 60
DEFAULT_MIRROR_TTL_SEC = 60 * 60


# ---------- §9.1 / §8.4 冻结契约 ----------


class ReMeCaps(BaseModel):
    """ReMe 三能力位（dd §8.4）。连接测试与任务启动时探测，缓存在 reader 上。"""

    metadata_filter: bool  # search 是否支持 types/scope 参数
    entry_version: bool  # 是否返回 updated_at/内容 hash
    passage_api: bool  # 是否支持段落级检索；False 则全量拉回本地切


class Entry(BaseModel):
    """ReMe 知识条目（dd §9.1 冻结字段）。raw 为原始字段，适配层外不消费。"""

    entry_id: str
    entry_version: str
    title: str
    content: str
    entry_type: EntryType
    link_id: str | None
    story_id: str | None
    updated_at: str | None
    raw: dict


# ---- §9.1 IndexTree 补型（dd 未给类定义，按 docstring/§4.3 补） ----


class IndexStory(BaseModel):
    """索引树故事节点（第二级）。"""

    story_id: str
    entry_id: str
    entry_version: str
    title: str
    summary: str = ""  # 一句话简介


class IndexLink(BaseModel):
    """索引树链路节点（第一级），stories 为其归属故事。"""

    link_id: str
    entry_id: str
    entry_version: str
    title: str
    summary: str = ""
    stories: list[IndexStory] = []


class IndexTree(BaseModel):
    """链路→故事两级树；索引镜像与 GET /kb/tree 共用（dd §9.1）。"""

    links: list[IndexLink] = []


class IndexEntryMeta(BaseModel):
    """索引树拍平后的条目元数据（本地过滤用；两类节点 entry_type 均为 link_index）。"""

    entry_id: str
    entry_version: str
    title: str
    summary: str = ""
    kind: Literal["link", "story"]
    link_id: str
    story_id: str | None = None

    @property
    def entry_type(self) -> EntryType:
        return EntryType.LINK_INDEX


@runtime_checkable
class ReMeReader(Protocol):
    """dd §9.1 冻结只读 Protocol（无任何写方法）。"""

    caps: ReMeCaps

    async def search(
        self,
        query: str,
        *,
        top_k: int,
        types: list[str] | None = None,
        scope: dict | None = None,
    ) -> list[Entry]: ...

    async def get_entry(self, entry_id: str) -> Entry: ...

    async def list_index_tree(self) -> IndexTree: ...


# ---------- §8.4 降级判定（每次管线构造时读 caps，不写死全局） ----------


class FallbackPlan(BaseModel):
    """caps 三态对应的降级动作；degraded 为直接可写入 trace 的留痕记录。"""

    use_mirror_filter: bool = False  # metadata_filter=False 且镜像非空：本地过滤
    skip_meta_filter: bool = False  # metadata_filter=False 且镜像空：全量进 rerank
    local_entry_version: bool = False  # entry_version=False：本地 h- hash
    local_passage_split: bool = False  # passage_api=False：拉正文本地切分
    degraded: list[DegradedStep] = []


def local_entry_version(content: str) -> str:
    """entry_version 能力缺失时的本地版本（dd §8.4：``h-{contenthash前12位}``）。

    hash 口径与全项目一致取 sha256（UTF-8 编码，见 workspace_files.HASH_PREFIX）。
    """
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()[:12]
    return f"h-{digest}"


def plan_fallbacks(caps: ReMeCaps, *, mirror_empty: bool) -> FallbackPlan:
    """按能力位产出降级方案（dd §8.4 表）。纯函数，便于单测与 eval 复用。

    - metadata_filter=False：镜像非空走 IndexMirror 本地过滤；镜像空跳过过滤，
      全量交 rerank，两种情况均写 degraded；
    - entry_version=False：拉回内容时算本地 h- hash（面板提示"版本由本地计算"）；
    - passage_api=False：recall 后批量 get_entry 拉正文，本地结构化切分。
    """
    degraded: list[DegradedStep] = []
    use_mirror_filter = False
    skip_meta_filter = False

    if not caps.metadata_filter:
        if mirror_empty:
            skip_meta_filter = True
            degraded.append(
                DegradedStep(
                    step="meta_filter",
                    reason="metadata_filter_unavailable",
                    fallback="skip_filter_all_to_rerank",
                )
            )
        else:
            use_mirror_filter = True
            degraded.append(
                DegradedStep(
                    step="meta_filter",
                    reason="metadata_filter_unavailable",
                    fallback="index_mirror_local_filter",
                )
            )

    local_version = not caps.entry_version
    if local_version:
        degraded.append(
            DegradedStep(
                step="entry_version",
                reason="entry_version_unavailable",
                fallback="local_content_hash",
            )
        )

    local_passage = not caps.passage_api
    if local_passage:
        degraded.append(
            DegradedStep(
                step="passage_extract",
                reason="passage_api_unavailable",
                fallback="fetch_full_then_local_split",
            )
        )

    return FallbackPlan(
        use_mirror_filter=use_mirror_filter,
        skip_meta_filter=skip_meta_filter,
        local_entry_version=local_version,
        local_passage_split=local_passage,
        degraded=degraded,
    )


# ---------- §8.4 IndexMirror：启动刷新 + 60min TTL 的只读索引树缓存 ----------


class IndexMirror:
    """链路/故事索引树的进程内只读缓存（dd §8.4 / tech-design §4.3）。

    刷新语义：
    - ``ensure_fresh``：首载/超过 TTL/force 时拉取；并发调用单飞（共享同一次
      list_index_tree）。best-effort——失败不抛异常：从未成功过则镜像为空
      （plan_fallbacks 走"跳过过滤"分支）；曾成功过则保留旧树并标 stale；
    - ``refresh``：强制拉取，失败原样抛出（连接测试等需要显式感知错误的场景）。

    时间通过 ``clock`` 注入（默认 time.monotonic），TTL 测试不 sleep。
    """

    def __init__(
        self,
        reader: ReMeReader,
        *,
        ttl_sec: float = DEFAULT_MIRROR_TTL_SEC,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._reader = reader
        self._ttl_sec = float(ttl_sec)
        self._clock = clock
        self._lock = asyncio.Lock()
        self._tree: IndexTree | None = None
        self._index: dict[str, IndexEntryMeta] = {}
        self._refreshed_at: float | None = None
        self._stale = False
        self._last_error: str | None = None

    @property
    def ttl_sec(self) -> float:
        return self._ttl_sec

    @property
    def tree(self) -> IndexTree | None:
        """最近一次成功刷新的索引树；从未成功时为 None。"""
        return self._tree

    @property
    def is_empty(self) -> bool:
        """从未成功加载（或加载到空树）。空镜像 → 跳过过滤全量进 rerank。"""
        return self._tree is None or not self._index

    @property
    def last_refreshed_at(self) -> float | None:
        return self._refreshed_at

    @property
    def stale(self) -> bool:
        """TTL 到期后刷新失败、正在用旧树提供服务时为 True。"""
        return self._stale

    @property
    def last_error(self) -> str | None:
        return self._last_error

    async def ensure_fresh(self, *, force: bool = False) -> bool:
        """需要时刷新；返回是否实际发起了拉取。异常不外抛（记 last_error）。"""
        if not force and self._is_fresh():
            return False
        async with self._lock:
            # 双检：等锁期间可能已被别的协程刷新（单飞）
            if not force and self._is_fresh():
                return False
            try:
                await self._load()
            except Exception as e:  # best-effort：空镜像/旧树两条降级分支
                self._last_error = str(e) or repr(e)
                if self._tree is not None:
                    self._stale = True
                logger.warning(
                    "index_mirror refresh failed, serving %s",
                    "stale tree" if self._tree is not None else "empty mirror",
                    extra={"error": self._last_error},
                )
            return True

    async def refresh(self) -> IndexTree:
        """强制刷新；失败原样抛出（旧树保留并标 stale）。"""
        async with self._lock:
            await self._load()
        assert self._tree is not None
        return self._tree

    async def _load(self) -> None:
        tree = await self._reader.list_index_tree()
        self._tree = tree
        self._index = dict(_flatten_tree(tree))
        self._refreshed_at = self._clock()
        self._stale = False
        self._last_error = None

    def _is_fresh(self) -> bool:
        if self._tree is None or self._refreshed_at is None:
            return False
        return self._clock() - self._refreshed_at < self._ttl_sec

    # ---- 本地过滤原语（WP-10 meta_filter 降级路径消费；drop 判定在算子层） ----

    def meta(self, entry_id: str) -> IndexEntryMeta | None:
        return self._index.get(entry_id)

    def known_link_ids(self) -> list[str]:
        return sorted(
            {m.link_id for m in self._index.values() if m.kind == "link"}
        )

    def known_story_ids(self) -> list[str]:
        return sorted(
            {m.story_id for m in self._index.values() if m.story_id is not None}
        )

    def matches(
        self,
        entry_id: str,
        *,
        types: list[str] | None = None,
        link_ids: set[str] | None = None,
        story_ids: set[str] | None = None,
    ) -> tuple[bool, str | None]:
        """单个 entry_id 是否通过本地类型/归属过滤。

        返回 (是否保留, drop_reason)；drop_reason 取值对齐 dd §2.8
        （filtered_type / filtered_scope）。规则：
        - 镜像中无 meta：类型过滤无法判定→保留（不误杀）；归属过滤无法
          判定→剔除 filtered_scope（scope 是白名单，不在索引内即不在白名单）；
        - types 与 scope 同时给出时任一不通过即剔除，类型优先判定。
        """
        m = self._index.get(entry_id)
        if types is not None and m is not None:
            # 镜像只收录索引条目：meta 缺失说明该 entry 不是 link/story 索引节点，
            # 其类型判定不了（类型过滤由 WP-10 算子层用 Entry.entry_type 完成），
            # 不在镜像侧误杀。
            if m.entry_type.value not in types:
                return False, "filtered_type"
        if link_ids is not None or story_ids is not None:
            if m is None:
                return False, "filtered_scope"
            if link_ids is not None and m.link_id not in link_ids:
                return False, "filtered_scope"
            if story_ids is not None and m.story_id is not None and m.story_id not in story_ids:
                return False, "filtered_scope"
        return True, None


def _flatten_tree(tree: IndexTree) -> list[tuple[str, IndexEntryMeta]]:
    out: list[tuple[str, IndexEntryMeta]] = []
    for link in tree.links:
        out.append(
            (
                link.entry_id,
                IndexEntryMeta(
                    entry_id=link.entry_id,
                    entry_version=link.entry_version,
                    title=link.title,
                    summary=link.summary,
                    kind="link",
                    link_id=link.link_id,
                    story_id=None,
                ),
            )
        )
        for story in link.stories:
            out.append(
                (
                    story.entry_id,
                    IndexEntryMeta(
                        entry_id=story.entry_id,
                        entry_version=story.entry_version,
                        title=story.title,
                        summary=story.summary,
                        kind="story",
                        link_id=link.link_id,
                        story_id=story.story_id,
                    ),
                )
            )
    return out


# ---------- §9.1 工厂：按 (target, kb_id) 缓存实例 ----------

# async builder：kb_config dict（KbConfigIn 形态 {mode,target,kb_id,options}）→ reader。
# builder 内部负责连接与能力探测，返回的 reader 已带缓存好的 caps。
ReaderBuilder = Callable[[dict], Awaitable[ReMeReader]]


class CapsProbe(BaseModel):
    """连接测试结果（WP-25 KbTestOut 的 capabilities/latency 来源）。"""

    caps: ReMeCaps
    latency_ms: int


class ReMeReaderFactory:
    """reader 实例工厂（dd §9.1）。

    WP-08 只提供缓存/校验骨架与测试 builder 注册口；WP-09 通过 ``register``
    注册 sdk（及可能的 service）真实 builder。缓存键固定为 (target, kb_id)
    （dd §9.1），同键并发构造单飞；builder 抛错不落缓存，允许下次重试。
    """

    def __init__(self, builders: Mapping[str, ReaderBuilder] | None = None) -> None:
        self._builders: dict[str, ReaderBuilder] = dict(builders or {})
        self._cache: dict[tuple[str, str], ReMeReader] = {}
        self._locks: dict[tuple[str, str], asyncio.Lock] = {}

    def register(self, mode: str, builder: ReaderBuilder) -> None:
        """注册/替换 mode 的 builder（WP-09 接线用；测试也走此口注入替身）。"""
        self._builders[mode] = builder

    async def for_workspace(self, kb_config: dict) -> ReMeReader:
        mode, target, kb_id = _validate_kb_config(kb_config)
        key = (target, kb_id)
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            cached = self._cache.get(key)  # 双检
            if cached is not None:
                return cached
            builder = self._builders.get(mode)
            if builder is None:
                raise ValidationError(
                    f"知识库接入模式暂不可用: {mode!r}（真实适配在后续版本注册）",
                    details={"mode": mode},
                )
            reader = await builder(kb_config)
            self._cache[key] = reader
            return reader

    async def probe(self, kb_config: dict) -> CapsProbe:
        """一次性连接探测（dd §8.4：连接测试时探测），不写实例缓存。

        builder 成功构造即视为连通（探测在 builder 内完成）；失败原样抛出，
        由端点边界翻译为 KB_UNREACHABLE/424（WP-25）。
        """
        mode, _target, _kb_id = _validate_kb_config(kb_config)
        builder = self._builders.get(mode)
        if builder is None:
            raise ValidationError(
                f"知识库接入模式暂不可用: {mode!r}（真实适配在后续版本注册）",
                details={"mode": mode},
            )
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        reader = await builder(kb_config)
        latency_ms = int((loop.time() - t0) * 1000)
        return CapsProbe(caps=reader.caps, latency_ms=latency_ms)


def _validate_kb_config(kb_config: dict) -> tuple[str, str, str]:
    if not isinstance(kb_config, dict):
        raise ValidationError("kb_config 必须为对象", details={"got": type(kb_config).__name__})
    mode = kb_config.get("mode")
    target = kb_config.get("target")
    kb_id = kb_config.get("kb_id")
    missing = [
        name
        for name, val in (("mode", mode), ("target", target), ("kb_id", kb_id))
        if not isinstance(val, str) or not val.strip()
    ]
    if missing:
        raise ValidationError(
            "kb_config 缺少必填项或取值非法", details={"missing": missing}
        )
    assert isinstance(mode, str) and isinstance(target, str) and isinstance(kb_id, str)
    return mode, target, kb_id


# ---------- §9.2 ReMeWriter（只被 api/kb.py import；L3 图/运行时禁入） ----------


class WriteResult(BaseModel):
    """ReMe 写入回执（dd §9.2）。"""

    ok: bool
    remote_ref: str | None = None  # 写入后的条目 ID/版本
    verified: bool = False  # 是否经过写入后回查（S6）
    error_code: str | None = None


class ReMeWriter(Protocol):
    """知识库写入协议（dd §9.2）。

    结构红线（PRD 7 / tech-design §2）：仅 L2 确认端点（api/kb.py）持有本
    协议实例，AppContext/TaskContext 不设写字段，图运行代码路径 import 本
    协议即违反 import-linter 门禁（dd §15.2 场景 12，WP-28 测试覆盖）。
    """

    async def write_proposal(self, token: str, proposal: dict) -> WriteResult: ...


class UnavailableWriter:
    """默认写实现：WP-09 真实适配未注册前，写入一律不可达（502）。

    与 dd §9.2 的偏离（交接单登记）：§9.2 形参为 ``OneTimeToken``/``KbPayload``
    具名类型，v1 落为 ``str``/``dict``（令牌明文串与提案 payload），语义不变。
    """

    async def write_proposal(self, token: str, proposal: dict) -> WriteResult:
        raise KbUnreachable(
            "ReMe 写入适配未注册（真实适配在后续版本接入）",
            details={"reason": "writer_not_registered"},
        )
