"""Explicit per-account resets, with a durable idempotency journal and no automatic spend."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import threading
import uuid
from pathlib import Path

from .codex_quota import ResetRejected, consume_reset, stamp

MESSAGES = {"reset": "已使用 1 次重置机会，额度已重新读取。",
            "alreadyRedeemed": "这次重置此前已完成，已确认结果并重新读取额度。",
            "nothingToReset": "官方返回：当前没有符合条件的额度窗口需要重置。",
            "noCredit": "官方返回：此账号当前没有可用重置机会。"}


def home_identity(config):
    return hashlib.sha256(str(Path(config["codex_home"]).resolve()).casefold().encode()).hexdigest()


class ResetManager:
    def __init__(self, path: Path, cache, consumer=consume_reset):
        self.path, self.cache, self.consumer = path, cache, consumer
        self.lock = threading.Lock()
        self.busy = set()
        self.records = {}
        self.error = None
        try:
            if path.exists():
                records = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(records, dict) or any(not isinstance(v, dict) for v in records.values()):
                    raise ValueError("Invalid reset journal")
                self.records = records
                for r in self.records.values():
                    if r.get("status") == "running":
                        r.update(status="unknown", message="上次重置结果待确认，请沿用同一次操作重试。")
        except (OSError, ValueError):
            self.error = "重置操作记录无法读取，已停用重置按钮，请先检查本地记录文件。"

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(".tmp")
        temp.write_text(json.dumps(self.records, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temp, self.path)

    def state(self, account: str) -> dict | None:
        with self.lock:
            if self.error:
                return {"status": "unavailable", "message": self.error}
            rows = [r for r in self.records.values() if r.get("account") == account]
            pending = [r for r in rows if r.get("status") in {"running", "unknown"}]
            latest = max(pending or rows, key=lambda r: r.get("updated_at", ""), default=None)
            return self.public(latest) if latest else None

    @staticmethod
    def public(record):
        return {k: record.get(k) for k in ("account", "email", "credit_id", "idempotency_key", "status", "outcome", "message", "updated_at")}

    def start(self, account: str, config: dict, payload: dict):
        key = payload.get("idempotency_key")
        try:
            if not isinstance(key, str) or str(uuid.UUID(key)) != key:
                raise ValueError
            email, credit = payload.get("email"), payload.get("credit_id")
            if not isinstance(email, str) or not 3 <= len(email) <= 254 or payload.get("confirmed") is not True:
                raise ValueError
            if credit is not None and (not isinstance(credit, str) or not 0 < len(credit) <= 512):
                raise ValueError
            if not config.get("enabled", True) or not config.get("codex_home"):
                raise ValueError
            home = home_identity(config)
        except (ValueError, TypeError, OSError):
            return 400, {"error": "重置参数无效，请刷新账号信息后重新确认。"}
        with self.lock:
            if self.error:
                return 503, {"error": self.error}
            old = self.records.get(key)
            if old and any(old.get(k) != v for k, v in {"account": account, "email": email, "credit_id": credit, "home": home}.items()):
                return 409, {"error": "这次操作绑定的账号或重置机会已变化，请核对账号。"}
            if old and old.get("status") == "completed":
                return 200, self.public(old)
            pending = next((r for k, r in self.records.items() if k != key and r.get("account") == account and r.get("status") in {"running", "unknown"}), None)
            if pending:
                return 409, {"error": "此账号有一次重置结果待确认，请先重试原操作。", "operation": self.public(pending)}
            if account in self.busy:
                return 202, self.public(old) if old else {"status": "running", "account": account}
            record = {"account": account, "home": home, "email": email, "credit_id": credit, "idempotency_key": key,
                      "status": "running", "updated_at": stamp(), "message": "正在核对账号并执行本次重置…"}
            self.records[key] = record
            try:
                self._save()  # Persist before any consumption RPC can be sent.
            except OSError:
                if old:
                    self.records[key] = old
                else:
                    self.records.pop(key)
                return 503, {"error": "无法保存重置操作记录，本次尚未提交。"}
            self.busy.add(account)
            threading.Thread(target=self._run, args=(key, dict(config), bool(old and old.get("status") in {"unknown", "running"})), daemon=True).start()
            return 202, self.public(record)

    def _run(self, key, config, retry):
        record = copy.deepcopy(self.records[key])
        data = None
        try:
            result = self.consumer(config, record, retry=retry)
            outcome, data = result["outcome"], result.get("data")
            if outcome not in MESSAGES:
                raise ValueError("Unknown reset outcome")
            changes = {"status": "completed", "outcome": outcome, "message": MESSAGES[outcome]}
            if data is None:
                changes["message"] = ("重置已完成，额度待重新读取。" if outcome in {"reset", "alreadyRedeemed"} else MESSAGES[outcome])
        except ResetRejected as exc:
            changes = {"status": "unknown" if retry else "rejected", "message": str(exc) + (" 原重置结果仍待确认，请保留同一次操作。" if retry else "")}
        except Exception:
            changes = {"status": "unknown", "message": "连接中断，重置结果待确认。请重试同一次操作，避免重复使用机会。"}
        with self.lock:
            self.records[key].update(**changes, updated_at=stamp())
            try:
                self._save()
            except OSError:
                self.error = "重置结果无法保存，请保留本次操作并先检查本地记录文件。"
            self.busy.discard(record["account"])
        self.cache.after_reset(record["account"], config, data)
