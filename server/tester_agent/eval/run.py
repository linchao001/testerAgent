"""eval smoke CLI（WP-13，dd §15.3）。

用法（server/ 目录下）：

    python -m tester_agent.eval.run --preset smoke
    python -m tester_agent.eval.run --preset smoke --config-diff '{"query_paths":1}'
    python -m tester_agent.eval.run --preset smoke --stage point_write

不带 --config-diff：跑一遍基线并输出指标表，退出码恒 0。
带 --config-diff：基线/变体各跑一遍，输出对比表；mean inject_hit 跌幅超过
阈值（默认 -0.05 即 -5pt，dd §15.3 暂定口径）时退出码 1（CI 阻断），否则 0。
输入类错误（坏 JSON/未知键/未知预设/无用例）退出码 2。

与 dd §15.3 的偏离（交接单登记）：文件落于 ``server/tester_agent/eval/`` 包内
（WP-02 同款落点先例，dd 文字路径为 server/eval/run.py）；
clause_coverage 以 fixture 的 entry→clauses 映射计算（覆盖矩阵 WP-20 落地前
的冒烟期代理口径）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from pydantic import ValidationError as PydanticValidationError

from ..domain import RetrievalConfig
from ..errors import ValidationError
from ..graph.constants import RETRIEVAL_PRESETS
from .fixtures import FIXTURES_ROOT, EvalCase, load_cases, load_knowledge
from .runner import CaseReport, run_case

# config-diff 允许覆盖的 RetrievalConfig 字段（stage/batch_unit_id 不可覆盖：
# stage 由用例决定，batch_unit_id 与检索冒烟无关）
_RETRIEVAL_DIFFABLE = {
    "query_paths",
    "recall_topk",
    "inject_limit",
    "inject_form",
    "allowed_types",
    "token_budget",
}
# WP-32：上下文开关（不进入 RetrievalConfig；对比时检索面字节不变，护栏看
# reference_rate / clause_coverage 不劣化）
_CONTEXT_DIFFABLE = {
    "context.enabled",
}
_DIFFABLE = _RETRIEVAL_DIFFABLE | _CONTEXT_DIFFABLE

_RATIO_COLS = ("recall_hit", "inject_hit", "reference_rate", "clause_coverage")
_INT_COLS = ("aux_tokens", "main_tokens", "wall_ms", "degraded_steps")
_HEADERS = [
    "case",
    "recall_hit",
    "inject_hit",
    "reference_rate",
    "clause_coverage",
    "aux_tok",
    "main_tok",
    "wall_ms",
    "degraded",
]


def _parse_diff(raw: str | None) -> dict | None:
    if raw is None:
        return None
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValidationError("--config-diff 不是合法 JSON", details={"error": str(e)}) from e
    if not isinstance(obj, dict) or not obj:
        raise ValidationError("--config-diff 必须为非空 JSON 对象", details={"got": obj})
    unknown = sorted(set(obj) - _DIFFABLE)
    if unknown:
        raise ValidationError(
            "--config-diff 含未知键",
            details={"unknown": unknown, "allowed": sorted(_DIFFABLE)},
        )
    return obj


def _build_cfg(stage: str, diff: dict | None) -> RetrievalConfig:
    """按阶段取 §8.1 预设并套用 config-diff（经模型校验，含 allowed_types 落型）。

    ``context.*`` 键仅作对照标记，不写入 RetrievalConfig。
    """
    cfg = RETRIEVAL_PRESETS[stage]
    if not diff:
        return cfg
    retrieval_diff = {k: v for k, v in diff.items() if k in _RETRIEVAL_DIFFABLE}
    if not retrieval_diff:
        return cfg
    data = cfg.model_dump()
    data.update(retrieval_diff)
    return RetrievalConfig.model_validate(data)


async def _run_all(
    cases: list[EvalCase], entries, cfgs: dict[str, RetrievalConfig]
) -> list[CaseReport]:
    reports: list[CaseReport] = []
    for case in cases:  # 顺序执行，wall_ms 不受用例间并发干扰
        reports.append(await run_case(case, entries, cfgs[case.name]))
    return reports


# ---------- 指标表渲染 ----------


def _fmt_ratio(v: float | None) -> str:
    return "-" if v is None else f"{v:.3f}"


def _mean(values: list[float | None]) -> float | None:
    vals = [v for v in values if v is not None]
    return None if not vals else sum(vals) / len(vals)


def _report_cells(r: CaseReport) -> list[str]:
    return [
        r.name,
        *[_fmt_ratio(getattr(r, c)) for c in _RATIO_COLS],
        str(r.aux_tokens),
        str(r.main_tokens),
        str(r.wall_ms),
        str(r.degraded_steps),
    ]


def _mean_row(label: str, reports: list[CaseReport]) -> list[str]:
    cells = [label]
    for col in _RATIO_COLS:
        cells.append(_fmt_ratio(_mean([getattr(r, col) for r in reports])))
    for col in _INT_COLS:
        mean_v = _mean([float(getattr(r, col)) for r in reports])
        cells.append("-" if mean_v is None else str(round(mean_v)))
    return cells


def _render(
    *,
    preset: str,
    stage: str | None,
    fixtures_root: Path,
    diff: dict | None,
    threshold: float,
    base: list[CaseReport],
    variant: list[CaseReport] | None,
) -> str:
    lines = [
        f"eval preset={preset} stage={stage or 'all'} cases={len(base)} "
        f"fixtures={fixtures_root}",
        f"config_diff={json.dumps(diff, ensure_ascii=False, sort_keys=True) if diff else '-'} "
        f"threshold_inject_hit={threshold * 100:.1f}pt",
    ]

    rows: list[list[str]] = []
    if variant is None:
        rows = [_report_cells(r) for r in base]
        rows.append(_mean_row("mean", base))
    else:
        for rb, rv in zip(base, variant):
            cb, cv = _report_cells(rb), _report_cells(rv)
            rows.append([cb[0], *[f"{b}->{v}" for b, v in zip(cb[1:], cv[1:])]])
        mb, mv = _mean_row("mean", base), _mean_row("mean", variant)
        rows.append([mb[0], *[f"{b}->{v}" for b, v in zip(mb[1:], mv[1:])]])

    widths = [len(h) for h in _HEADERS]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))
    lines.append("  ".join(h.ljust(widths[i]) for i, h in enumerate(_HEADERS)))
    for row in rows:
        lines.append("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)))

    for label, group in (("base", base), ("diff", variant)):
        if group is None:
            continue
        for r in group:
            if r.hallucinated:
                lines.append(f"note: {label}/{r.name} hallucinated={r.hallucinated}")
    return "\n".join(lines)


def _verdict(
    base: list[CaseReport],
    variant: list[CaseReport],
    threshold: float,
    *,
    diff: dict | None = None,
) -> tuple[str, int]:
    """mean inject_hit 跌幅超阈值 → REGRESSION + 退出码 1（dd §15.3 CI 阻断口径）。

    WP-32：含 ``context.enabled`` 的 config-diff 额外校验——
    reference_rate 均值下降 ≤5%、clause_coverage 均值不降。
    """
    notes: list[str] = []
    mb = _mean([r.inject_hit for r in base])
    mv = _mean([r.inject_hit for r in variant])
    if mb is None or mv is None:
        notes.append("inject_hit 无有效样本，跳过阈值判定")
        code = 0
    else:
        delta = mv - mb
        if delta < threshold:
            return (
                f"verdict: REGRESSION inject_hit mean Δ={delta * 100:+.1f}pt "
                f"(< {threshold * 100:.1f}pt)",
                1,
            )
        notes.append(f"inject_hit mean Δ={delta * 100:+.1f}pt")
        code = 0

    if diff and "context.enabled" in diff:
        rb = _mean([r.reference_rate for r in base])
        rv = _mean([r.reference_rate for r in variant])
        cb = _mean([r.clause_coverage for r in base])
        cv = _mean([r.clause_coverage for r in variant])
        if rb is not None and rv is not None and (rv - rb) < -0.05:
            return (
                f"verdict: REGRESSION reference_rate mean Δ={(rv - rb) * 100:+.1f}pt "
                f"(context.enabled 护栏 ≤-5pt)",
                1,
            )
        if cb is not None and cv is not None and cv < cb - 1e-12:
            return (
                f"verdict: REGRESSION clause_coverage mean {cb:.3f}->{cv:.3f} "
                f"(context.enabled 护栏禁止下降)",
                1,
            )
        if rb is not None and rv is not None:
            notes.append(f"reference_rate mean Δ={(rv - rb) * 100:+.1f}pt")
        if cb is not None and cv is not None:
            notes.append(f"clause_coverage mean {cb:.3f}->{cv:.3f}")

    return f"verdict: OK ({'; '.join(notes)})", code


# ---------- CLI ----------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="tester_agent.eval", description="eval 冒烟集与回归（dd §15.3）"
    )
    parser.add_argument("--preset", default="smoke", help="用例预设（当前仅 smoke）")
    parser.add_argument(
        "--stage", choices=sorted(RETRIEVAL_PRESETS), default=None, help="只跑指定阶段"
    )
    parser.add_argument("--config-diff", default=None, help="检索配置覆盖 JSON 对象")
    parser.add_argument("--fixtures-root", type=Path, default=FIXTURES_ROOT)
    parser.add_argument(
        "--threshold-inject-hit",
        type=float,
        default=-0.05,
        help="inject_hit 均值跌幅阈值（小数，默认 -0.05 即 -5pt）",
    )
    args = parser.parse_args(argv)

    try:
        diff = _parse_diff(args.config_diff)
        entries = load_knowledge(args.fixtures_root)
        cases = load_cases(args.fixtures_root, preset=args.preset, stage=args.stage)
        if not cases:
            raise ValidationError(
                "无匹配用例",
                details={"preset": args.preset, "stage": args.stage},
            )
        base_cfgs = {c.name: _build_cfg(c.stage, None) for c in cases}
        diff_cfgs = {c.name: _build_cfg(c.stage, diff) for c in cases} if diff else None
    except (ValidationError, PydanticValidationError) as e:
        print(f"eval 输入错误: {e}", file=sys.stderr)
        return 2

    base = asyncio.run(_run_all(cases, entries, base_cfgs))
    variant = asyncio.run(_run_all(cases, entries, diff_cfgs)) if diff_cfgs else None

    print(
        _render(
            preset=args.preset,
            stage=args.stage,
            fixtures_root=args.fixtures_root,
            diff=diff,
            threshold=args.threshold_inject_hit,
            base=base,
            variant=variant,
        )
    )
    if variant is None:
        print("verdict: OK（单次基线运行，无对比）")
        return 0
    verdict, code = _verdict(base, variant, args.threshold_inject_hit, diff=diff)
    print(verdict)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
