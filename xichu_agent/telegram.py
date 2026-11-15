"""最小 Telegram Bot API 客户端（标准库），长轮询 getUpdates，无需公网入口。"""

from __future__ import annotations

import json
import mimetypes
import uuid
from pathlib import Path
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
        self.file_base = f"{api_base.rstrip('/')}/file/bot{token}/"
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

    def download_file(self, file_id: str, target: Path, max_bytes=20_000_000):
        info = self.call("getFile", {"file_id": file_id})
        if info.get("file_size", 0) > max_bytes:
            raise TelegramError("附件超过当前 Bot API 下载上限，请提供本机路径")
        path = info.get("file_path", "")
        if not path or path.startswith(("/", "\\")) or ".." in path.split("/") or ":" in path:
            raise TelegramError("Telegram 附件路径无效")
        try:
            with self.opener.open(self.file_base + path, timeout=90) as resp:
                data = resp.read(max_bytes + 1)
        except (urllib.error.URLError, OSError, TimeoutError):
            raise TelegramError("附件下载失败（网络或文件已失效）") from None
        if len(data) > max_bytes:
            raise TelegramError("附件超过下载上限")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        return target

    def send_document(self, chat_id: int, path: Path, reply_to=None):
        path = Path(path)
        if path.stat().st_size > 49_000_000:
            raise TelegramError("交付附件超过49MB，请从本机工单目录查看", permanent=True)
        boundary = "Xichu" + uuid.uuid4().hex
        fields = {"chat_id": str(chat_id)}
        if reply_to:
            fields["reply_parameters"] = json.dumps({"message_id": reply_to, "allow_sending_without_reply": True})
        chunks = []
        for key, value in fields.items():
            chunks.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode())
        name = path.name.replace('"', '_').replace('\r', '_').replace('\n', '_')
        kind = mimetypes.guess_type(name)[0] or "application/octet-stream"
        chunks += [f'--{boundary}\r\nContent-Disposition: form-data; name="document"; filename="{name}"\r\nContent-Type: {kind}\r\n\r\n'.encode(),
                   path.read_bytes(), f'\r\n--{boundary}--\r\n'.encode()]
        req = urllib.request.Request(self.base + "sendDocument", data=b"".join(chunks),
                                     headers={"Content-Type": "multipart/form-data; boundary=" + boundary})
        try:
            with self.opener.open(req, timeout=120) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, OSError, TimeoutError):
            raise TelegramError("交付附件发送失败（网络或Bot API限制）") from None
        if not body.get("ok"):
            raise TelegramError("交付附件被Bot API拒绝")
        return body["result"]


def message_attachments(msg, bot):
    items = []
    for kind in ("document", "video", "audio", "voice", "animation"):
        if msg.get(kind):
            items.append((kind, msg[kind]))
    if msg.get("photo"):
        items.append(("photo", msg["photo"][-1]))
    return [{"bot": bot, "kind": kind, "file_id": item["file_id"], "file_unique_id": item.get("file_unique_id"),
             "file_name": item.get("file_name") or (kind + (".jpg" if kind == "photo" else ".bin")),
             "bytes": item.get("file_size"), "message_id": msg["message_id"]} for kind, item in items]
