"""SQLite 工单库。所有状态变化都在事务里完成，进程崩溃后可从库中恢复。"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

ACTIVE_TICKET_STATES = ("queued", "running", "waiting_publish_link", "review_rejected", "waiting_approval", "needs_info", "partial")
TERMINAL_TICKET_STATES = ("draft_ready", "done", "failed", "cancelled")

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS updates (
    update_id INTEGER PRIMARY KEY, received_at TEXT NOT NULL, outcome TEXT
);
CREATE TABLE IF NOT EXISTS tickets (
    id TEXT PRIMARY KEY,
    seq INTEGER NOT NULL UNIQUE,
    chat_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    robot TEXT NOT NULL,
    text TEXT NOT NULL,
    text_hash TEXT NOT NULL,
    state TEXT NOT NULL,
    stage TEXT,
    round INTEGER NOT NULL DEFAULT 1,
    youtube_url TEXT,
    youtube_info TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    source_bot TEXT NOT NULL DEFAULT 'pm',
    UNIQUE (source_bot, chat_id, message_id)
);
CREATE TABLE IF NOT EXISTS stages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id TEXT NOT NULL REFERENCES tickets(id),
    name TEXT NOT NULL,
    executor TEXT NOT NULL,
    round INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL,
    attempt INTEGER NOT NULL DEFAULT 1,
    workdir TEXT,
    result TEXT,
    error TEXT,
    simulated INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    last_heartbeat TEXT
);
CREATE TABLE IF NOT EXISTS outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id TEXT,
    dedupe_key TEXT NOT NULL UNIQUE,
    chat_id INTEGER NOT NULL,
    reply_to INTEGER,
    text TEXT NOT NULL,
    sent INTEGER NOT NULL DEFAULT 0,
    sent_message_id INTEGER,
    sent_at TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id TEXT, at TEXT NOT NULL, kind TEXT NOT NULL, detail TEXT
);
CREATE TABLE IF NOT EXISTS bot_updates (
    bot TEXT NOT NULL, update_id INTEGER NOT NULL, received_at TEXT NOT NULL, outcome TEXT,
    PRIMARY KEY (bot, update_id)
);
CREATE TABLE IF NOT EXISTS seen_messages (
    chat_id INTEGER NOT NULL, message_id INTEGER NOT NULL, bot TEXT, at TEXT NOT NULL,
    PRIMARY KEY (chat_id, message_id)
);
CREATE INDEX IF NOT EXISTS idx_stages_status ON stages(status, executor);
CREATE INDEX IF NOT EXISTS idx_outbox_sent ON outbox(sent);
CREATE INDEX IF NOT EXISTS idx_outbox_msg ON outbox(chat_id, sent_message_id);
CREATE TABLE IF NOT EXISTS incoming_messages (fingerprint TEXT PRIMARY KEY, at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS incoming_deliveries (
 fingerprint TEXT NOT NULL, bot TEXT NOT NULL, chat_id INTEGER NOT NULL, message_id INTEGER NOT NULL,
 PRIMARY KEY(bot,chat_id,message_id)
);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self.conn().executescript(SCHEMA)  # executescript 自带提交，不能放进 BEGIN 事务
        self._migrate()

    def _migrate(self) -> None:
        """给早期版本的库补列（多机器人、岗位与账号池）。"""
        c = self.conn()
        cols = lambda t: {r["name"] for r in c.execute(f"PRAGMA table_info({t})")}
        if "bot" not in cols("outbox"):
            c.execute("ALTER TABLE outbox ADD COLUMN bot TEXT NOT NULL DEFAULT 'pm'")
        if "role" not in cols("stages"):
            c.execute("ALTER TABLE stages ADD COLUMN role TEXT")
            for name, role in (("produce", "video"), ("review", "pm"), ("site_draft", "site"), ("site_ops", "site")):
                c.execute("UPDATE stages SET role=? WHERE name=? AND role IS NULL", (role, name))
        if "brief" not in cols("tickets"):
            c.execute("ALTER TABLE tickets ADD COLUMN brief TEXT")
        for name in ("continuation", "attachments", "source_fingerprint"):
            if name not in cols("tickets"):
                c.execute(f"ALTER TABLE tickets ADD COLUMN {name} TEXT")
        if "source_bot" not in cols("tickets"):
            c.execute("ALTER TABLE tickets ADD COLUMN source_bot TEXT NOT NULL DEFAULT 'pm'")
        sql = c.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='tickets'").fetchone()[0]
        old_unique = r"UNIQUE\s*\(\s*chat_id\s*,\s*message_id\s*\)"
        if re.search(old_unique, sql, re.I):
            # Basic-group message numbers belong to individual bot accounts.
            create = re.sub(r"CREATE TABLE\s+tickets\b", "CREATE TABLE tickets_message_migration", sql, count=1, flags=re.I)
            create = re.sub(old_unique, "UNIQUE (source_bot, chat_id, message_id)", create, flags=re.I)
            names = ','.join('"' + row['name'] + '"' for row in c.execute('PRAGMA table_info(tickets)'))
            c.execute("PRAGMA foreign_keys=OFF")
            try:
                with self.tx() as tx:
                    tx.execute(create)
                    tx.execute(f"INSERT INTO tickets_message_migration({names}) SELECT {names} FROM tickets")
                    tx.execute("DROP TABLE tickets")
                    tx.execute("ALTER TABLE tickets_message_migration RENAME TO tickets")
            finally:
                c.execute("PRAGMA foreign_keys=ON")
        if "document" not in cols("outbox"):
            c.execute("ALTER TABLE outbox ADD COLUMN document TEXT")
        if "reply_fingerprint" not in cols("outbox"):
            c.execute("ALTER TABLE outbox ADD COLUMN reply_fingerprint TEXT")
        if "sent_at" not in cols("outbox"):
            c.execute("ALTER TABLE outbox ADD COLUMN sent_at TEXT")
        old = c.execute("SELECT value FROM meta WHERE key='tg_offset'").fetchone()
        if old and not c.execute("SELECT 1 FROM meta WHERE key='tg_offset:pm'").fetchone():
            c.execute("INSERT INTO meta(key,value) VALUES('tg_offset:pm',?)", (old["value"],))

    def conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=FULL")
            conn.execute("PRAGMA foreign_keys=ON")
            self._local.conn = conn
        return conn

    @contextmanager
    def tx(self):
        c = self.conn()
        c.execute("BEGIN IMMEDIATE")
        try:
            yield c
        except BaseException:
            c.execute("ROLLBACK")
            raise
        else:
            c.execute("COMMIT")

    def close(self):
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    # ---- meta ----
    def get_meta(self, key: str, default=None):
        row = self.conn().execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set_meta(self, c, key: str, value) -> None:
        c.execute("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                  (key, str(value)))

    # ---- events / outbox ----
    def event(self, c, ticket_id, kind, detail=None):
        if detail is not None and not isinstance(detail, str):
            detail = json.dumps(detail, ensure_ascii=False)
        c.execute("INSERT INTO events(ticket_id,at,kind,detail) VALUES(?,?,?,?)", (ticket_id, now(), kind, detail))

    def enqueue_reply(self, c, dedupe_key: str, chat_id: int, reply_to: int | None, text: str,
                      ticket_id: str | None = None, bot: str = "pm") -> bool:
        cur = c.execute(
            "INSERT OR IGNORE INTO outbox(ticket_id,dedupe_key,chat_id,reply_to,text,created_at,bot)"
            " VALUES(?,?,?,?,?,?,?)",
            (ticket_id, dedupe_key, chat_id, reply_to, text[:4000], now(), bot))
        if cur.rowcount:
            fp = None
            if ticket_id:
                t = c.execute("SELECT source_fingerprint FROM tickets WHERE id=?", (ticket_id,)).fetchone()
                fp = t[0] if t else None
            fp = fp or getattr(self._local, "incoming_fingerprint", None)
            if fp:
                c.execute("UPDATE outbox SET reply_fingerprint=? WHERE id=?", (fp, cur.lastrowid))
        return cur.rowcount == 1

    def pending_outbox(self, limit=20):
        return self.conn().execute(
            "SELECT * FROM outbox WHERE sent=0 AND attempts<20 ORDER BY id LIMIT ?", (limit,)).fetchall()

    # ---- tickets ----
    def ticket(self, ticket_id: str):
        return self.conn().execute("SELECT * FROM tickets WHERE id=?", (ticket_id,)).fetchone()

    def ticket_by_message(self, chat_id: int, message_id: int, bot: str | None = None):
        if bot:
            row = self.conn().execute("SELECT t.* FROM tickets t JOIN incoming_deliveries d ON d.fingerprint=t.source_fingerprint WHERE d.bot=? AND d.chat_id=? AND d.message_id=?", (bot, chat_id, message_id)).fetchone()
            if row:
                return row
        row = self.conn().execute("SELECT * FROM tickets WHERE chat_id=? AND message_id=?" + (" AND source_bot=?" if bot else ""),
                                  (chat_id, message_id, bot) if bot else (chat_id, message_id)).fetchone()
        if row:
            return row
        out = self.conn().execute(
            "SELECT ticket_id FROM outbox WHERE chat_id=? AND sent_message_id=? AND ticket_id IS NOT NULL" + (" AND bot=?" if bot else ""),
            (chat_id, message_id, bot) if bot else (chat_id, message_id)).fetchone()
        return self.ticket(out["ticket_id"]) if out else None

    def next_ticket_id(self, c) -> tuple[str, int]:
        seq = (c.execute("SELECT COALESCE(MAX(seq),0) FROM tickets").fetchone()[0]) + 1
        return f"T-{seq:04d}", seq

    def stages(self, ticket_id: str):
        return self.conn().execute("SELECT * FROM stages WHERE ticket_id=? ORDER BY id", (ticket_id,)).fetchall()

    def stage(self, stage_id: int):
        return self.conn().execute("SELECT * FROM stages WHERE id=?", (stage_id,)).fetchone()

    def latest_stage(self, ticket_id: str, name: str):
        return self.conn().execute("SELECT * FROM stages WHERE ticket_id=? AND name=? ORDER BY id DESC LIMIT 1",
                                   (ticket_id, name)).fetchone()

    def add_stage(self, c, ticket_id: str, name: str, role: str, round_: int, attempt: int = 1) -> int:
        """阶段只登记岗位；具体由账号池里哪个执行账号来做，在分派时决定。"""
        cur = c.execute(
            "INSERT INTO stages(ticket_id,name,executor,role,round,status,attempt,simulated,created_at)"
            " VALUES(?,?,'',?,?,'queued',?,0,?)", (ticket_id, name, role, round_, attempt, now()))
        c.execute("UPDATE tickets SET state='queued', stage=?, updated_at=? WHERE id=?", (name, now(), ticket_id))
        return cur.lastrowid
