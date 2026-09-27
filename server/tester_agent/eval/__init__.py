"""eval 冒烟管线（WP-13，dd §15.3）。

- fixtures：knowledge 假知识库 + requirements 构造需求/期望产物的加载与落型；
- runner：单用例执行（retrieve_pipeline + 脚本化生成 + 引用闭环）与指标计算；
- run：CLI 入口（``python -m tester_agent.eval.run --preset smoke``）。
"""

from .fixtures import (
    FIXTURES_ROOT,
    PRESETS,
    EvalCase,
    EntrySpec,
    Expected,
    LLMScript,
    MultiQueryScript,
    load_cases,
    load_knowledge,
)
from .runner import CaseReport, run_case

__all__ = [
    "FIXTURES_ROOT",
    "PRESETS",
    "CaseReport",
    "EntrySpec",
    "EvalCase",
    "Expected",
    "LLMScript",
    "MultiQueryScript",
    "load_cases",
    "load_knowledge",
    "run_case",
]
