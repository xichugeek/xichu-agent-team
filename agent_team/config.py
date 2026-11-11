"""配置加载：config.json（可提交）+ secrets.local.json（私密，不提交）。"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent

DEFAULTS: dict[str, Any] = {
    "telegram": {
        "owner_user_id": 0,       # 你本人的 Telegram 用户 ID（运行 discover.bat 获取）
        "group_chat_id": 0,       # 私人群的 ID（负数，运行 discover.bat 获取）
        "api_base": "https://api.telegram.org",
        "poll_timeout": 25,
        "proxy": None,
    },
    "data_dir": "runtime",
    "max_review_rounds": 1,
    "max_stage_attempts": 3,
    "duplicate_window_minutes": 1440,
    "heartbeat_minutes": 10,
    "lock_port": 47231,
    "simulate": [],
    # 群里的机器人（岗位）。pm 是项目经理，必须保留；其余是员工，可以增删改名。
    "roles": {
        "pm": {"label": "项目经理(Claude)", "pool": ["claude"], "aliases": ["项目经理"]},
        "writer": {"label": "写手(Codex)", "pool": ["codex"], "aliases": ["写手"],
                   "duty": "写文档、文章、周报、方案、说明书等文字草稿", "prompt": "work_writer.md"},
        "coder": {"label": "程序员(Codex)", "pool": ["codex"], "aliases": ["程序员"],
                  "duty": "在指定的项目目录里写代码、改 bug、跑测试", "prompt": "work_coder.md",
                  "project_dir": None},
    },
    "quota_cooldown_minutes": 60,
    "progress_push_seconds": 120,
    "force_quota_limited": [],
    "executors": {
        "codex": {"type": "codex", "label": "Codex（ChatGPT 订阅）", "codex_home": None, "timeout_sec": 3600,
                  "cli": "codex", "model": None, "windows_sandbox": "elevated"},
        "claude": {"type": "claude", "label": "Claude Code（订阅）", "timeout_sec": 1800,
                   "cli": "claude", "model": None, "max_turns": 40},
    },
}

# 这些键在用户配置里出现时整体替换默认值（方便删掉不需要的岗位）
REPLACE_KEYS = ("roles", "executors")


def _merge(base: dict, extra: dict) -> dict:
    out = dict(base)
    for key, value in (extra or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


@dataclass
class Config:
    raw: dict
    root: Path = field(default=ROOT)

    @property
    def tg(self) -> dict:
        return self.raw["telegram"]

    @property
    def bot_token(self) -> str:
        return self.bot_tokens.get("pm", "")

    @property
    def bot_tokens(self) -> dict:
        """岗位 → bot token（来自 secrets.local.json 的 telegram_bot_tokens）。"""
        tokens = dict(self.raw.get("telegram_bot_tokens") or {})
        return {k: v for k, v in tokens.items() if v and not str(v).startswith("在这里")}

    def role(self, name: str) -> dict:
        return self.raw["roles"][name]

    @property
    def workers(self) -> list[str]:
        """员工岗位（除项目经理外）。"""
        return [r for r in self.raw["roles"] if r != "pm"]

    @property
    def data_dir(self) -> Path:
        path = Path(self.raw["data_dir"])
        return path if path.is_absolute() else self.root / path

    @property
    def db_path(self) -> Path:
        return self.data_dir / "agent.db"

    @property
    def work_dir(self) -> Path:
        return self.data_dir / "work"

    def executor(self, name: str) -> dict:
        return self.raw["executors"][name]

    def is_simulated(self, name: str) -> bool:
        sim = self.raw.get("simulate") or []
        return sim == "all" or "all" in sim or name in sim

    def get(self, key: str, default=None):
        return self.raw.get(key, default)


def load(path: str | os.PathLike | None = None, overrides: dict | None = None) -> Config:
    cfg_path = Path(path) if path else ROOT / "config.json"
    data: dict = {}
    if cfg_path.is_file():
        data = json.loads(cfg_path.read_text(encoding="utf-8"))
    secrets_path = cfg_path.parent / "secrets.local.json"
    if secrets_path.is_file():
        data = _merge(data, json.loads(secrets_path.read_text(encoding="utf-8")))
    if overrides:
        data = _merge(data, overrides)
    raw = _merge(DEFAULTS, {k: v for k, v in data.items() if k not in REPLACE_KEYS})
    for key in REPLACE_KEYS:
        if key in data:
            raw[key] = data[key]
    if "pm" not in raw["roles"]:
        raise ValueError("config.json 的 roles 里必须保留 pm（项目经理）")
    root = cfg_path.parent.resolve() if cfg_path.is_file() else ROOT
    return Config(raw=raw, root=root)
