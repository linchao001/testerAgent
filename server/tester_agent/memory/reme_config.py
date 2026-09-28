# -*- coding: utf-8 -*-
"""Embedded ReMe application config builder for TesterAgent memory."""

from __future__ import annotations

from typing import Any

_MAX_FILE_BYTES = 10 * 1024 * 1024


def build_reme_app_config(
    *,
    workspace_dir: str,
    kb_config: dict[str, Any],
    model_config: dict[str, Any] | None = None,
    language: str = "zh",
    timezone: str = "Asia/Shanghai",
) -> dict[str, Any]:
    """Build kwargs for ``reme.ReMe`` / Application from workspace kb_config."""
    options = dict(kb_config.get("options") or {})
    daily_dir = str(options.get("daily_dir") or "daily")
    digest_dir = str(options.get("digest_dir") or "digest")

    cfg = _base_config(daily_dir=daily_dir, digest_dir=digest_dir)
    cfg.update(
        {
            "workspace_dir": workspace_dir,
            "daily_dir": daily_dir,
            "digest_dir": digest_dir,
            "language": language,
            "timezone": timezone,
            "enable_logo": False,
            "log_to_console": True,
        }
    )
    _apply_knowledge_config(cfg, kb_config)
    _apply_model_placeholders(cfg, model_config)
    _apply_embedding_config(cfg, options.get("embedding") or {})
    return cfg


def _apply_knowledge_config(cfg: dict[str, Any], kb_config: dict[str, Any]) -> None:
    kb_id = (kb_config.get("kb_id") or "").strip()
    if not kb_id:
        return
    knowledge_dir = (kb_config.get("knowledge_dir") or "knowledge").strip() or "knowledge"
    cfg.update(
        {
            "knowledge_base_id": kb_id,
            "knowledge_bases_dir": (kb_config.get("knowledge_bases_dir") or "").strip(),
            "knowledge_dir": knowledge_dir,
            "knowledge_domain": kb_config.get("knowledge_domain", "business"),
            "create_knowledge_base": bool(kb_config.get("create_knowledge_base", False)),
        }
    )
    cfg.setdefault("jobs", {}).update(_knowledge_jobs())


def _knowledge_jobs() -> dict[str, Any]:
    return {
        "knowledge_search": {
            "backend": "base",
            "description": "Hybrid search scoped to the mounted shared knowledge base.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "limit": {"type": "integer", "default": 5},
                    "min_score": {"type": "number", "default": 0.0},
                    "bucket": {"type": "string", "default": "all"},
                },
                "required": ["query"],
            },
            "steps": [
                {
                    "backend": "knowledge_search_step",
                    "vector_weight": 0.7,
                    "candidate_multiplier": 5.0,
                    "expand_links": True,
                    "max_links_per_direction": 10,
                }
            ],
        },
        "save_to_knowledge": {
            "backend": "base",
            "description": "Write or refine one published node in the shared KB.",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "content": {"type": "string"},
                    "bucket": {"type": "string", "default": "business/wiki"},
                },
                "required": ["title", "content"],
            },
            "steps": [{"backend": "save_to_knowledge_step"}],
        },
    }


def _base_config(*, daily_dir: str, digest_dir: str) -> dict[str, Any]:
    watch_dirs = ["daily_dir", "digest_dir"]
    return {
        "service": {"backend": "http"},
        "jobs": {
            "index_update_loop": {
                "backend": "background",
                "max_file_bytes": _MAX_FILE_BYTES,
                "watch_dirs": watch_dirs,
                "watch_suffixes": ["md"],
                "steps": [
                    {
                        "backend": "init_changes_step",
                        "monitor_type": "file_store",
                        "monitor_name": "default",
                        "dispatch_steps": ["update_index_step"],
                    },
                    {
                        "backend": "watch_changes_step",
                        "dispatch_steps": [
                            {"backend": "update_index_step", "persist": False},
                        ],
                    },
                ],
            },
            "version": {
                "backend": "base",
                "description": "return reme package version",
                "parameters": {"type": "object", "properties": {}},
                "steps": [{"backend": "version_step"}],
            },
            "status": {
                "backend": "base",
                "description": "report memory estimates",
                "parameters": {"type": "object", "properties": {}},
                "steps": [{"backend": "status_step"}],
            },
            "search": {
                "backend": "base",
                "description": "Hybrid workspace search (vector + BM25).",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "limit": {"type": "integer", "default": 5},
                        "min_score": {"type": "number", "default": 0.0},
                    },
                    "required": ["query"],
                },
                "steps": [
                    {
                        "backend": "search_step",
                        "vector_weight": 0.7,
                        "candidate_multiplier": 3.0,
                        "expand_links": True,
                        "max_links_per_direction": 10,
                    }
                ],
            },
            "list": {
                "backend": "base",
                "description": "List files under a vault path.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "default": ""},
                        "recursive": {"type": "boolean", "default": False},
                        "limit": {"type": "integer", "default": 100},
                    },
                },
                "steps": [{"backend": "list_step"}],
            },
            "read": {
                "backend": "base",
                "description": "Read a markdown file under the vault.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "start_line": {"type": "integer"},
                        "end_line": {"type": "integer"},
                    },
                    "required": ["path"],
                },
                "steps": [{"backend": "read_step"}],
            },
            "frontmatter_read": {
                "backend": "base",
                "description": "Read a file's frontmatter.",
                "parameters": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                },
                "steps": [{"backend": "frontmatter_read_step"}],
            },
            "auto_memory": {
                "backend": "base",
                "description": "Record conversation facts into a daily note",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "messages": {
                            "type": "array",
                            "items": {"type": "object"},
                        },
                        "session_id": {"type": "string", "default": ""},
                        "memory_hint": {"type": "string"},
                    },
                    "required": ["messages"],
                },
                "steps": [{"backend": "auto_memory_step"}],
            },
        },
        "components": _base_components(),
    }


def _base_components() -> dict[str, Any]:
    return {
        "tokenizer": {"default": {"backend": "regex"}},
        "as_llm": {
            "default": {
                "backend": "openai",
                "model": "testeragent-injected",
                "stream": True,
                "context_size": 200000,
                "max_retries": 3,
                "credential": {"api_key": "", "base_url": ""},
                "parameters": {"max_tokens": 65536, "thinking_enable": False},
            }
        },
        "file_graph": {"default": {"backend": "local"}},
        "file_catalog": {
            "default": {"backend": "local"},
            "digest": {"backend": "local"},
        },
        "file_chunker": {
            "markdown": {
                "backend": "markdown",
                "supported_extensions": ["md"],
            },
            "default": {
                "backend": "default",
                "supported_extensions": ["jsonl"],
            },
        },
        "keyword_index": {
            "default": {"backend": "bm25", "tokenizer": "default"},
        },
        "as_embedding": {
            "default": {
                "backend": "openai",
                "model": "",
                "dimensions": 1024,
                "credential": {"api_key": "", "base_url": ""},
                "parameters": {},
            }
        },
        "embedding_store": {
            "default": {
                "backend": "local",
                "as_embedding": "default",
                "enable_cache": True,
                "max_cache_size": 3000,
                "max_input_length": 8192,
                "max_batch_size": 10,
            }
        },
        "file_store": {
            "default": {
                "backend": "local",
                "store_name": "local",
                "embedding_store": "default",
                "keyword_index": "default",
                "file_graph": "default",
            }
        },
    }


def _apply_model_placeholders(
    cfg: dict[str, Any], model_config: dict[str, Any] | None
) -> None:
    if not model_config:
        return
    llm = cfg["components"]["as_llm"]["default"]
    if model_config.get("model"):
        llm["model"] = model_config["model"]
    cred = llm.setdefault("credential", {})
    if model_config.get("api_key"):
        cred["api_key"] = model_config["api_key"]
    if model_config.get("base_url"):
        cred["base_url"] = model_config["base_url"]


def _apply_embedding_config(cfg: dict[str, Any], embedding: dict[str, Any]) -> None:
    model_name = str(embedding.get("model_name") or "").strip()
    api_key = str(embedding.get("api_key") or "").strip()
    components = cfg["components"]
    if not model_name or not api_key:
        components["file_store"]["default"]["embedding_store"] = ""
        components.pop("embedding_store", None)
        components.pop("as_embedding", None)
        return
    emb = components["as_embedding"]["default"]
    emb["backend"] = embedding.get("backend") or "openai"
    emb["model"] = model_name
    emb["dimensions"] = int(embedding.get("dimensions") or 1024)
    cred = {"api_key": api_key}
    base_url = str(embedding.get("base_url") or "").strip()
    if base_url:
        cred["base_url"] = base_url
    emb["credential"] = cred
