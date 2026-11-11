"""群消息解析与鉴权。"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass

# 固定命令；岗位相关的命令和别名由 setup(roles) 按 config.json 生成
BASE_ALIASES = {
    "review": "review", "审核": "review", "质检": "review",
    "status": "status", "状态": "status",
    "retry": "retry", "重试": "retry",
    "cancel": "cancel", "取消": "cancel",
    "help": "help", "start": "help", "帮助": "help",
}
ALIASES: dict[str, str] = dict(BASE_ALIASES)
ROBOTS: dict[str, str] = {"review": "独立质检"}   # 工单类型 → 显示名
ROLE_TASK: dict[str, str] = {}                    # 机器人岗位 → 不带命令派单时的工单类型
HELP = ""


def setup(roles: dict) -> None:
    """按 config.json 的岗位生成命令别名、工单类型和帮助文字。"""
    global HELP
    ALIASES.clear()
    ALIASES.update(BASE_ALIASES)
    ROBOTS.clear()
    ROBOTS["review"] = "独立质检"
    ROLE_TASK.clear()
    for key, conf in roles.items():
        ALIASES[key.lower()] = key
        for a in conf.get("aliases") or []:
            ALIASES[str(a).lower()] = key
        ROBOTS[key] = "项目经理拆单" if key == "pm" else conf.get("label", key)
        ROLE_TASK[key] = key
    lines = ["AI 员工团队（本机调度器）", "群里的机器人："]
    for key, conf in roles.items():
        duty = "接需求、派给合适的员工，并负责独立质检" if key == "pm" else conf.get("duty", "")
        lines.append(f"· {conf.get('label', key)}：{duty}（命令 /{key}）")
    lines += ["派单（同一任务只发一次，进度都回复在原消息下）：",
              "@项目经理 需求 —— 由项目经理判断派给谁",
              "@某个员工 需求 —— 直接派给它，做完由项目经理质检",
              "/review 内容 —— 只让项目经理做一次质检",
              "其他：/status [T-0001]  /retry T-0001  /cancel T-0001  /help",
              "所有产物只写在本机 runtime\\work 下；发布、上传、付款、删除之类的事一律不做，交给你自己确认。"]
    HELP = "\n".join(lines)


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
