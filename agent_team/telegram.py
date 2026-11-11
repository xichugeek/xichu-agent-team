"""最小 Telegram Bot API 客户端（标准库），长轮询 getUpdates，无需公网入口。"""

from __future__ import annotations

import json
import urllib.error
import urllib.request


class TelegramError(RuntimeError):
    def __init__(self, message: str, retry_after: int | None = None, permanent: bool = False):
        super().__init__(message)
        self.retry_after = retry_after
        self.permanent = permanent


class TelegramClient:
    def __init__(self, token: str, api_base: str = "https://api.telegram.org", proxy: str | None = None):
        if not token:
            raise ValueError("缺少 Telegram bot token（XICHU_TG_BOT_TOKEN 或 secrets.local.json）")
        self.base = f"{api_base.rstrip('/')}/bot{token}/"
        handlers = []
        if proxy:
            handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
        self.opener = urllib.request.build_opener(*handlers)

    def call(self, method: str, payload: dict | None = None, timeout: float = 30):
        data = json.dumps(payload or {}).encode("utf-8")
        req = urllib.request.Request(self.base + method, data=data,
                                     headers={"Content-Type": "application/json"})
        try:
            with self.opener.open(req, timeout=timeout) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            try:
                body = json.loads(exc.read().decode("utf-8"))
            except Exception:
                raise TelegramError(f"HTTP {exc.code}") from None
            finally:
                exc.close()
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            raise TelegramError(f"网络错误: {exc}") from None
        if not body.get("ok"):
            params = body.get("parameters") or {}
            code = body.get("error_code")
            raise TelegramError(f"{code}: {body.get('description')}", params.get("retry_after"),
                                permanent=code in (400, 403))
        return body["result"]

    def get_me(self):
        return self.call("getMe")

    def get_chat(self, chat_id: int):
        return self.call("getChat", {"chat_id": chat_id})

    def get_updates(self, offset: int | None, timeout: int = 25):
        payload = {"timeout": timeout, "allowed_updates": ["message", "edited_message", "my_chat_member"]}
        if offset is not None:
            payload["offset"] = offset
        return self.call("getUpdates", payload, timeout=timeout + 15)

    def send_message(self, chat_id: int, text: str, reply_to: int | None = None):
        payload = {"chat_id": chat_id, "text": text[:4000], "disable_web_page_preview": True}
        if reply_to:
            payload["reply_parameters"] = {"message_id": reply_to, "allow_sending_without_reply": True}
        return self.call("sendMessage", payload)
