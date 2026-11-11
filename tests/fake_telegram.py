"""本地伪 Telegram Bot API 服务（标准库），支持多个机器人，让测试走真实的 HTTP 客户端代码路径。"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class FakeTelegram:
    def __init__(self, bots: dict | None = None):
        """bots: {token: username}。默认一个机器人 TEST=team_test_bot。"""
        bots = bots or {"TEST": "team_test_bot"}
        self.bots = {tok: {"id": 900 + i, "is_bot": True, "username": u, "first_name": u,
                           "can_read_all_group_messages": True} for i, (tok, u) in enumerate(bots.items())}
        self.updates: dict[str, list] = {tok: [] for tok in bots}
        self.next_update = {tok: 1000 for tok in bots}
        self.sent: list[dict] = []
        self.next_msg = 5000
        self.lock = threading.Lock()
        self.fail_send = 0
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                _, _, rest = self.path.partition("/bot")
                token, _, method = rest.partition("/")
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
                ok, result = outer.handle(token, method, body)
                data = json.dumps({"ok": True, "result": result} if ok else
                                  {"ok": False, "error_code": 500, "description": result}).encode()
                self.send_response(200 if ok else 500)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base(self):
        return f"http://127.0.0.1:{self.server.server_port}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def username(self, token):
        return self.bots[token]["username"]

    def handle(self, token, method, body):
        with self.lock:
            if token not in self.bots:
                return False, "Unauthorized"
            if method == "getMe":
                return True, self.bots[token]
            if method == "getChat":
                return True, {"id": body["chat_id"], "type": "group", "title": "测试群"}
            if method == "getUpdates":
                off = body.get("offset") or 0
                return True, [u for u in self.updates[token] if u["update_id"] >= off]
            if method == "sendMessage":
                if self.fail_send > 0:
                    self.fail_send -= 1
                    return False, "temporary failure"
                self.next_msg += 1
                rp = body.get("reply_parameters") or {}
                msg = {"message_id": self.next_msg, "chat": {"id": body["chat_id"]}, "text": body["text"],
                       "reply_to": rp.get("message_id"), "from": self.bots[token], "bot": token}
                self.sent.append(msg)
                return True, msg
        return False, f"unknown method {method}"

    def push_message(self, text, chat_id=-100123, user_id=42, chat_type="supergroup", reply_to=None,
                     message_id=None, update_id=None, edited=False, to=None):
        """to：投递给哪些机器人（token 列表），默认全部（相当于隐私模式关闭）。"""
        with self.lock:
            if message_id is None:
                self.next_msg += 1
                message_id = self.next_msg
            msg = {"message_id": message_id, "chat": {"id": chat_id, "type": chat_type, "title": "测试群"},
                   "from": {"id": user_id, "is_bot": False, "first_name": "站长"}, "text": text, "date": 0}
            if reply_to:
                src = next((m for m in self.sent if m["message_id"] == reply_to), None)
                msg["reply_to_message"] = {"message_id": reply_to}
                if src:
                    msg["reply_to_message"]["from"] = src["from"]
            last_uid = None
            for tok in (to or list(self.bots)):
                if update_id is None:
                    self.next_update[tok] += 1
                    uid = self.next_update[tok]
                else:
                    uid = update_id
                self.updates[tok].append({"update_id": uid, "edited_message" if edited else "message": dict(msg)})
                last_uid = uid
            return message_id, last_uid
