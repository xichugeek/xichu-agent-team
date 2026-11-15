"""群消息解析与鉴权。"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass

# 工单类型（不是群里的机器人）。群里的机器人见 config.roles：pm / video / site。
ROBOTS = {
    "pm": "项目经理拆单",
    "video": "视频制作",
    "review": "独立质检",
    "site": "官网运维",
}
# 机器人收到不带命令的派单时，对应的工单类型
ROLE_TASK = {"pm": "pm", "video": "video", "site": "site"}

ALIASES = {
    "pm": "pm", "项目经理": "pm",
    "video": "video", "视频": "video", "视频制作": "video",
    "review": "review", "审核": "review",
    "site": "site", "官网": "site", "官网运维": "site",
    "published": "published", "发布链接": "published", "已发布": "published",
    "status": "status", "状态": "status",
    "retry": "retry", "重试": "retry",
    "continue": "continue", "接续": "continue",
    "approve": "approve", "批准": "approve",
    "cancel": "cancel", "取消": "cancel",
    "help": "help", "start": "help", "帮助": "help",
}

TICKET_RE = re.compile(r"\bT-\d{4,}\b", re.I)


@dataclass
class Command:
    kind: str
    arg: str
    ticket_id: str | None = None


MENTION_RE = re.compile(r"@([A-Za-z0-9_]{5,32})")


def addressed_role(message: dict, usernames: dict) -> str | None:
    """消息点名给了哪个机器人：先看 @用户名 和 /命令@用户名，再看是回复了哪个机器人的消息。"""
    rev = {u.lower(): r for r, u in usernames.items() if u}
    text = message.get("text") or message.get("caption") or ""
    for name in MENTION_RE.findall(text):
        if name.lower() in rev:
            return rev[name.lower()]
    rt = (message.get("reply_to_message") or {}).get("from") or {}
    if rt.get("is_bot") and (rt.get("username") or "").lower() in rev:
        return rev[rt["username"].lower()]
    return None


def strip_mentions(text: str, usernames: dict) -> str:
    names = {u.lower() for u in usernames.values() if u}
    out = MENTION_RE.sub(lambda m: "" if m.group(1).lower() in names else m.group(0), text or "")
    return re.sub(r"\s+", " ", out).strip()


def parse(text: str, bot_username=None) -> Command | None:
    """bot_username 可以是单个用户名或我们所有机器人的用户名集合。"""
    text = (text or "").strip()
    if not text:
        return None
    ours = {bot_username} if isinstance(bot_username, str) else set(bot_username or ())
    ours = {u.lower() for u in ours if u}
    if text.startswith("/"):
        head, _, rest = text[1:].partition(" ")
        head, _, target = head.partition("@")
        if target and ours and target.lower() not in ours:
            return None  # 发给群里别的（不是我们的）机器人的命令
        name = ALIASES.get(head.lower())
    else:
        m = re.match(r"^([一-鿿]{2,4})\s*[:：]\s*(.*)$", text, re.S)
        if not m:
            return None
        name, rest = ALIASES.get(m.group(1)), m.group(2)
    if not name:
        return None
    rest = rest.strip()
    tid = TICKET_RE.search(rest)
    return Command(name, rest, tid.group(0).upper() if tid else None)


def authorized(message: dict, owner_user_id: int, group_chat_id: int) -> tuple[bool, str]:
    chat = message.get("chat") or {}
    sender = message.get("from") or {}
    if chat.get("type") not in ("group", "supergroup"):
        return False, "not_group"
    if int(chat.get("id", 0)) != int(group_chat_id):
        return False, "wrong_chat"
    if sender.get("is_bot"):
        return False, "from_bot"
    if int(sender.get("id", 0)) != int(owner_user_id):
        return False, "not_owner"
    if message.get("sender_chat"):
        return False, "anonymous_sender"
    return True, "ok"


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).lower()
    return re.sub(r"\s+", " ", text).strip()


def text_hash(robot: str, text: str) -> str:
    return hashlib.sha256(f"{robot}\n{normalize(text)}".encode("utf-8")).hexdigest()


def message_fingerprint(message: dict) -> str:
    """Basic-group message IDs differ per bot; sender/date/content identify one delivery."""
    files = []
    for kind in ("document", "video", "audio", "voice", "animation"):
        f = message.get(kind) or {}
        if f:
            files.append([kind, f.get("file_unique_id") or f.get("file_name"), f.get("file_size")])
    if message.get("photo"):
        f = message["photo"][-1]
        files.append(["photo", f.get("file_unique_id"), f.get("file_size")])
    data = [message.get("chat", {}).get("id"), message.get("from", {}).get("id"),
            message.get("date") or ("legacy", message.get("message_id")),
            message.get("text") or message.get("caption") or "", files]
    return hashlib.sha256(json.dumps(data, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


HELP = """西楚极客 Agent 指挥中心（本机调度器）
群里三个机器人：
· 项目经理(Codex)：{pm_pool}，@它拆单派发，并在独立会话中质检
· 视频制作(Codex)：{video_pool}，按 Studio 流程切换岗位，到人工关卡停下
· 官网运维(Codex)：{site_pool}，接原 Forge/官网工作区及发布工作流，按具体工单授权执行
派单（同一任务只发一次，进度都回复在原消息下）：
@项目经理 需求 —— 由项目经理拆单派发
@视频制作 / @官网运维 需求 —— 直接派给该机器人
也可用命令：/video  /review（只做质检）  /site
确认已发布：回复该工单任一消息 /published https://youtu.be/xxxxxxxxxxx
接续：/continue T-0001 补充要求（或回复原工单）
人工确认：/approve T-0001 SCRIPT_APPROVAL（只确认机器人已列出的具体目标/哈希）
其他：/status [T-0001]  /retry T-0001  /cancel T-0001
可以随命令附图片、文档、音视频；回复工单发附件可接续。额度用完自动切换账号，资料从原项目及工单交接恢复。人工关卡保留，机器人质检不会代签。"""
