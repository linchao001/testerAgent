"""prompts 包测试（dd §20.7）。"""

from __future__ import annotations

import pytest

from tester_agent.prompts import (
    PromptLoader,
    PromptTemplate,
    default_loader,
    load_prompt,
    prompt_version,
)


class TestPromptTemplate:
    def test_render(self):
        tpl = PromptTemplate(name="test", version="1", content="hello {name}", source="builtin")
        assert tpl.render(name="world") == "hello world"

    def test_render_missing_var(self):
        tpl = PromptTemplate(name="test", version="1", content="hello {name}", source="builtin")
        with pytest.raises(KeyError):
            tpl.render()


class TestBuiltinTemplates:
    """内置模板加载与版本读取。"""

    def test_system_shared(self):
        tpl = load_prompt("system.shared")
        assert tpl.version == "2026-09-26.1"
        assert "你是资深测试设计助手" in tpl.content
        assert "铁律" in tpl.content

    def test_intake_ambiguity(self):
        tpl = load_prompt("intake.ambiguity")
        assert tpl.version == "2026-09-26.1"
        assert "clarifications" in tpl.content

    def test_link_identify_main(self):
        tpl = load_prompt("link_identify.main")
        assert tpl.version == "2026-09-26.1"
        assert "链路" in tpl.content

    def test_point_write_main(self):
        tpl = load_prompt("point_write.main")
        assert tpl.version == "2026-09-26.1"
        assert "测试点" in tpl.content

    def test_case_generate_main(self):
        tpl = load_prompt("case_generate.main")
        assert tpl.version == "2026-09-26.1"
        assert "测试用例" in tpl.content

    def test_retrieve_multi_query(self):
        tpl = load_prompt("retrieve/multi_query")
        assert tpl.version == "2026-09-26.1"
        assert "keyword_queries" in tpl.content
        assert "rewrite_queries" in tpl.content
        assert "{intent}" in tpl.content
        assert "{want}" in tpl.content

    def test_retrieve_rerank(self):
        tpl = load_prompt("retrieve/rerank")
        assert tpl.version == "2026-09-26.1"
        assert "scores" in tpl.content
        assert "{intent}" in tpl.content
        assert "{entries}" in tpl.content

    def test_prompt_version(self):
        assert prompt_version("retrieve/multi_query") == "2026-09-26.1"
        assert prompt_version("retrieve/rerank") == "2026-09-26.1"

    def test_default_loader_singleton(self):
        a = default_loader()
        b = default_loader()
        assert a is b


class TestCustomOverride:
    """自定义目录覆盖内置目录（dd §20.7）。"""

    def test_custom_override_builtin(self, tmp_path):
        custom = tmp_path / "prompts"
        custom.mkdir()
        (custom / "retrieve").mkdir()
        (custom / "retrieve" / "multi_query.md").write_text(
            "---\nversion: \"2099-01-01.1\"\nnode: multi_query\n---\n自定义内容 {intent}",
            encoding="utf-8",
        )
        loader = PromptLoader(custom_dir=custom)
        tpl = loader.load("retrieve/multi_query")
        assert tpl.version == "2099-01-01.1"
        assert "自定义内容" in tpl.content
        assert tpl.source == str(custom / "retrieve")

    def test_custom_fallback_to_builtin(self, tmp_path):
        custom = tmp_path / "prompts"
        custom.mkdir()
        (custom / "retrieve").mkdir()
        # 自定义目录只覆盖 multi_query，rerank 仍走内置
        (custom / "retrieve" / "multi_query.md").write_text(
            "---\nversion: \"2099-01-01.1\"\nnode: multi_query\n---\n自定义",
            encoding="utf-8",
        )
        loader = PromptLoader(custom_dir=custom)
        mq = loader.load("retrieve/multi_query")
        rr = loader.load("retrieve/rerank")
        assert mq.version == "2099-01-01.1"
        assert rr.version == "2026-09-26.1"  # 内置版本

    def test_custom_dir_not_exists(self):
        loader = PromptLoader(custom_dir="/nonexistent/path")
        tpl = loader.load("retrieve/multi_query")
        assert tpl.version == "2026-09-26.1"  # 回退内置


class TestEdgeCases:
    def test_not_found(self):
        loader = PromptLoader()
        with pytest.raises(FileNotFoundError, match="prompt 模板不存在"):
            loader.load("nonexistent/template")

    def test_missing_yaml_header(self, tmp_path):
        custom = tmp_path / "prompts"
        custom.mkdir()
        (custom / "bad.md").write_text("没有文件头", encoding="utf-8")
        loader = PromptLoader(custom_dir=custom)
        with pytest.raises(ValueError, match="缺少 YAML 文件头"):
            loader.load("bad")

    def test_missing_version_field(self, tmp_path):
        custom = tmp_path / "prompts"
        custom.mkdir()
        (custom / "noversion.md").write_text(
            "---\nnode: test\n---\n正文", encoding="utf-8"
        )
        loader = PromptLoader(custom_dir=custom)
        with pytest.raises(ValueError, match="缺少 version 字段"):
            loader.load("noversion")

    def test_version_with_quotes(self, tmp_path):
        custom = tmp_path / "prompts"
        custom.mkdir()
        (custom / "quoted.md").write_text(
            "---\nversion: '2026-09-26.2'\nnode: test\n---\n正文",
            encoding="utf-8",
        )
        loader = PromptLoader(custom_dir=custom)
        tpl = loader.load("quoted")
        assert tpl.version == "2026-09-26.2"

    def test_cache(self):
        loader = PromptLoader()
        a = loader.load("retrieve/multi_query")
        b = loader.load("retrieve/multi_query")
        assert a is b
        loader.clear_cache()
        c = loader.load("retrieve/multi_query")
        assert a is not c

    def test_load_many(self):
        loader = PromptLoader()
        tpls = loader.load_many(["retrieve/multi_query", "retrieve/rerank"])
        assert len(tpls) == 2
        assert tpls["retrieve/multi_query"].version == "2026-09-26.1"
        assert tpls["retrieve/rerank"].version == "2026-09-26.1"


class TestInlinePromptVer:
    """pipeline.INLINE_PROMPT_VER 已换成 loader 版本。"""

    def test_inline_prompt_ver_matches_template(self):
        from tester_agent.graph.retrieval.pipeline import INLINE_PROMPT_VER
        assert INLINE_PROMPT_VER == "2026-09-26.1"
        assert INLINE_PROMPT_VER == prompt_version("retrieve/multi_query")
