"""本地调度器：收单 → 存库 → 分派 → 核验 → 自动交接 → 回复原消息。

- 群里每个岗位一个机器人（项目经理 + 若干员工），各自用自己的名字回复；
- 只接受 owner 本人在指定私群里的消息；
- 去重：每个机器人的 update_id、跨机器人的 (chat_id, message_id)、"同内容未结工单"；
- 每个执行器一条车道；额度受限时冷却并排队，群里告诉你大约几点恢复；
- 状态变化与待发回复在同一事务里写库，重启后继续发送与处理。
"""

from __future__ import annotations

import json
import socket
import threading
import time
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import commands
from .executors import Executor, Outcome, build
from .store import ACTIVE_TICKET_STATES, Store, now
from .telegram import TelegramError
from .workflows import STAGE_LABEL, Validation, Workflows

STATE_LABEL = {"queued": "排队中", "running": "执行中", "review_rejected": "质检未通过，等待你处理",
               "needs_info": "项目经理需要你补充信息", "done": "已完成", "failed": "失败", "cancelled": "已取消"}


def _ts(s: str) -> datetime:
    return datetime.fromisoformat(s)


class Scheduler:
    def __init__(self, cfg, telegram, executors: dict[str, Executor] | None = None):
        self.cfg = cfg
        # telegram：{岗位: 客户端}；兼容只传一个客户端（视为项目经理）
        if telegram is None:
            self.tgs = {}
        elif isinstance(telegram, dict):
            self.tgs = dict(telegram)
        else:
            self.tgs = {"pm": telegram}
        self.store = Store(cfg.db_path)
        self.wf = Workflows(cfg)
        commands.setup(cfg.raw["roles"])
        self.executors = executors or {
            name: build(name, cfg.executor(name), cfg.is_simulated(name), self.wf.simulate)
            for name in cfg.raw["executors"]}
        self.owner = int(cfg.tg["owner_user_id"])
        self.group = int(cfg.tg["group_chat_id"])
        self.usernames: dict[str, str] = {}      # 岗位 → bot 用户名
        self.threads: dict[str, threading.Thread] = {}   # 执行账号 → 线程
        self.cancels: dict[int, threading.Event] = {}
        self._last_push: dict[int, float] = {}
        self.stop = threading.Event()
        self._lock_sock = None
        self._log_lock = threading.Lock()

    # ================= 工具 =================
    def log(self, msg: str) -> None:
        line = f"{datetime.now().astimezone().strftime('%Y-%m-%d %H:%M:%S')} {msg}"
        with self._log_lock:
            try:
                print(line, flush=True)
            except Exception:
                pass
            try:
                self.cfg.data_dir.mkdir(parents=True, exist_ok=True)
                with open(self.cfg.data_dir / "agent.log", "a", encoding="utf-8") as f:
                    f.write(line + "\n")
            except OSError:
                pass

    @property
    def bot_username(self):  # 兼容旧调用
        return self.usernames.get("pm")

    @bot_username.setter
    def bot_username(self, value):
        self.usernames["pm"] = value

    def role_label(self, role: str) -> str:
        return self.cfg.role(role)["label"]

    def ex_label(self, name: str) -> str:
        ex = self.executors.get(name)
        return ex.label if ex else name

    def speaker(self, role: str) -> str:
        """实际发消息的机器人：该岗位有 token 就用它，否则用项目经理。"""
        return role if role in self.tgs else ("pm" if "pm" in self.tgs else next(iter(self.tgs), "pm"))

    def stage_role(self, st) -> str:
        return (st["role"] if "role" in st.keys() and st["role"] else None) or "pm"

    def in_cooldown(self, ex: str) -> datetime | None:
        v = self.store.get_meta(f"cooldown:{ex}")
        if not v:
            return None
        until = _ts(v)
        return until if until > datetime.now(timezone.utc) else None

    # ================= 启动与恢复 =================
    def acquire_lock(self) -> None:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.bind(("127.0.0.1", int(self.cfg.get("lock_port", 47231))))
        except OSError:
            raise SystemExit("另一个调度器实例正在运行（单实例锁端口被占用），本次不启动。")
        s.listen(1)
        self._lock_sock = s

    def init_bots(self) -> None:
        for role, client in self.tgs.items():
            try:
                self.usernames[role] = client.get_me().get("username")
            except TelegramError as exc:
                self.log(f"[警告] {role} getMe 失败：{exc}")

    def recover(self) -> int:
        """把上次进程退出时仍在 running 的阶段重新排队（或超过次数后判失败）。"""
        max_attempts = int(self.cfg.get("max_stage_attempts", 3))
        n = 0
        with self.store.tx() as c:
            for st in c.execute("SELECT * FROM stages WHERE status='running'").fetchall():
                t = c.execute("SELECT * FROM tickets WHERE id=?", (st["ticket_id"],)).fetchone()
                bot = self.speaker(self.stage_role(st))
                n += 1
                if st["attempt"] >= max_attempts:
                    c.execute("UPDATE stages SET status='failed', error=?, finished_at=? WHERE id=?",
                              ("调度器多次中断，超过最大尝试次数", now(), st["id"]))
                    c.execute("UPDATE tickets SET state='failed', updated_at=? WHERE id=?", (now(), t["id"]))
                    self.store.enqueue_reply(c, f"{st['id']}:recover-fail", t["chat_id"], t["message_id"],
                                             f"❌ {t['id']} 阶段「{STAGE_LABEL[st['name']]}」因调度器中断已尝试 "
                                             f"{st['attempt']} 次，停止自动重排。可 /retry {t['id']}", t["id"], bot)
                else:
                    c.execute("UPDATE stages SET status='queued', executor='', attempt=attempt+1, started_at=NULL"
                              " WHERE id=?", (st["id"],))
                    c.execute("UPDATE tickets SET state='queued', updated_at=? WHERE id=?", (now(), t["id"]))
                    self.store.enqueue_reply(c, f"{st['id']}:recover:{st['attempt']}", t["chat_id"], t["message_id"],
                                             f"🔁 {t['id']} 调度器重启，阶段「{STAGE_LABEL[st['name']]}」重新排队"
                                             f"（第 {st['attempt'] + 1} 次）", t["id"], bot)
                self.store.event(c, t["id"], "recovered", {"stage": st["id"], "attempt": st["attempt"]})
        return n

    # ================= 收单 =================
    def poll_once(self, timeout: int | None = None, role: str | None = None) -> int:
        total = 0
        for r in ([role] if role else list(self.tgs)):
            offset = self.store.get_meta(f"tg_offset:{r}")
            updates = self.tgs[r].get_updates(int(offset) if offset else None,
                                              timeout if timeout is not None else int(self.cfg.tg.get("poll_timeout", 25)))
            for upd in updates:
                outcome = self.handle_update(upd, r)
                self.log(f"update {r}:{upd.get('update_id')} -> {outcome}")
            total += len(updates)
        return total

    def handle_update(self, upd: dict, bot: str = "pm") -> str:
        uid = int(upd["update_id"])
        key = f"tg_offset:{bot}"
        try:
            with self.store.tx() as c:
                if c.execute("SELECT 1 FROM bot_updates WHERE bot=? AND update_id=?", (bot, uid)).fetchone():
                    outcome = "duplicate_update"
                else:
                    outcome = self._handle(c, upd, bot)
                    c.execute("INSERT INTO bot_updates(bot,update_id,received_at,outcome) VALUES(?,?,?,?)",
                              (bot, uid, now(), outcome))
                cur = int(self.store.get_meta(key) or 0)
                if uid + 1 > cur:
                    self.store.set_meta(c, key, uid + 1)
            return outcome
        except Exception:
            with self.store.tx() as c:
                c.execute("INSERT OR IGNORE INTO bot_updates(bot,update_id,received_at,outcome) VALUES(?,?,?,?)",
                          (bot, uid, now(), "error"))
                self.store.set_meta(c, key, max(uid + 1, int(self.store.get_meta(key) or 0)))
                self.store.event(c, None, "update_error", traceback.format_exc()[-1500:])
            return "error"

    def _handle(self, c, upd: dict, bot: str) -> str:
        if "edited_message" in upd:
            return "edited_ignored"
        if "my_chat_member" in upd:
            m = upd["my_chat_member"]
            self.store.event(c, None, "membership", {"bot": bot, "chat": m.get("chat", {}).get("id"),
                                                    "status": m.get("new_chat_member", {}).get("status")})
            return "membership"
        msg = upd.get("message")
        if not msg:
            return "no_message"
        if msg.get("migrate_to_chat_id"):
            self.store.event(c, None, "group_migrated", {"new_chat_id": msg["migrate_to_chat_id"]})
            return "migrated"
        ok, why = commands.authorized(msg, self.owner, self.group)
        if not ok:
            self.store.event(c, None, "rejected", {"why": why, "bot": bot, "chat": msg.get("chat", {}).get("id"),
                                                  "from": (msg.get("from") or {}).get("id")})
            return f"unauthorized:{why}"
        chat, mid = msg["chat"]["id"], msg["message_id"]
        # 同一条消息可能被几个机器人同时收到，只处理一次
        if c.execute("INSERT OR IGNORE INTO seen_messages(chat_id,message_id,bot,at) VALUES(?,?,?,?)",
                     (chat, mid, bot, now())).rowcount == 0:
            return "duplicate_message"
        text = msg.get("text") or msg.get("caption") or ""
        names = {r: u for r, u in self.usernames.items() if u}
        addressed = commands.addressed_role(msg, names)
        mentioned = any(n.lower() in {u.lower() for u in names.values()} for n in commands.MENTION_RE.findall(text))
        cmd = commands.parse(text, set(names.values()) or None)
        if cmd is None:
            plain = commands.strip_mentions(text, names)
            if mentioned and addressed:
                if not plain:
                    cmd = commands.Command("help", "")
                else:
                    cmd = commands.Command(commands.ROLE_TASK[addressed], plain,
                                           (commands.TICKET_RE.search(plain) or [None])[0])
            elif addressed:
                self.store.enqueue_reply(c, f"m{mid}:hint", chat, mid,
                                         "要派新单请 @我 并写需求；其他见 /help",
                                         None, self.speaker(addressed))
                return "hint"
            else:
                return "not_command"
        role = ("pm" if cmd.kind == "review" else cmd.kind if cmd.kind in self.cfg.raw["roles"] else None) \
            or addressed or bot
        speaker = self.speaker(role)
        reply = lambda key, body, tid=None: self.store.enqueue_reply(c, key, chat, mid, body, tid, speaker)
        if cmd.kind == "help":
            reply(f"m{mid}:help", commands.HELP)
            return "help"
        if cmd.kind in commands.ROBOTS:
            return self._new_ticket(c, msg, cmd, reply)
        if cmd.kind == "status":
            reply(f"m{mid}:status", self.status_text(cmd.ticket_id))
            return "status"
        if cmd.kind == "retry":
            return self._retry(c, msg, cmd, reply)
        if cmd.kind == "cancel":
            return self._cancel(c, msg, cmd, reply)
        return "unknown"

    def _new_ticket(self, c, msg, cmd, reply) -> str:
        chat, mid = msg["chat"]["id"], msg["message_id"]
        if not cmd.arg:
            reply(f"m{mid}:usage", "请在后面写明任务内容，例如：@项目经理 做一期……")
            return "empty"
        if c.execute("SELECT 1 FROM tickets WHERE chat_id=? AND message_id=?", (chat, mid)).fetchone():
            return "duplicate_message"
        h = commands.text_hash(cmd.kind, cmd.arg)
        since = (datetime.now(timezone.utc) - timedelta(minutes=int(self.cfg.get("duplicate_window_minutes", 1440)))
                 ).isoformat(timespec="seconds")
        q = ",".join("?" * len(ACTIVE_TICKET_STATES))
        dup = c.execute(f"SELECT * FROM tickets WHERE text_hash=? AND (state IN ({q}) OR created_at>=?)"
                        " ORDER BY seq DESC LIMIT 1", (h, *ACTIVE_TICKET_STATES, since)).fetchone()
        if dup:
            reply(f"m{mid}:dup", f"⚠️ 重复派单：与 {dup['id']}（{STATE_LABEL.get(dup['state'], dup['state'])}）内容相同，"
                                 f"本条不执行。确需重做请用 /retry {dup['id']}，或改写任务内容后再发。", dup["id"])
            self.store.event(c, dup["id"], "duplicate_text", {"message_id": mid})
            return "duplicate_text"
        tid, seq = self.store.next_ticket_id(c)
        c.execute("INSERT INTO tickets(id,seq,chat_id,message_id,user_id,robot,text,text_hash,state,created_at,updated_at)"
                  " VALUES(?,?,?,?,?,?,?,?,'queued',?,?)",
                  (tid, seq, chat, mid, msg["from"]["id"], cmd.kind, cmd.arg, h, now(), now()))
        stage, role = self.wf.first_stage(cmd.kind)
        self.store.add_stage(c, tid, stage, role, 1)
        self.store.event(c, tid, "created", {"robot": cmd.kind})
        L = self.role_label
        if cmd.kind == "pm":
            flow = f"{L('pm')} 拆解需求，派给 {' / '.join(L(r) for r in self.cfg.workers)}"
        elif cmd.kind == "review":
            flow = f"{L('pm')} 独立质检"
        else:
            flow = f"{L(cmd.kind)} 干活 → {L('pm')} 独立质检（不合格自动返工）"
        pool = " → ".join(self.ex_label(e) for e in self.cfg.role(role)["pool"])
        reply(f"{tid}:accepted", f"✅ 已受理 {tid}｜{commands.ROBOTS[cmd.kind]}\n流程：{flow}\n"
                                 f"首个阶段：{STAGE_LABEL[stage]}（{L(role)}，账号池：{pool}）", tid)
        return "created"

    def _find_ticket(self, c, msg, cmd):
        if cmd.ticket_id:
            return c.execute("SELECT * FROM tickets WHERE id=?", (cmd.ticket_id,)).fetchone()
        rt = msg.get("reply_to_message")
        if rt:
            return self.store.ticket_by_message(msg["chat"]["id"], rt["message_id"])
        return None

    def _retry(self, c, msg, cmd, reply) -> str:
        mid = msg["message_id"]
        t = self._find_ticket(c, msg, cmd)
        if not t:
            reply(f"m{mid}:retry-none", "用法：/retry T-0001（或回复工单消息发 /retry）")
            return "retry_no_ticket"
        if t["state"] == "review_rejected":
            self.store.add_stage(c, t["id"], "work", t["robot"], t["round"] + 1)
            c.execute("UPDATE tickets SET round=round+1 WHERE id=?", (t["id"],))
            reply(f"m{mid}:retry", f"🔁 {t['id']} 按质检意见交给 {self.role_label(t['robot'])} 返工（第 {t['round'] + 1} 轮）", t["id"])
            return "retry_rework"
        if t["state"] not in ("failed", "cancelled"):
            reply(f"m{mid}:retry-state", f"ℹ️ {t['id']} 当前「{STATE_LABEL.get(t['state'], t['state'])}」，无需重试", t["id"])
            return "retry_not_needed"
        last = c.execute("SELECT * FROM stages WHERE ticket_id=? ORDER BY id DESC LIMIT 1", (t["id"],)).fetchone()
        self.store.add_stage(c, t["id"], last["name"], self.stage_role(last), last["round"], last["attempt"] + 1)
        reply(f"m{mid}:retry", f"🔁 {t['id']} 重新排队：{STAGE_LABEL[last['name']]}（{self.role_label(self.stage_role(last))}）",
              t["id"])
        return "retry"

    def _cancel(self, c, msg, cmd, reply) -> str:
        mid = msg["message_id"]
        t = self._find_ticket(c, msg, cmd)
        if not t:
            reply(f"m{mid}:cancel-none", "用法：/cancel T-0001")
            return "cancel_no_ticket"
        c.execute("UPDATE tickets SET state='cancelled', updated_at=? WHERE id=?", (now(), t["id"]))
        for st in c.execute("SELECT id,status FROM stages WHERE ticket_id=? AND status IN ('queued','running')",
                            (t["id"],)).fetchall():
            if st["status"] == "queued":
                c.execute("UPDATE stages SET status='cancelled', finished_at=? WHERE id=?", (now(), st["id"]))
            elif st["id"] in self.cancels:
                self.cancels[st["id"]].set()
        reply(f"m{mid}:cancel", f"🛑 {t['id']} 已取消，正在运行的进程会被结束", t["id"])
        return "cancelled"

    # ================= 分派与执行 =================
    def dispatch(self) -> list[int]:
        started = []
        busy = {ex for ex, th in self.threads.items() if th.is_alive()}
        queued = self.store.conn().execute(
            "SELECT s.id FROM stages s JOIN tickets t ON t.id=s.ticket_id WHERE s.status='queued'"
            " AND t.state!='cancelled' ORDER BY s.id").fetchall()
        for row in queued:
            st = self.store.stage(row["id"])
            role = self.stage_role(st)
            pool = [e for e in self.cfg.role(role)["pool"] if e in self.executors]
            cooling = {e: self.in_cooldown(e) for e in pool}
            free = [e for e in pool if e not in busy and not cooling[e]]
            if not free:
                if pool and all(cooling.values()):
                    t = self.store.ticket(st["ticket_id"])
                    soonest = min(cooling.values()).astimezone().strftime("%H:%M")
                    with self.store.tx() as c:
                        self.store.enqueue_reply(
                            c, f"{st['id']}:allquota:{st['attempt']}", t["chat_id"], t["message_id"],
                            f"⏸️ {t['id']} 「{STAGE_LABEL[st['name']]}」：{self.role_label(role)} 的账号池都在额度冷却中，"
                            f"排队等待（最早约 {soonest} 恢复后自动继续）", t["id"], self.speaker(role))
                continue
            ex = free[0]
            with self.store.tx() as c:
                claimed = c.execute("UPDATE stages SET status='running', executor=?, simulated=?, started_at=?,"
                                    " last_heartbeat=? WHERE id=? AND status='queued'",
                                    (ex, int(self.executors[ex].simulated), now(), now(), st["id"])).rowcount
                if not claimed:
                    continue
                t = c.execute("SELECT * FROM tickets WHERE id=?", (st["ticket_id"],)).fetchone()
                c.execute("UPDATE tickets SET state='running', stage=?, updated_at=? WHERE id=?",
                          (st["name"], now(), t["id"]))
                note = ""
                if ex != pool[0]:
                    why = "额度冷却中" if cooling.get(pool[0]) else "正忙"
                    note = f"（{self.ex_label(pool[0])} {why}，由 {self.ex_label(ex)} 接手）"
                sim = "【模拟】" if self.executors[ex].simulated else ""
                self.store.enqueue_reply(c, f"{st['id']}:start:{st['attempt']}", t["chat_id"], t["message_id"],
                                         f"▶️ {sim}{t['id']} 开始「{STAGE_LABEL[st['name']]}」第 {st['round']} 轮｜"
                                         f"执行账号：{self.ex_label(ex)}{note}", t["id"], self.speaker(role))
            busy.add(ex)
            self.cancels[st["id"]] = threading.Event()
            th = threading.Thread(target=self._run_stage, args=(st["id"],), name=f"lane-{ex}", daemon=True)
            self.threads[ex] = th
            th.start()
            self.log(f"dispatch {st['ticket_id']} stage={st['name']} role={role} r{st['round']} a{st['attempt']} "
                     f"executor={ex}{' [SIMULATED]' if self.executors[ex].simulated else ''}")
            started.append(st["id"])
        return started

    def _run_stage(self, stage_id: int) -> None:
        st = self.store.stage(stage_id)
        t = self.store.ticket(st["ticket_id"])
        ex_name = st["executor"]
        ex = self.executors[ex_name]
        workdir = None
        try:
            workdir = self.wf.workdir(t["id"], st["name"], st["round"], st["attempt"])
            with self.store.tx() as c:
                c.execute("UPDATE stages SET workdir=? WHERE id=?", (str(workdir), stage_id))
            if ex_name in (self.cfg.get("force_quota_limited") or []):
                outcome = Outcome(False, "【强制模拟】额度受限（force_quota_limited 配置）", quota_limited=True,
                                  detail={"forced": True})
                v = Validation(False, outcome.error)
            else:
                job = self.wf.prepare(self.store, t, st, workdir)
                job.cancel = self.cancels.get(stage_id) or threading.Event()
                outcome = ex.run(job)
                v = self.wf.validate(t, st, workdir) if outcome.ok else Validation(False, outcome.error)
        except Exception as exc:  # 准备/执行/核验中的任何异常都转成失败回复
            outcome = Outcome(False, f"{type(exc).__name__}: {exc}", simulated=ex.simulated)
            v = Validation(False, outcome.error)
            if workdir:
                (workdir / "_logs").mkdir(exist_ok=True)
                (workdir / "_logs" / "scheduler_error.txt").write_text(traceback.format_exc(), encoding="utf-8")
        outcome.simulated = outcome.simulated or ex.simulated
        if self.stop.is_set():
            # 调度器正在退出：保持 running，下次启动由 recover() 重新排队，不算失败
            return
        if outcome.quota_limited:
            self.quota_requeue(stage_id, ex_name, outcome)
            return
        self.complete(stage_id, outcome, v)

    def quota_requeue(self, stage_id: int, ex_name: str, outcome: Outcome) -> None:
        minutes = int(self.cfg.get("quota_cooldown_minutes", 60))
        until = datetime.now(timezone.utc) + timedelta(minutes=minutes)
        with self.store.tx() as c:
            st = c.execute("SELECT * FROM stages WHERE id=?", (stage_id,)).fetchone()
            t = c.execute("SELECT * FROM tickets WHERE id=?", (st["ticket_id"],)).fetchone()
            self.store.set_meta(c, f"cooldown:{ex_name}", until.isoformat(timespec="seconds"))
            role = self.stage_role(st)
            if t["state"] == "cancelled":
                c.execute("UPDATE stages SET status='cancelled', finished_at=? WHERE id=?", (now(), stage_id))
                return
            if st["attempt"] >= 10:
                c.execute("UPDATE stages SET status='failed', error=?, finished_at=? WHERE id=?",
                          ("账号池反复额度受限", now(), stage_id))
                c.execute("UPDATE tickets SET state='failed', updated_at=? WHERE id=?", (now(), t["id"]))
                self.store.enqueue_reply(c, f"{stage_id}:quota-fail", t["chat_id"], t["message_id"],
                                         f"❌ {t['id']} 账号池反复额度受限，已停止。额度恢复后 /retry {t['id']}",
                                         t["id"], self.speaker(role))
                return
            c.execute("UPDATE stages SET status='queued', executor='', attempt=attempt+1, error=?, started_at=NULL"
                      " WHERE id=?", (outcome.error, stage_id))
            c.execute("UPDATE tickets SET state='queued', updated_at=? WHERE id=?", (now(), t["id"]))
            others = [e for e in self.cfg.role(role)["pool"] if e != ex_name]
            nxt = f"改由 {'、'.join(self.ex_label(e) for e in others)} 接手" if others else "排队等待额度恢复"
            self.store.enqueue_reply(c, f"{stage_id}:quota:{st['attempt']}", t["chat_id"], t["message_id"],
                                     f"⚠️ {t['id']} 「{STAGE_LABEL[st['name']]}」：{self.ex_label(ex_name)} 订阅额度受限，"
                                     f"冷却到 {until.astimezone().strftime('%H:%M')}；{nxt}。\n原因：{outcome.error}",
                                     t["id"], self.speaker(role))
            self.store.event(c, t["id"], "quota_limited", {"executor": ex_name, "until": until.isoformat()})
        self.log(f"quota {st['ticket_id']} stage={st['name']} executor={ex_name} cooldown_until={until.isoformat()}")

    def complete(self, stage_id: int, outcome: Outcome, v: Validation) -> None:
        with self.store.tx() as c:
            st = c.execute("SELECT * FROM stages WHERE id=?", (stage_id,)).fetchone()
            t = c.execute("SELECT * FROM tickets WHERE id=?", (st["ticket_id"],)).fetchone()
            result = {"validation": v.__dict__, "detail": outcome.detail, "simulated": outcome.simulated}
            status = "ok" if v.ok else ("cancelled" if t["state"] == "cancelled" else "failed")
            c.execute("UPDATE stages SET status=?, result=?, error=?, finished_at=?, simulated=? WHERE id=?",
                      (status, json.dumps(result, ensure_ascii=False, default=str), v.error, now(),
                       int(outcome.simulated), stage_id))
            self.store.event(c, t["id"], f"stage_{status}", {"stage": st["name"], "error": v.error})
            self.log(f"complete {t['id']} stage={st['name']} executor={st['executor']} status={status} "
                     f"verdict={v.verdict} simulated={outcome.simulated} error={v.error}")
            if t["state"] == "cancelled":
                return
            role = self.stage_role(st)
            L = self.role_label
            sim = "【模拟】" if outcome.simulated else ""
            key = f"{stage_id}:end:{st['attempt']}"
            send = lambda body, r=role, k=key: self.store.enqueue_reply(
                c, k, t["chat_id"], t["message_id"], body, t["id"], self.speaker(r))
            label = STAGE_LABEL[st["name"]]
            who = f"{L(role)}｜{self.ex_label(st['executor'])}"
            if not v.ok:
                c.execute("UPDATE tickets SET state='failed', updated_at=? WHERE id=?", (now(), t["id"]))
                send(f"❌ {sim}{t['id']} 「{label}」失败（{who}）\n原因：{v.error}\n工作目录：{st['workdir']}\n"
                     f"处理后可 /retry {t['id']}")
                return
            arts = "\n".join(f"· {a['rel']}（{a['bytes']} B，sha256 {a['sha256'][:12]}）" for a in v.artifacts[:8])
            warn = ("\n⚠️ " + "；".join(v.warnings)) if v.warnings else ""
            head = (f"✅ {sim}{t['id']} 「{label}」完成（{who}）：{v.summary[:300]}\n产物：\n{arts or '（无）'}{warn}"
                    f"\n目录：{st['workdir']}")
            name = st["name"]
            if name == "triage":
                route, brief = v.facts.get("route"), v.facts.get("brief", "")
                if route == "clarify":
                    c.execute("UPDATE tickets SET state='needs_info', updated_at=? WHERE id=?", (now(), t["id"]))
                    send(f"❓ {sim}{t['id']} {L('pm')} 需要你补充：{v.facts.get('question') or v.summary}\n"
                         "补充后请重新 @项目经理 发完整需求。")
                    return
                first, frole = self.wf.first_stage(route)
                c.execute("UPDATE tickets SET robot=?, brief=?, updated_at=? WHERE id=?", (route, brief, now(), t["id"]))
                self.store.add_stage(c, t["id"], first, frole, st["round"])
                send(f"📋 {sim}{t['id']} {L('pm')} 拆解完成 → 交给 {L(frole)}\n任务说明：{brief}\n"
                     f"（{v.summary[:200]}）")
            elif name == "work":
                send(head)
                self.store.add_stage(c, t["id"], "review", "pm", st["round"])
            elif name == "review":
                verdict_line = f"\n质检结论：{v.verdict}" + ("\n" + "\n".join(f"- {r}" for r in v.reasons) if v.reasons else "")
                worker = t["robot"] if t["robot"] in self.cfg.workers else None
                if not worker or v.verdict == "PASS":
                    c.execute("UPDATE tickets SET state='done', updated_at=? WHERE id=?", (now(), t["id"]))
                    send(head + verdict_line + "\n\n✅ 工单完成。产物在上面的目录里，用之前请自己再看一遍。")
                elif st["round"] <= int(self.cfg.get("max_review_rounds", 1)):
                    c.execute("UPDATE tickets SET round=?, updated_at=? WHERE id=?", (st["round"] + 1, now(), t["id"]))
                    self.store.add_stage(c, t["id"], "work", worker, st["round"] + 1)
                    send(head + verdict_line + f"\n\n↩️ 自动退回 {L(worker)} 按质检意见返工（第 {st['round'] + 1} 轮）")
                else:
                    c.execute("UPDATE tickets SET state='review_rejected', updated_at=? WHERE id=?", (now(), t["id"]))
                    send(head + verdict_line + f"\n\n已达自动返工上限。看过意见后可 /retry {t['id']} 再返工一轮，或 /cancel")

    # ================= 进度：岗位进度转发 + 心跳 =================
    def heartbeat(self) -> None:
        minutes = float(self.cfg.get("heartbeat_minutes", 10))
        push_gap = float(self.cfg.get("progress_push_seconds", 120))
        rows = self.store.conn().execute(
            "SELECT s.*, t.chat_id, t.message_id FROM stages s JOIN tickets t ON t.id=s.ticket_id"
            " WHERE s.status='running'").fetchall()
        for st in rows:
            bot = self.speaker(self.stage_role(st))
            label = STAGE_LABEL[st["name"]]
            lines = []
            if st["workdir"]:
                prog = Path(st["workdir"]) / "progress.log"
                if prog.exists():
                    lines = [x for x in prog.read_text(encoding="utf-8", errors="replace").splitlines() if x.strip()]
            pkey = f"prog:{st['id']}:{st['attempt']}"
            posted = int(self.store.get_meta(pkey) or 0)
            if len(lines) > posted and push_gap >= 0 and \
                    time.monotonic() - self._last_push.get(st["id"], -1e9) >= push_gap:
                new = lines[posted:]
                with self.store.tx() as c:
                    self.store.enqueue_reply(c, f"{st['id']}:prog:{st['attempt']}:{len(lines)}", st["chat_id"],
                                             st["message_id"], f"📍 {st['ticket_id']} 「{label}」进度：\n"
                                             + "\n".join(x[:200] for x in new[-6:]), st["ticket_id"], bot)
                    self.store.set_meta(c, pkey, len(lines))
                self._last_push[st["id"]] = time.monotonic()
            if minutes <= 0 or not st["started_at"]:
                continue
            elapsed = (datetime.now(timezone.utc) - _ts(st["started_at"])).total_seconds() / 60
            n = int(elapsed // minutes)
            if n < 1:
                continue
            tail = f"\n最近进度：{lines[-1][:200]}" if lines else ""
            with self.store.tx() as c:
                self.store.enqueue_reply(c, f"{st['id']}:hb:{st['attempt']}:{n}", st["chat_id"], st["message_id"],
                                         f"⏳ {st['ticket_id']} 「{label}」已运行 {int(elapsed)} 分钟{tail}",
                                         st["ticket_id"], bot)

    # ================= 发送回复 =================
    def flush_outbox(self) -> int:
        sent = 0
        for row in self.store.pending_outbox():
            bot = row["bot"] if "bot" in row.keys() and row["bot"] in self.tgs else self.speaker(row["bot"] or "pm")
            client = self.tgs.get(bot)
            if client is None:
                continue
            try:
                res = client.send_message(row["chat_id"], row["text"], row["reply_to"])
            except TelegramError as exc:
                self.log(f"send failed outbox={row['id']} bot={bot}: {exc}")
                with self.store.tx() as c:
                    c.execute("UPDATE outbox SET attempts=attempts+1, last_error=? WHERE id=?", (str(exc)[:300], row["id"]))
                if exc.retry_after:
                    time.sleep(min(int(exc.retry_after), 30))
                break
            with self.store.tx() as c:
                c.execute("UPDATE outbox SET sent=1, sent_message_id=? WHERE id=?", (res.get("message_id"), row["id"]))
            sent += 1
        return sent

    # ================= 查询 =================
    def status_text(self, ticket_id: str | None = None) -> str:
        conn = self.store.conn()
        if ticket_id:
            t = self.store.ticket(ticket_id)
            if not t:
                return f"没有工单 {ticket_id}"
            lines = [f"{t['id']}｜{commands.ROBOTS.get(t['robot'], t['robot'])}｜{STATE_LABEL.get(t['state'], t['state'])}",
                     f"任务：{t['text'][:200]}"]
            for s in self.store.stages(t["id"]):
                sim = "【模拟】" if s["simulated"] else ""
                who = self.ex_label(s["executor"]) if s["executor"] else "未分派"
                lines.append(f"· {sim}{STAGE_LABEL[s['name']]}（{self.role_label(self.stage_role(s))}｜{who}）"
                             f" r{s['round']} a{s['attempt']}：{s['status']}"
                             + (f"（{s['error'][:120]}）" if s["error"] else ""))
            return "\n".join(lines)
        q = ",".join("?" * len(ACTIVE_TICKET_STATES))
        rows = conn.execute(f"SELECT * FROM tickets WHERE state IN ({q}) ORDER BY seq", ACTIVE_TICKET_STATES).fetchall()
        cool = [f"{self.ex_label(e)} 冷却到 {u.astimezone().strftime('%H:%M')}"
                for e in self.executors if (u := self.in_cooldown(e))]
        extra = ("\n额度：" + "；".join(cool)) if cool else ""
        if not rows:
            return "当前没有未结工单" + extra
        return "未结工单：\n" + "\n".join(
            f"{r['id']}｜{commands.ROBOTS.get(r['robot'], r['robot'])}｜{STATE_LABEL.get(r['state'], r['state'])}｜{r['text'][:40]}"
            for r in rows) + extra

    # ================= 主循环 =================
    def _poll_loop(self, role: str):
        backoff = 1
        while not self.stop.is_set():
            try:
                self.poll_once(role=role)
                backoff = 1
            except TelegramError as exc:
                self.log(f"poll error {role}: {exc}")
                with self.store.tx() as c:
                    self.store.event(c, None, "poll_error", f"{role}: {str(exc)[:300]}")
                self.stop.wait(min(backoff, 60))
                backoff *= 2

    def run_forever(self):
        self.acquire_lock()
        self.init_bots()
        n = self.recover()
        bots = ", ".join(f"{self.role_label(r)}=@{u}" for r, u in self.usernames.items())
        modes = ", ".join(f"{k}={'模拟' if e.simulated else e.label}" for k, e in self.executors.items())
        self.log(f"调度器已启动（{bots}），恢复 {n} 个中断阶段；执行账号：{modes}。")
        for role in self.tgs:
            threading.Thread(target=self._poll_loop, args=(role,), name=f"poller-{role}", daemon=True).start()
        try:
            stop_file = self.cfg.data_dir / "STOP"
            while not self.stop.is_set():
                if stop_file.exists():
                    try:
                        stop_file.unlink()
                    except OSError:
                        pass
                    self.log("检测到 runtime/STOP，正常停止。")
                    break
                self.dispatch()
                self.heartbeat()
                self.flush_outbox()
                self.stop.wait(1.0)
        except KeyboardInterrupt:
            self.log("收到 Ctrl+C，停止接新任务；正在运行的阶段下次启动会重新排队。")
        finally:
            self.stop.set()
            for ev in self.cancels.values():
                ev.set()
            for th in list(self.threads.values()):
                th.join(timeout=40)
            self.flush_outbox()
