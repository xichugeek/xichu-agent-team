"""YouTube 发布链接解析与公开状态检查（oEmbed，只读）。"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request

VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
URL_IN_TEXT = re.compile(r"https?://\S+")


def extract_video_id(url: str) -> str | None:
    try:
        parts = urllib.parse.urlsplit(url.strip())
    except ValueError:
        return None
    if parts.scheme != "https":
        return None
    host = (parts.hostname or "").lower()
    path = parts.path
    vid = None
    if host == "youtu.be":
        vid = path.strip("/").split("/")[0]
    elif host in ("youtube.com", "www.youtube.com", "m.youtube.com"):
        if path == "/watch":
            vid = (urllib.parse.parse_qs(parts.query).get("v") or [None])[0]
        else:
            m = re.match(r"^/(shorts|live|embed)/([^/?#]+)", path)
            vid = m.group(2) if m else None
    return vid if vid and VIDEO_ID.match(vid) else None


def find_url(text: str) -> str | None:
    m = URL_IN_TEXT.search(text or "")
    return m.group(0).rstrip("。，,.)）>") if m else None


def canonical(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"


def oembed_check(video_id: str, timeout: float = 15, proxy: str | None = None) -> dict:
    """返回 {ok, status, title, author}；私享/删除/不存在时 ok=False。"""
    q = urllib.parse.urlencode({"url": canonical(video_id), "format": "json"})
    handlers = [urllib.request.ProxyHandler({"http": proxy, "https": proxy})] if proxy else []
    opener = urllib.request.build_opener(*handlers)
    req = urllib.request.Request(f"https://www.youtube.com/oembed?{q}",
                                 headers={"User-Agent": "XichuAgent/0.1"})
    try:
        with opener.open(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return {"ok": True, "status": resp.status, "title": data.get("title"),
                    "author": data.get("author_name"), "checked": "oembed"}
    except urllib.error.HTTPError as exc:
        exc.close()
        return {"ok": False, "status": exc.code, "checked": "oembed"}
    except (urllib.error.URLError, OSError, TimeoutError, ValueError) as exc:
        return {"ok": False, "status": None, "error": str(exc)[:200], "checked": "oembed"}
