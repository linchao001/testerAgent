"""环境配置（dd §13.1）。

只读进程环境变量，不使用全局单例：由组合根（main.py）构造一次后显式下发。
所有路径在构造时解析为绝对路径，避免工作目录漂移。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


@dataclass(frozen=True)
class Settings:
    """进程级配置，来源：环境变量（dd §13.1）。"""

    data_dir: Path
    host: str
    port: int
    log_level: str
    single_worker: bool

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> Settings:
        env = os.environ if environ is None else environ

        def get(name: str, default: str) -> str:
            return env.get(name, default)

        single_worker_raw = get("TESTER_AGENT_SINGLE_WORKER", "1").strip()
        return cls(
            data_dir=Path(get("TESTER_AGENT_DATA_DIR", "./data")).expanduser().resolve(),
            host=get("TESTER_AGENT_HOST", "127.0.0.1"),
            port=int(get("TESTER_AGENT_PORT", "8080")),
            log_level=get("TESTER_AGENT_LOG_LEVEL", "INFO").upper(),
            # 任何非 "1" 的取值（含 "0"、"true"）都视为约束被破坏，启动时拒绝（dd §13.1）
            single_worker=single_worker_raw == "1",
        )

    @property
    def app_db_path(self) -> Path:
        return self.data_dir / "app.db"

    @property
    def checkpoints_db_path(self) -> Path:
        return self.data_dir / "checkpoints.db"

    @property
    def workspaces_dir(self) -> Path:
        return self.data_dir / "workspaces"


def validate_single_worker(settings: Settings) -> None:
    """单 worker 守卫：TESTER_AGENT_SINGLE_WORKER != 1 时拒绝启动（dd §13.1）。

    SQLite + 文件系统双事实源依赖单进程写语义，多 worker 会破坏一致性协议（dd §11）。
    """
    if not settings.single_worker:
        raise RuntimeError(
            "拒绝启动：TESTER_AGENT_SINGLE_WORKER 必须为 1。"
            "本系统依赖单进程写语义（SQLite WAL + 文件系统），不支持多 worker 部署。"
        )
