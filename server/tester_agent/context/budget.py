"""分区预算与校验（spec §5/§10）。

每个 LLM 调用 profile 声明 P0/P1/P2 三区 token 预算；单分区超支时允许侵占
其他分区预算和的 10%（SPILL_RATE）。校验：任一分区非正、总量超过模型窗口
80% 一律拒绝（启动/首次组装期暴露配置错误）。
"""

from __future__ import annotations

import math

from pydantic import BaseModel, ConfigDict

from ..errors import ValidationError
from .models import ContextPartition, ProfileName

SPILL_RATE = 0.10
_WINDOW_USAGE = 0.8


class ProfileBudget(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    p0: int
    p1: int
    p2: int

    def total(self) -> int:
        return self.p0 + self.p1 + self.p2

    def of(self, partition: ContextPartition) -> int:
        if partition is ContextPartition.P0:
            return self.p0
        if partition is ContextPartition.P1:
            return self.p1
        return self.p2

    def capacity(self, partition: ContextPartition) -> int:
        """本区预算 + 其他分区预算和的 10% 侵占余量。"""
        own = self.of(partition)
        others = self.total() - own
        return own + math.floor(others * SPILL_RATE)


PROFILES: dict[ProfileName, ProfileBudget] = {
    ProfileName.CHAT: ProfileBudget(p0=1500, p1=3000, p2=8000),
    ProfileName.PLAN: ProfileBudget(p0=3000, p1=4000, p2=6000),
    ProfileName.EXECUTE: ProfileBudget(p0=3000, p1=8000, p2=6000),
    ProfileName.REFLECT: ProfileBudget(p0=2000, p1=2000, p2=4000),
    ProfileName.REVIEW: ProfileBudget(p0=2500, p1=8000, p2=8000),
    ProfileName.CASE_ITEM: ProfileBudget(p0=2000, p1=6000, p2=4000),
}


def validate_budget(budget: ProfileBudget, *, model_window: int) -> None:
    """非法预算抛 ValidationError（model_window 为模型上下文窗口 token 数）。"""
    if model_window <= 0:
        raise ValidationError("模型上下文窗口必须为正数", details={"model_window": model_window})
    for name in ("p0", "p1", "p2"):
        if getattr(budget, name) <= 0:
            raise ValidationError(
                f"分区预算必须为正数：{name}={getattr(budget, name)}",
                details={"partition": name},
            )
    if budget.total() > math.floor(model_window * _WINDOW_USAGE):
        raise ValidationError(
            "分区预算总和超过模型窗口的 80%",
            details={"total": budget.total(), "max": math.floor(model_window * _WINDOW_USAGE)},
        )
