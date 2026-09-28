"""时间戳工具（与 store.db.utcnow_iso 同格式的层内实现）。

context 为叶子层不可 import store；UTC ISO-8601（毫秒，Z 后缀）口径必须与
store/db.py 一致，故在此按相同实现声明（偏离登记：未复用同一函数，跨层
import 违规代价更大）。
"""

from __future__ import annotations

from datetime import datetime, timezone


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
