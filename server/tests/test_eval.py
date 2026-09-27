"""WP-13 验收：eval smoke 管线（dd §15.3）。

覆盖验收口径"CLI 跑冒烟集输出指标对比"：
- fixture 加载（knowledge 5 类+link_index、requirements 落型/排序/预设/阶段过滤）；
- run_case 指标正确性（recall_hit/inject_hit/reference_rate/clause_coverage/cost），
  含幻觉引用与未覆盖条款的判别力；
- CLI：基线退出码 0；--config-diff 对比表；inject_hit 均值跌幅超阈值 → 退出码 1
  （dd §15.3 CI 阻断口径）；输入类错误 → 退出码 2；--stage 过滤；运行确定性。

fixture 期望的核算口径（FakeReMeReader 空白分词词面打分 + scope 服务端过滤）：
- 01_link：s2 词面零命中（未被召回）→ recall/inject 3/4；生成文本引用 s2 → 幻觉；
- 04_hallucinated：a2 期望外注入、b1/a2 未引用、x99 幻觉 → reference_rate 0.5。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pydantic import ValidationError as PydanticValidationError

from tester_agent.domain import EntryType
from tester_agent.errors import ValidationError
from tester_agent.eval.fixtures import FIXTURES_ROOT, load_cases, load_knowledge
from tester_agent.eval.run import _build_cfg, main
from tester_agent.eval.runner import run_case
from tester_agent.graph.constants import RETRIEVAL_PRESETS


# ---------- fixture 加载 ----------


def test_load_knowledge_entries():
    entries = load_knowledge()
    assert len(entries) == 14
    types = {e.entry_type for e in entries}
    assert types == {
        EntryType.LINK_INDEX,
        EntryType.BUSINESS,
        EntryType.FLOW_CASE,
        EntryType.DEFECT,
        EntryType.API,
        EntryType.DB,
    }
    by_id = {e.entry_id: e for e in entries}
    assert by_id["l1"].entry_version == "ver-l1"
    assert by_id["l1"].raw["summary"].startswith("订单链路")
    assert by_id["s2"].link_id == "L1" and by_id["s2"].story_id == "S2"
    assert by_id["db2"].entry_type is EntryType.DB


def test_load_cases_smoke_sorted_and_filter():
    cases = load_cases()
    assert [c.name for c in cases] == [
        "01_link",
        "02_point",
        "03_case",
        "04_hallucinated",
    ]
    only_point = load_cases(stage="point_write")
    assert [c.name for c in only_point] == ["02_point"]
    assert only_point[0].scope == {"link_ids": ["L1"]}


def test_load_cases_unknown_preset():
    with pytest.raises(ValidationError):
        load_cases(preset="nope")


def test_case_fixture_extra_key_forbidden(tmp_path):
    bad = tmp_path / "requirements"
    bad.mkdir()
    (bad / "x.json").write_text(
        '{"name":"x","stage":"link_identify","intent":"i","typo":1}', encoding="utf-8"
    )
    with pytest.raises(PydanticValidationError):
        load_cases(tmp_path)


def test_load_knowledge_missing_dir(tmp_path):
    with pytest.raises(ValidationError):
        load_knowledge(tmp_path)


# ---------- run_case 指标正确性 ----------


def _case(name: str):
    return next(c for c in load_cases() if c.name == name)


async def test_run_case_01_link_recall_miss_and_hallucinated():
    entries = load_knowledge()
    case = _case("01_link")
    report = await run_case(case, entries, RETRIEVAL_PRESETS[case.stage])

    # s2 词面零命中 → 期望 4 条只召回/注入 3 条
    assert report.recall_hit == pytest.approx(0.75)
    assert report.inject_hit == pytest.approx(0.75)
    assert set(report.injected) == {"l1", "l2", "s1"}
    # 生成文本引用了未注入的 s2 → 幻觉；注入的 3 条全被引用
    assert report.hallucinated == ["s2"]
    assert sorted(report.referenced) == ["l1", "l2", "s1"]
    assert report.reference_rate == pytest.approx(1.0)
    # 注入条目覆盖 c1/c2，c3 未覆盖
    assert report.clause_coverage == pytest.approx(2 / 3)
    # cost：multi_query + rerank 两次辅助调用（FakeLLM 固定 10+5），主调用一次 15
    assert report.aux_calls == 2
    assert report.aux_tokens == 30
    assert report.main_tokens == 15
    assert report.degraded_steps == 0
    assert report.wall_ms >= 0
    assert report.funnel["kept"] == 3
    assert report.funnel["total"] == 3


async def test_run_case_02_point_all_hit():
    entries = load_knowledge()
    case = _case("02_point")
    report = await run_case(case, entries, RETRIEVAL_PRESETS[case.stage])
    assert report.recall_hit == pytest.approx(1.0)
    assert report.inject_hit == pytest.approx(1.0)
    assert report.reference_rate == pytest.approx(1.0)
    assert report.clause_coverage == pytest.approx(1.0)
    assert report.hallucinated == []
    assert set(report.injected) == {"b1", "d1", "f1"}


async def test_run_case_04_reference_rate_and_injected_not_used():
    entries = load_knowledge()
    case = _case("04_hallucinated")
    report = await run_case(case, entries, RETRIEVAL_PRESETS[case.stage])
    assert report.recall_hit == pytest.approx(1.0)
    assert report.inject_hit == pytest.approx(1.0)
    # a2 被"接口"词面额外召回并注入（期望外但合法）；b1/a2 注入未引用、
    # x99 幻觉 → reference_rate = referenced{a1,db1} / injected 4 = 0.5
    assert set(report.injected) == {"a1", "b1", "db1", "a2"}
    assert sorted(report.referenced) == ["a1", "db1"]
    assert report.hallucinated == ["x99"]
    assert report.reference_rate == pytest.approx(0.5)
    assert report.clause_coverage == pytest.approx(1.0)


async def test_run_case_config_diff_query_paths_1_skips_multi_query():
    entries = load_knowledge()
    case = _case("01_link")
    cfg = _build_cfg(case.stage, {"query_paths": 1})
    report = await run_case(case, entries, cfg)
    # raw 路单独即可召回同一集合（本 fixture 口径），仅辅助调用次数下降
    assert report.aux_calls == 1
    assert report.aux_tokens == 15
    assert report.recall_hit == pytest.approx(0.75)


async def test_run_case_deterministic():
    entries = load_knowledge()
    case = _case("04_hallucinated")
    cfg = RETRIEVAL_PRESETS[case.stage]
    r1 = await run_case(case, entries, cfg)
    r2 = await run_case(case, entries, cfg)
    for col in ("recall_hit", "inject_hit", "reference_rate", "clause_coverage",
                "aux_calls", "aux_tokens", "main_tokens", "degraded_steps",
                "injected", "referenced", "hallucinated"):
        assert getattr(r1, col) == getattr(r2, col), col


# ---------- _build_cfg ----------


def test_build_cfg_diff_applies_and_validates():
    cfg = _build_cfg("point_write", {"query_paths": 2, "allowed_types": ["api", "db"]})
    assert cfg.stage == "point_write"
    assert cfg.query_paths == 2
    assert cfg.allowed_types == [EntryType.API, EntryType.DB]
    assert cfg.recall_topk == RETRIEVAL_PRESETS["point_write"].recall_topk
    with pytest.raises(PydanticValidationError):
        _build_cfg("point_write", {"inject_form": "bad-form"})


# ---------- CLI（验收口径：跑冒烟集输出指标对比） ----------


def test_cli_baseline_exit0(capsys):
    code = main(["--preset", "smoke"])
    out = capsys.readouterr().out
    assert code == 0
    for name in ("01_link", "02_point", "03_case", "04_hallucinated", "mean"):
        assert name in out
    for col in ("recall_hit", "inject_hit", "reference_rate", "clause_coverage"):
        assert col in out
    assert "0.750" in out  # 01_link recall/inject
    assert "verdict: OK" in out
    assert "note: base/01_link hallucinated=['s2']" in out


def test_cli_config_diff_regression_exit1(capsys):
    code = main(["--preset", "smoke", "--config-diff", '{"inject_limit": 1}'])
    out = capsys.readouterr().out
    assert code == 1
    assert "REGRESSION" in out
    assert "inject_hit mean Δ=" in out
    assert "0.750->0.250" in out  # 01_link inject_hit 基线→变体


def test_cli_config_diff_neutral_exit0(capsys):
    code = main(["--preset", "smoke", "--config-diff", '{"recall_topk": 45}'])
    out = capsys.readouterr().out
    assert code == 0
    assert "verdict: OK" in out
    assert "->" in out  # 对比模式


def test_cli_stage_filter(capsys):
    code = main(["--preset", "smoke", "--stage", "point_write"])
    out = capsys.readouterr().out
    assert code == 0
    assert "02_point" in out
    assert "01_link" not in out
    assert "cases=1" in out


def test_cli_unknown_preset_exit2(capsys):
    assert main(["--preset", "nope"]) == 2
    assert "未知 eval 预设" in capsys.readouterr().err


def test_cli_bad_diff_key_exit2(capsys):
    assert main(["--config-diff", '{"nope": 1}']) == 2
    assert "未知键" in capsys.readouterr().err


def test_cli_bad_diff_json_exit2(capsys):
    assert main(["--config-diff", "not-json"]) == 2
    assert "不是合法 JSON" in capsys.readouterr().err
