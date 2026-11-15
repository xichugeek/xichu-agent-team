"""Subscription limits and explicit earned resets via official app-server RPC.

No thread or turn is started. Credentials remain inside the account's Codex home.
"""
from __future__ import annotations

import copy
import json
import os
import queue
import shutil
import subprocess
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


def stamp():
    return datetime.now(timezone.utc).isoformat()


def normalize_limits(result: dict) -> dict:
    buckets = result.get("rateLimitsByLimitId") or {}
    if not isinstance(buckets, dict):
        buckets = {}
    if not buckets and isinstance(result.get("rateLimits"), dict):
        bucket = result["rateLimits"]
        buckets = {bucket.get("limitId") or "codex": bucket}
    windows = []
    for key, bucket in buckets.items():
        if not isinstance(bucket, dict):
            continue
        for slot in ("primary", "secondary"):
            window = bucket.get(slot)
            if not isinstance(window, dict):
                continue
            used = window.get("usedPercent")
            valid = type(used) in (int, float) and 0 <= used <= 100
            mins = window.get("windowDurationMins")
            label = "5 小时" if mins == 300 else "每周" if mins == 10080 else f"{mins} 分钟" if type(mins) is int and mins > 0 else "窗口未知"
            reset = window.get("resetsAt")
            try:
                reset = datetime.fromtimestamp(reset, timezone.utc).isoformat() if type(reset) in (int, float) else None
            except (OverflowError, OSError, ValueError):
                reset = None
            windows.append({"bucket": str(key)[:100], "label": label,
                            "remaining_percent": round(100 - used, 2) if valid else None,
                            "resets_at": reset})
    earned = result.get("rateLimitResetCredits")
    count, credits = None, []
    if isinstance(earned, dict):
        value = earned.get("availableCount")
        count = value if type(value) is int and value >= 0 else None
        for credit in earned.get("credits") or []:
            if not isinstance(credit, dict) or credit.get("status") != "available" or credit.get("resetType") != "codexRateLimits":
                continue
            expiry = credit.get("expiresAt")
            try:
                expires_at = datetime.fromtimestamp(expiry, timezone.utc).isoformat() if type(expiry) in (int, float) else None
            except (OverflowError, OSError, ValueError):
                continue
            if expires_at and datetime.fromisoformat(expires_at) <= datetime.now(timezone.utc):
                continue
            if isinstance(credit.get("id"), str) and 0 < len(credit["id"]) <= 512:
                credits.append({"id": credit["id"], "expires_at": expires_at,
                                "title": str(credit.get("title") or "额度重置")[:200],
                                "description": str(credit.get("description") or "重置符合条件的 Codex 额度窗口")[:600]})
    credits.sort(key=lambda c: c["expires_at"] or "9999")
    return {"windows": windows, "ordinary_usage_allowed": result.get("ordinaryUsageAllowed")
            if isinstance(result.get("ordinaryUsageAllowed"), bool) else None,
            "reset_credits": {"available_count": count, "credits": credits}}


def resolve_cli(name: str) -> str:
    if name == "auto:codex-app":
        base = Path(os.environ.get("LOCALAPPDATA", "")) / "OpenAI/Codex/bin"
        candidates = list(base.glob("*/codex.exe")) + list(base.glob("codex.exe"))
        # The installed desktop bundle is updated atomically; prefer the newest binary.
        if candidates:
            return str(max(candidates, key=lambda p: p.stat().st_mtime))
        name = "codex"
    resolved = str(Path(name)) if Path(name).is_absolute() and Path(name).is_file() else shutil.which(name)
    if not resolved:
        raise OSError("Codex CLI unavailable")
    return resolved


@contextmanager
def app_session(config: dict, timeout: float = 35):
    env = os.environ.copy()
    # Scope identity to the configured subscription; never fall back to paid API keys.
    for key in list(env):
        if key.upper() in {"OPENAI_API_KEY", "OPENAI_BASE_URL", "CODEX_API_KEY", "OPENAI_ORG_ID", "OPENAI_PROJECT_ID"}:
            del env[key]
    if not config.get("codex_home"):
        raise ValueError("Missing account home")
    env["CODEX_HOME"] = str(config["codex_home"])
    proc = subprocess.Popen([resolve_cli(config.get("cli", "codex")), "app-server", "--listen", "stdio://"],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            text=True, encoding="utf-8", env=env,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    messages = queue.Queue(maxsize=256)

    def receive():
        try:
            for line in proc.stdout:
                try:
                    value = json.loads(line)
                except ValueError:
                    continue
                if "id" in value:
                    messages.put(value, timeout=1)
        except (OSError, ValueError, queue.Full):
            pass
        finally:
            try:
                messages.put_nowait({"closed": True})
            except queue.Full:
                pass

    threading.Thread(target=receive, daemon=True).start()
    deadline = time.monotonic() + timeout

    def send(value):
        proc.stdin.write(json.dumps(value) + "\n")
        proc.stdin.flush()

    def rpc(i, method, params=None):
        send({"id": i, "method": method, **({"params": params} if params is not None else {})})
        while True:
            response = messages.get(timeout=max(.01, deadline - time.monotonic()))
            if response.get("closed"):
                raise OSError("App server closed")
            if "method" in response:  # This reader grants no server-requested action.
                send({"id": response["id"], "error": {"code": -32601, "message": "Read-only client"}})
            elif response.get("id") == i:
                if "error" in response:
                    raise ValueError("Account RPC failed")
                return response.get("result", {})
            if time.monotonic() >= deadline:
                raise TimeoutError("Account RPC timed out")

    try:
        rpc(0, "initialize", {"clientInfo": {"name": "xichugeek_control_center", "title": "XichuGeek Control Center", "version": "0.2.0"}})
        send({"method": "initialized"})
        yield rpc
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=3)
        for stream in (proc.stdin, proc.stdout):
            stream.close()


def read_account(config: dict, timeout: float = 25) -> dict:
    with app_session(config, timeout) as rpc:
        account = rpc(1, "account/read", {"refreshToken": False}).get("account") or {}
        if account.get("type") != "chatgpt":
            raise ValueError("Subscription login required")
        return {"email": account.get("email"), "plan": account.get("planType"),
                **normalize_limits(rpc(2, "account/rateLimits/read"))}


class ResetRejected(Exception):
    """A reset was rejected before any credit consumption request was sent."""


def consume_reset(config: dict, request: dict, retry: bool = False) -> dict:
    sent = False
    try:
        with app_session(config) as rpc:
            account = rpc(1, "account/read", {"refreshToken": False}).get("account") or {}
            if account.get("type") != "chatgpt" or not account.get("email") or account["email"].casefold() != request["email"].casefold():
                raise ResetRejected("账号身份已变化，请刷新账号信息后再操作。")
            fresh = normalize_limits(rpc(2, "account/rateLimits/read"))
            identity = {"email": account["email"], "plan": account.get("planType")}
            if not retry:
                count = fresh["reset_credits"]["available_count"]
                if count == 0:
                    return {"outcome": "noCredit", "data": {**identity, **fresh}}
                if count is None:
                    raise ResetRejected("官方暂未返回可用重置次数，请稍后刷新。")
                if request.get("credit_id") and request["credit_id"] not in {c["id"] for c in fresh["reset_credits"]["credits"]}:
                    raise ResetRejected("选中的重置机会已失效，请刷新后重新选择。")
            params = {"idempotencyKey": request["idempotency_key"]}
            if request.get("credit_id"):
                params["creditId"] = request["credit_id"]
            sent = True
            outcome = rpc(3, "account/rateLimitResetCredit/consume", params).get("outcome")
            if outcome not in {"reset", "alreadyRedeemed", "nothingToReset", "noCredit"}:
                raise ValueError("Unknown reset outcome")
            try:
                refreshed = {**identity, **normalize_limits(rpc(4, "account/rateLimits/read"))}
            except Exception:
                refreshed = None
            return {"outcome": outcome, "data": refreshed}
    except ResetRejected:
        raise
    except Exception as exc:
        if not sent:
            raise ResetRejected("无法核对账号或可用机会，本次尚未提交重置。请稍后重试。") from exc
        raise  # The result is uncertain; the caller must retain this idempotency key.


class QuotaCache:
    def __init__(self, reader=read_account, clock=time.monotonic):
        self.reader, self.clock = reader, clock
        self.lock = threading.Lock()
        self.entries = {}

    def snapshot(self, accounts: dict, roles: dict) -> list[dict]:
        rows = []
        with self.lock:
            for key, config in accounts.items():
                identity = (key, config.get("codex_home"), config.get("cli"))
                entry = self.entries.setdefault(identity, {"next": 0, "busy": False, "data": None, "error": None, "checked_at": None, "attempted_at": None, "generation": 0})
                enabled = config.get("enabled", True)
                if enabled and not entry["busy"] and self.clock() >= entry["next"]:
                    entry["busy"] = True
                    threading.Thread(target=self._read, args=(identity, dict(config), entry["generation"]), daemon=True).start()
                data = copy.deepcopy(entry["data"] or {"windows": []})
                rows.append({"id": key, "label": config.get("label", key), "model": config.get("model"),
                             "primary_roles": [name for name, role in roles.items() if role.get("pool", [None])[0:1] == [key]],
                             "status": "disabled" if not enabled else "stale" if entry["error"] and entry["data"] else "error" if entry["error"] else "ok" if entry["data"] else "loading",
                             "refreshing": entry["busy"], "checked_at": entry["checked_at"], "attempted_at": entry["attempted_at"],
                             "message": entry["error"], **data})
        return rows

    def _read(self, identity, config, generation):
        try:
            data, error = self.reader(config), None
        except Exception:
            data, error = None, "额度读取失败，请检查此账号登录或连接；未推算剩余额度。"
        with self.lock:
            entry = self.entries[identity]
            if entry["generation"] != generation:
                return
            if data is not None:
                entry.update(data=data, checked_at=stamp())
            entry.update(error=error, busy=False, attempted_at=stamp(), next=self.clock() + (60 if error else 300))

    def after_reset(self, key: str, config: dict, data: dict | None):
        identity = (key, config.get("codex_home"), config.get("cli"))
        with self.lock:
            entry = self.entries.get(identity)
            if not entry:
                return
            entry.update(generation=entry["generation"] + 1, busy=False, next=self.clock() + (300 if data else 0))
            if data is not None:
                entry.update(data=copy.deepcopy(data), checked_at=stamp(), error=None)
            else:
                entry["error"] = "重置请求已返回，额度待重新读取。"
