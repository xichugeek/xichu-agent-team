"""配置加载：公开 config.json + 私密 config.local.json / secrets.local.json + 显式覆盖。"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent

DEFAULTS: dict[str, Any] = {
    "telegram": {
        "owner_user_id": 0,
        "group_chat_id": 0,
        "api_base": "https://api.telegram.org",
        "poll_timeout": 25,
        "proxy": None,
    },
    "data_dir": "runtime",
    "studio_root": "C:/Projects/YourStudio",
    "site_repo": "C:/Projects/YourWebsite",
    "site_template_episode": "018",
    "ffmpeg_path": "C:/Projects/YourStudio/tools/ffmpeg-9.0.2-essentials_build/bin/ffmpeg.exe",
    "ffprobe_path": "C:/Projects/YourStudio/tools/ffmpeg-9.0.2-essentials_build/bin/ffprobe.exe",
    "video_mode": "synthetic",
    "site_mode": "draft",
    "forge_root": "C:/Projects/YourWebsiteOperations",
    "tools_source_home": None,
    "youtube_online_check": True,
    "max_review_rounds": 1,
    "auto_review": False,
    "send_handoff_documents": False,
    "max_stage_attempts": 3,
    "duplicate_window_minutes": 1440,
    "heartbeat_minutes": 10,
    "lock_port": 47231,
    "simulate": [],
    # 群里的三个机器人（岗位）。pool 是账号池：按顺序优先使用，额度受限时切到下一个。
    "roles": {
        "pm": {"label": "项目经理(Codex)", "pool": ["codex_a", "codex_b"]},
        "video": {"label": "视频制作(Codex)", "pool": ["codex_a", "codex_b"]},
        "site": {"label": "官网运维(Codex)", "pool": ["codex_a", "codex_b"]},
    },
    "pm_triage": True,
    "quota_cooldown_minutes": 60,
    "progress_push_seconds": 120,
    "context_boot_max_chars": 18000,
    "stage_reasoning_effort": {"triage": "medium"},
    "force_quota_limited": [],
    "executors": {
        "codex_a": {"type": "codex", "label": "Codex 账号 A", "codex_home": None, "timeout_sec": 3600,
                    "cli": "codex", "model": None},
        "codex_b": {"type": "codex", "label": "Codex 账号 B", "codex_home": None, "timeout_sec": 3600,
                    "cli": "codex", "model": None},
        "codex_c": {"type": "codex", "label": "Codex 账号 C", "codex_home": "C:/CodexAccounts/C",
                    "timeout_sec": 3600, "cli": "auto:codex-app", "model": None},
    },
}


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
        """岗位 → bot token。兼容旧的单 token 配置（视为项目经理）。"""
        tokens = dict(self.raw.get("telegram_bot_tokens") or {})
        legacy = os.environ.get("XICHU_TG_BOT_TOKEN") or self.raw.get("telegram_bot_token")
        if legacy and not tokens.get("pm"):
            tokens["pm"] = legacy
        return {k: v for k, v in tokens.items() if v}

    def role(self, name: str) -> dict:
        return self.raw["roles"][name]

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

    @property
    def executor_names(self) -> list[str]:
        """只启用岗位账号池引用的执行器，同一账号在三个岗位间共用车道。"""
        return list(dict.fromkeys(name for role in self.raw["roles"].values() for name in role["pool"]
                                  if self.executor(name).get("enabled", True)))

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
    local_path = cfg_path.with_name(cfg_path.stem + ".local" + cfg_path.suffix)
    if local_path.is_file():
        data = _merge(data, json.loads(local_path.read_text(encoding="utf-8")))
    secrets_path = cfg_path.parent / "secrets.local.json"
    if secrets_path.is_file():
        data = _merge(data, json.loads(secrets_path.read_text(encoding="utf-8")))
    raw = _merge(DEFAULTS, data)
    if overrides:
        raw = _merge(raw, overrides)
    root = cfg_path.parent.resolve() if cfg_path.is_file() else ROOT
    return Config(raw=raw, root=root)
