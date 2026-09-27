"""模型配置路由（WP-25 API-A；tech-design §5.6 / dd §10.2 §13.2）。

- GET/PUT /config/model：平台全局模型配置（config 表单行 id=1 的
  model_config JSON）；本地服务不设鉴权（tech-design §6 运维约定，单机
  单用户），api_key 原样回显；
- POST /config/model/test：探活=一次最小调用（tech-design §5.6"PUT 后可
  POST test"）。每次探活按**已保存**配置现场构造客户端（验证的是落库配置
  而非进程启动配置），成功返回延迟与 model；失败按 AppError 类属性映射
  （LLM_BAD_REQUEST 400 / RATE_LIMITED 429 / LLM_UPSTREAM 502 /
  LLM_TIMEOUT 504，dd §17.1 为 HTTP 映射唯一来源）。
"""

from __future__ import annotations

import time

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from ..adapters.llm import OpenAICompatLLMClient
from ..store.models import ConfigDAO

router = APIRouter(prefix="/api/v1", tags=["config"])


# ---- 请求/响应模型（dd §10.2 config 段） ----


class ModelConfigIn(BaseModel):
    base_url: str
    api_key: str
    model: str
    temperature: float = 0.2
    top_p: float = 1.0
    timeout: int = 120


class ModelConfigOut(ModelConfigIn):
    """GET 用：未配置项回默认值而非 4xx（引导页需要先读后写）。"""

    base_url: str = ""
    api_key: str = ""
    model: str = ""


class ModelTestOut(BaseModel):
    ok: bool
    latency_ms: int
    model: str | None = None
    error_code: str | None = None


def _config_dao(request: Request) -> ConfigDAO:
    return ConfigDAO(request.app.state.db)


@router.get("/config/model")
async def get_model_config(request: Request) -> ModelConfigOut:
    cfg = (await _config_dao(request).get()).model_dict()
    return ModelConfigOut(
        base_url=cfg.get("base_url", ""),
        api_key=cfg.get("api_key", ""),
        model=cfg.get("model", ""),
        temperature=cfg.get("temperature", 0.2),
        top_p=cfg.get("top_p", 1.0),
        timeout=cfg.get("timeout", 120),
    )


@router.put("/config/model")
async def put_model_config(body: ModelConfigIn, request: Request) -> ModelConfigOut:
    await _config_dao(request).update_model(body.model_dump())
    return ModelConfigOut(**body.model_dump())


def _build_client(model_config: dict, runtime_config: dict):
    """探活客户端构造点（测试经 monkeypatch 注入 FakeLLM）。"""
    return OpenAICompatLLMClient.from_configs(model_config, runtime_config)


@router.post("/config/model/test")
async def test_model_config(request: Request) -> ModelTestOut:
    cfg_row = await _config_dao(request).get()
    client = _build_client(cfg_row.model_dict(), cfg_row.runtime_dict())
    t0 = time.perf_counter()
    try:
        result = await client.chat([{"role": "user", "content": "ping"}])
    finally:
        aclose = getattr(client, "aclose", None)
        if aclose is not None:
            await aclose()
    return ModelTestOut(
        ok=True,
        latency_ms=int((time.perf_counter() - t0) * 1000),
        model=result.model,
        error_code=None,
    )
