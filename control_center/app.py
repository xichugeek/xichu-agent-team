"""Local Studio service with opt-in, authenticated LAN viewing."""
from __future__ import annotations

import argparse
import json
import os
import secrets
import subprocess
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from .pipeline import enrich
from .agent_reader import AgentReader
from .quota_reset import ResetManager
from .lan_access import LanAccess
from .status_reader import StatusReader, now

ROOT = Path(__file__).resolve().parents[1]
STATIC = Path(__file__).parent / "static"
SERVICE_ID = "xichugeek-studio-control-center-v1"


class ControlCenter:
    def __init__(self, root: Path = ROOT):
        self.reader = StatusReader(root)
        self.agents = AgentReader(root)
        self.reset_token = secrets.token_urlsafe(32)
        self.resets = ResetManager(root / "output/control_center/quota-resets.json", self.agents.quotas)
        self.lock = threading.Lock()
        self.validations: dict[str, dict] = {}
        self.busy = False
        self.stopping = False

    def validation_for(self, detail: dict) -> dict:
        with self.lock:
            result = dict(self.validations.get(detail["ref"], {"status": "NOT_RUN", "checked_at": None, "issues": []}))
        if result.get("fingerprint") and result["fingerprint"] != detail["fingerprint"] and result["status"] != "RUNNING":
            result["previous_status"] = result["status"]
            result["status"] = "STALE"
            result["issues"] = ["文件 metadata 已变化；此前 Validate 不代表当前版本，请手动重检。"]
        result.pop("fingerprint", None)
        return result

    def detail(self, ref: str) -> dict:
        detail = self.reader.read(ref)
        return enrich(detail, self.validation_for(detail))

    def snapshot(self, ref: str | None = None) -> dict:
        episodes = self.reader.listing()
        formal = [x for x in episodes if not x["test_only"] and x["status"] != "PUBLISHED"]
        candidates = formal or [x for x in episodes if not x["test_only"]] or episodes
        active = max(candidates, key=lambda x: x["updated_at"] or "", default=None)
        selected = ref or (active["ref"] if active else None)
        detail = self.detail(selected) if selected else None
        return {"studio": {"name": "XichuGeek Studio", "service_id": SERVICE_ID,
                           "root": str(self.reader.root), "pid": os.getpid(), "mode": "STUDIO READ ONLY",
                           "refresh_seconds": 4, "active_episode": active["ref"] if active else None,
                           "active_selection": "按最近更新的未发布正式 Episode 推导；不是运行进程监测",
                           "scanned_at": now()}, "episodes": episodes, "episode": detail,
                "health": self.reader.health(detail)}

    def start_validation(self, ref: str) -> dict:
        detail = self.reader.read(ref)
        key = detail["ref"]
        with self.lock:
            if self.busy:
                return {"status": "BUSY", "message": "已有一次 Validate 正在执行，请等待结果。"}
            self.busy = True
            self.validations[key] = {"status": "RUNNING", "checked_at": None, "issues": [], "fingerprint": detail["fingerprint"]}
        threading.Thread(target=self._validate, args=(key, detail["fingerprint"]), daemon=True).start()
        return {"status": "RUNNING", "message": "正在调用现有 Studio Validate；仅本次手动检查计算媒体指纹。"}

    def _validate(self, ref: str, fingerprint: str):
        try:
            worker = Path(__file__).with_name("validate_worker.py")
            process = subprocess.run([sys.executable, "-B", "-X", "utf8", str(worker), ref],
                                     cwd=self.reader.root, capture_output=True, text=True, encoding="utf-8",
                                     timeout=180, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            result = json.loads(process.stdout) if process.returncode == 0 else {}
            if not isinstance(result, dict) or result.get("status") not in {"PASS", "FAIL", "ERROR", "UNSAFE"}:
                raise ValueError("Invalid validator result")
        except (OSError, ValueError, subprocess.TimeoutExpired):
            result = {"status": "ERROR", "issues": ["Validate 超时或无法运行，请在终端排查。"]}
        result.update(checked_at=now(), fingerprint=fingerprint)
        with self.lock:
            self.validations[ref] = result
            self.busy = False


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, port=18765, center: ControlCenter | None = None, access: LanAccess | None = None):
        self.center = center or ControlCenter()
        self.access = access or LanAccess()
        super().__init__(("0.0.0.0" if self.access.enabled else "127.0.0.1", port), Handler)


class Handler(BaseHTTPRequestHandler):
    server_version = "XichuGeekControlCenter/1"

    def log_message(self, fmt, *args):
        # Avoid echoing request URLs/parameters into logs.
        pass

    def local_request(self) -> bool:
        allowed = {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}
        return self.client_address[0] == "127.0.0.1" and self.headers.get("Host", "").lower() in allowed

    def allow_read(self) -> bool:
        if self.local_request():
            return True
        access = self.server.access
        if not access.allows(self.client_address[0], self.headers.get("Host", "").lower(), self.server.server_port):
            self.send(403, {"error": "此地址不在已启用的局域网范围内。"})
            return False
        status = access.authenticate(self.client_address[0], self.headers.get("Authorization", ""))
        if status != 200:
            headers = {"WWW-Authenticate": 'Basic realm="XichuGeek Studio - viewer", charset="UTF-8"'} if status == 401 else {"Retry-After": "60"}
            self.send(status, {"error": "请输入局域网用户名 viewer 和访问密码。" if status == 401 else "尝试次数过多，请一分钟后重试。"}, extra_headers=headers)
            return False
        return True

    def send(self, code: int, body, content_type="application/json; charset=utf-8", extra_headers=None):
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") if not isinstance(body, bytes) else body
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; base-uri 'none'; frame-ancestors 'none'; form-action 'none'")
        for name, value in (extra_headers or {}).items():
            self.send_header(name, value)
        try:
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def do_GET(self):
        if not self.allow_read():
            return
        parsed = urlsplit(self.path)
        path = unquote(parsed.path)
        try:
            if path in ("/", "/index.html"):
                return self.send(200, (STATIC / "index.html").read_bytes(), "text/html; charset=utf-8")
            for filename, content_type in (("studio.css", "text/css; charset=utf-8"), ("studio.js", "text/javascript; charset=utf-8"), ("agent.js", "text/javascript; charset=utf-8"), ("navigation.js", "text/javascript; charset=utf-8"), ("site.js", "text/javascript; charset=utf-8")):
                if path == "/static/" + filename:
                    return self.send(200, (STATIC / filename).read_bytes(), content_type)
            if path == "/api/snapshot":
                ref = parse_qs(parsed.query).get("episode", [None])[0]
                result = self.server.center.snapshot(ref)
                result["studio"]["lan_read_only"] = not self.local_request()
                return self.send(200, result)
            if path == "/api/episodes":
                return self.send(200, {"episodes": self.server.center.reader.listing()})
            if path == "/api/site":
                ticket = parse_qs(parsed.query).get("ticket", [None])[0]
                return self.send(200, self.server.center.agents.site_snapshot(self.server.center.reader.listing(), ticket))
            if path == "/api/agent":
                ticket = parse_qs(parsed.query).get("ticket", [None])[0]
                result = self.server.center.agents.snapshot(self.server.center.reader.listing(), ticket)
                result["lan_read_only"] = not self.local_request()
                result["reset_token"] = self.server.center.reset_token if self.local_request() else None
                for account in result["accounts"]:
                    account["reset_operation"] = self.server.center.resets.state(account["id"])
                return self.send(200, result)
            if path == "/api/health":
                return self.send(200, {"service_id": SERVICE_ID, "pid": os.getpid(), "root": str(self.server.center.reader.root),
                                       "read_only": not self.local_request(), "production_read_only": True,
                                       "account_reset_enabled": self.local_request(), "lan_enabled": self.server.access.enabled,
                                       "lan_urls": self.server.access.urls(self.server.server_port) if self.local_request() else [],
                                       "health": self.server.center.reader.health()})
            if path.startswith("/api/episodes/") and path.count("/") == 3:
                return self.send(200, self.server.center.detail(path.rsplit("/", 1)[1]))
            return self.send(404, {"error": "没有此只读页面或接口；不提供文件正文下载"})
        except (ValueError, FileNotFoundError):
            return self.send(404, {"error": "Episode 不存在或引用不合法"})
        except Exception:
            return self.send(500, {"error": "无法读取状态；生产文件未修改，请核对契约格式"})

    def do_POST(self):
        # Drain bounded request bodies before rejecting an origin/header. Closing
        # a Windows socket with unread input can discard the intended 4xx reply.
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 <= length <= 2048:
                return self.send(400, {"error": "请求内容过大或长度无效。"})
            previous_timeout = self.connection.gettimeout()
            self.connection.settimeout(5)
            try:
                body = self.rfile.read(length)
            finally:
                self.connection.settimeout(previous_timeout)
        except (ValueError, OSError):
            return self.send(400, {"error": "无法读取请求内容。"})
        if not self.local_request():
            return self.send(403, {"error": "局域网访问仅供查看；请在电脑本机执行此操作。"})
        parsed = urlsplit(self.path)
        path = unquote(parsed.path)
        expected_origin = "http://" + self.headers["Host"]
        parts = path.strip("/").split("/")
        if len(parts) == 4 and parts[:2] == ["api", "accounts"] and parts[3] == "reset":
            if (self.headers.get("Origin") != expected_origin
                    or self.headers.get("X-Studio-Request") != "quota-reset"
                    or not secrets.compare_digest(self.headers.get("X-Studio-CSRF", ""), self.server.center.reset_token)):
                return self.send(403, {"error": "额度重置必须由本面板手动确认。"})
            try:
                if not 0 < length <= 2048 or self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                    return self.send(400, {"error": "重置参数无效。"})
                payload = json.loads(body)
                if not isinstance(payload, dict) or set(payload) - {"email", "credit_id", "idempotency_key", "confirmed"}:
                    return self.send(400, {"error": "重置参数无效。"})
                config = self.server.center.agents.account_configs().get(parts[2])
                if config is None:
                    return self.send(404, {"error": "未配置此 Codex 账号。"})
                code, response = self.server.center.resets.start(parts[2], config, payload)
                return self.send(code, response)
            except (ValueError, OSError, KeyError):
                return self.send(400, {"error": "无法读取账号或重置参数，请刷新后重试。"})
        if (self.headers.get("X-Studio-Request") != "validate"
                or self.headers.get("Origin", expected_origin) != expected_origin):
            return self.send(403, {"error": "手动 Validate 必须来自本面板"})
        if len(parts) != 4 or parts[:2] != ["api", "episodes"] or parts[3] != "validate":
            return self.send(405, {"error": "面板只读；没有批准、sync、编辑或发布接口"})
        try:
            if not 0 <= length <= 64:
                return self.send(400, {"error": "无需提交状态或命令"})
            if body.strip() not in (b"", b"{}"):
                return self.send(400, {"error": "无需提交状态或命令"})
            result = self.server.center.start_validation(parts[2])
            return self.send(202 if result["status"] == "RUNNING" else 409, result)
        except (ValueError, FileNotFoundError):
            return self.send(404, {"error": "Episode 不存在或引用不合法"})

    def do_PUT(self):
        self.send(405, {"error": "READ ONLY"})

    do_DELETE = do_PATCH = do_PUT


def main() -> int:
    parser = argparse.ArgumentParser(description="XichuGeek Studio · 本地制作看板与账号额度")
    parser.add_argument("--port", type=int, default=18765)
    parser.add_argument("--studio-root", type=Path, default=ROOT, help="Read an existing Studio workspace; keep private data outside this repository")
    parser.add_argument("--enable-lan", action="append", metavar="IP/PREFIX", help="Opt in to password-protected read-only LAN viewing for your own interface, e.g. 192.168.50.10/24")
    parser.add_argument("--open", action="store_true", help="启动后打开默认浏览器")
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error("端口必须为 1024–65535")
    try:
        studio_root = args.studio_root.expanduser().resolve()
        if args.enable_lan:
            password = LanAccess.configure(studio_root, args.enable_lan)
            print(f"LAN username: viewer\nLAN password (keep private): {password}", flush=True)
        access = LanAccess.load(studio_root)
        server = Server(args.port, ControlCenter(studio_root), access=access)
    except (ValueError, TypeError):
        print("局域网访问配置无效，请检查 output/control_center/lan-access.local.json。", flush=True)
        return 1
    except OSError:
        print(f"无法绑定 127.0.0.1:{args.port}；端口可能已占用，可用 --port 指定其他端口。", flush=True)
        return 1
    url = f"http://127.0.0.1:{server.server_port}"
    print(f"XichuGeek Studio Control Center\nRunning at:\n{url}\nStudio read-only / Manual account resets · Ctrl+C stops the service", flush=True)
    for lan_url in access.urls(server.server_port):
        print(f"LAN read-only (username viewer, password required): {lan_url}", flush=True)
    if args.open:
        webbrowser.open(url)
    try:
        server.serve_forever(poll_interval=.5)
    except KeyboardInterrupt:
        print("\nControl Center stopped.", flush=True)
    finally:
        server.server_close()
    return 0
