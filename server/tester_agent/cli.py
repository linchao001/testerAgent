"""CLI 框架（dd §19.3/§19.5）。

子命令：
  init-db   迁移 + 内置 agent 种子 + config 单行（WP-02 接线）
  reap      手动触发 Reaper/对账（WP-23/WP-29 接线，排障用）
  check     DB/目录/依赖体检
  backup    两 DB 在线备份 + workspaces/ 打包（WP-X2 / dd §19.5）
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path

from .logging_config import setup_logging
from .settings import Settings

_REQUIRED_DEPS = ("fastapi", "uvicorn", "langgraph", "pydantic", "httpx", "mistune", "json_repair", "yaml")


def _not_implemented(name: str, wp: str) -> int:
    print(f"cli `{name}` 尚未接线（由 {wp} 实现），本次为骨架占位。", file=sys.stderr)
    return 2


def cmd_init_db(settings: Settings, _args: argparse.Namespace) -> int:
    # 迁移 + 幂等种子（dd §3.2 §19.4）
    from .store.db import run_migrations, seed_defaults

    settings.data_dir.mkdir(parents=True, exist_ok=True)
    migrated = run_migrations(settings.app_db_path)
    seeded = seed_defaults(settings.app_db_path)
    print(
        json.dumps(
            {"ok": True, "db": str(settings.app_db_path), "migrated": migrated, "seeded": seeded},
            ensure_ascii=False,
        )
    )
    return 0


def cmd_reap(_settings: Settings, _args: argparse.Namespace) -> int:
    # WP-23/WP-29：手动触发 Reaper 与 Reconciler
    return _not_implemented("reap", "WP-23")


def cmd_backup(settings: Settings, args: argparse.Namespace) -> int:
    """两 DB 在线备份 + workspaces/ 打包（dd §19.5）。"""
    from .store.backup import run_backup

    out_dir = Path(args.out_dir).expanduser()
    report = run_backup(data_dir=settings.data_dir, out_dir=out_dir)
    print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    return 0 if report.ok else 1


def cmd_check(settings: Settings, _args: argparse.Namespace) -> int:
    report: dict = {"python_ok": sys.version_info >= (3, 11), "deps": {}, "data_dir": str(settings.data_dir)}

    for mod in _REQUIRED_DEPS:
        report["deps"][mod] = importlib.util.find_spec(mod) is not None

    try:
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        probe = settings.data_dir / ".check_probe"
        probe.write_text("ok")
        probe.unlink()
        report["data_dir_writable"] = True
    except OSError as exc:
        report["data_dir_writable"] = False
        report["data_dir_error"] = str(exc)

    try:
        from .tools.shell_backend import detect_shell_backend

        kind, argv = detect_shell_backend("auto")
        report["shell_backend"] = {"kind": kind, "argv0": argv[0], "ok": True}
        shell_ok = True
    except Exception as exc:  # noqa: BLE001 — surface any detection failure
        report["shell_backend"] = {"ok": False, "error": str(exc)}
        shell_ok = False

    deps_ok = all(report["deps"].values())
    ok = report["python_ok"] and deps_ok and report["data_dir_writable"] and shell_ok
    report["ok"] = ok
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tester_agent", description="TesterAgent 运维 CLI")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init-db")
    sub.add_parser("reap")
    backup_p = sub.add_parser("backup", help="SQLite 在线备份 + workspaces/ 打包")
    backup_p.add_argument(
        "out_dir",
        type=str,
        help="备份输出目录（将写入 app.db / checkpoints.db / workspaces/）",
    )
    sub.add_parser("check")
    args = parser.parse_args(argv)

    settings = Settings.from_env()
    setup_logging(settings.log_level)

    handlers = {
        "init-db": cmd_init_db,
        "reap": cmd_reap,
        "backup": cmd_backup,
        "check": cmd_check,
    }
    return handlers[args.command](settings, args)


if __name__ == "__main__":
    raise SystemExit(main())
