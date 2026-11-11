import json
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path

from tests.fake_telegram import FakeTelegram
from agent_team import config as config_mod
from agent_team.executors import Executor, Outcome
from agent_team.scheduler import Scheduler
from agent_team.telegram import TelegramClient

OWNER, GROUP = 42, -100123


def make_cfg(tmp: Path, **over):
    raw = {"telegram": {"owner_user_id": OWNER, "group_chat_id": GROUP}, "data_dir": str(tmp / "rt"),
           "simulate": "all", "heartbeat_minutes": 0, "progress_push_seconds": 0}
    raw.update(over)
    return config_mod.load(tmp / "absent.json", overrides=raw)


def run_until(test, s, until, rounds=60):
    for _ in range(rounds):
        s.poll_once(timeout=0)
        s.dispatch()
        for th in list(s.threads.values()):
            th.join(timeout=60)
        s.heartbeat()
        s.flush_outbox()
        if until and until():
            return
    if until:
        test.fail("pump 条件未满足")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="at-test-"))
        self.tg = FakeTelegram()
        self.cfg = make_cfg(self.tmp)
        self.s = self.new_sched()

    def tearDown(self):
        self.s.stop.set()
        self.tg.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def new_sched(self, executors=None, cfg=None):
        client = TelegramClient("TEST", api_base=self.tg.base)
        s = Scheduler(cfg or self.cfg, client, executors=executors)
        s.bot_username = "team_test_bot"
        return s

    def pump(self, s=None, until=None, rounds=60):
        run_until(self, s or self.s, until, rounds)

    def ticket(self, tid="T-0001", s=None):
        return (s or self.s).store.ticket(tid)

    def texts(self):
        return [m["text"] for m in self.tg.sent]


class FlowTests(Base):
    def test_worker_then_review_replies_under_original(self):
        mid, _ = self.tg.push_message("/writer 写一份本周工作周报")
        self.pump(until=lambda: self.ticket() and self.ticket()["state"] == "done")
        stages = self.s.store.stages("T-0001")
        self.assertEqual([(st["name"], st["role"]) for st in stages], [("work", "writer"), ("review", "pm")])
        self.assertTrue(all(st["simulated"] for st in stages))
        ticket_replies = [m for m in self.tg.sent if "T-0001" in m["text"]]
        self.assertTrue(all(m["reply_to"] == mid for m in ticket_replies), [m["reply_to"] for m in ticket_replies])
        self.assertTrue(any("工单完成" in x for x in self.texts()))
        # 质检方拿到员工的交付
        rv = Path(self.s.store.latest_stage("T-0001", "review")["workdir"])
        self.assertTrue((rv / "inputs" / "work" / "result.md").is_file())
        work = Path(self.s.store.latest_stage("T-0001", "work")["workdir"])
        self.assertTrue((work / "progress.log").is_file())

    def test_review_reject_auto_rework(self):
        self.tg.push_message("/coder 模拟驳回 修一个 bug")
        self.pump(until=lambda: self.ticket() and self.ticket()["state"] == "done")
        names = [(st["name"], st["round"]) for st in self.s.store.stages("T-0001")]
        self.assertEqual(names, [("work", 1), ("review", 1), ("work", 2), ("review", 2)])
        self.assertTrue(any("自动退回" in x for x in self.texts()))
        rework = Path(self.s.store.latest_stage("T-0001", "work")["workdir"])
        self.assertTrue((rework / "inputs" / "review_feedback.md").is_file())

    def test_review_only(self):
        self.tg.push_message("/review 看一下这份方案")
        self.pump(until=lambda: self.ticket() and self.ticket()["state"] == "done")
        self.assertEqual([st["name"] for st in self.s.store.stages("T-0001")], ["review"])

    def test_pm_triage_routes_to_worker(self):
        self.tg.push_message("/pm 让程序员给项目加一个导出功能")
        self.pump(until=lambda: self.ticket() and self.ticket()["state"] == "done")
        self.assertEqual([st["name"] for st in self.s.store.stages("T-0001")], ["triage", "work", "review"])
        self.assertEqual(self.ticket()["robot"], "coder")
        self.assertTrue(any("拆解完成" in x for x in self.texts()))

    def test_pm_triage_clarify(self):
        self.tg.push_message("/pm 这个需求我也不清楚")
        self.pump(until=lambda: self.ticket() and self.ticket()["state"] == "needs_info")
        self.assertTrue(any("需要你补充" in x for x in self.texts()))


class GuardTests(Base):
    def test_only_owner_in_group(self):
        self.tg.push_message("/writer 陌生人派单", user_id=7)
        self.tg.push_message("/writer 别的群", chat_id=-100999)
        self.tg.push_message("/writer 私聊", chat_id=OWNER, chat_type="private")
        self.pump(rounds=2)
        self.assertIsNone(self.ticket())
        self.assertEqual(self.tg.sent, [])

    def test_duplicate_update_and_message(self):
        mid, uid = self.tg.push_message("/writer 同一任务")
        self.pump(rounds=1)
        self.s.handle_update({"update_id": uid, "message": {"message_id": mid}})
        self.tg.push_message("/writer 同一任务", message_id=mid)
        self.tg.push_message("/writer   同一任务 ")
        self.pump(rounds=3)
        n = self.s.store.conn().execute("SELECT COUNT(*) FROM tickets").fetchone()[0]
        self.assertEqual(n, 1)
        self.assertTrue(any("重复派单" in x for x in self.texts()))
        self.assertEqual(sum("已受理" in x for x in self.texts()), 1)

    def test_edited_message_not_executed(self):
        self.tg.push_message("/writer 编辑过的", edited=True)
        self.pump(rounds=1)
        self.assertIsNone(self.ticket())


class SlowExec(Executor):
    simulated = True
    label = "【模拟】慢执行器"

    def __init__(self, inner, gate):
        self.inner, self.gate, self.calls = inner, gate, 0

    def run(self, job):
        self.calls += 1
        self.gate.wait(30)
        return self.inner.run(job)


class RestartTests(Base):
    def test_restart_requeues_running_stage(self):
        gate = threading.Event()
        first = self.new_sched()
        first.executors["codex"] = SlowExec(first.executors["codex"], gate)
        self.tg.push_message("/writer 重启验收")
        first.poll_once(timeout=0)
        first.dispatch()
        time.sleep(0.5)
        self.assertEqual(first.store.latest_stage("T-0001", "work")["status"], "running")
        first.stop.set()
        gate.set()
        first.threads["codex"].join(10)
        self.assertEqual(first.store.latest_stage("T-0001", "work")["status"], "running")
        second = self.new_sched()
        self.assertEqual(second.recover(), 1)
        self.pump(s=second, until=lambda: self.ticket(s=second)["state"] == "done")
        work = second.store.latest_stage("T-0001", "work")
        self.assertEqual((work["status"], work["attempt"]), ("ok", 2))
        self.assertTrue(any("调度器重启" in x for x in self.texts()))
        self.assertEqual(sum("已受理" in x for x in self.texts()), 1)


class FailingExec(Executor):
    label = "失败执行器"

    def run(self, job):
        (job.workdir / "result.json").write_text(json.dumps({"status": "fail", "reasons": ["测试失败原因"]}),
                                                 encoding="utf-8")
        return Outcome(True)


class FailureTests(Base):
    def test_failure_reason_reported_and_retry(self):
        s = self.new_sched()
        good = s.executors["codex"]
        s.executors["codex"] = FailingExec()
        self.tg.push_message("/writer 会失败的任务")
        self.pump(s=s, until=lambda: self.ticket(s=s) and self.ticket(s=s)["state"] == "failed")
        self.assertTrue(any("测试失败原因" in x for x in self.texts()))
        s.executors["codex"] = good
        self.tg.push_message("/retry T-0001")
        self.pump(s=s, until=lambda: self.ticket(s=s)["state"] == "done")

    def test_worker_without_artifacts_fails(self):
        class NoArtifacts(Executor):
            label = "空交付"

            def run(self, job):
                (job.workdir / "result.json").write_text(json.dumps({"status": "ok", "summary": "说做完了"}),
                                                         encoding="utf-8")
                return Outcome(True)
        s = self.new_sched()
        s.executors["codex"] = NoArtifacts()
        self.tg.push_message("/writer 空交付测试")
        self.pump(s=s, until=lambda: self.ticket(s=s) and self.ticket(s=s)["state"] == "failed")
        self.assertTrue(any("没有交付任何产物" in x for x in self.texts()))

    def test_outbox_survives_send_failure(self):
        self.tg.fail_send = 2
        self.tg.push_message("/help")
        self.pump(rounds=4)
        self.assertTrue(any("AI 员工团队" in x for x in self.texts()))


BOTS = {"PM": "pm_bot", "WR": "writer_bot", "CO": "coder_bot"}
ROLE_TOKEN = {"pm": "PM", "writer": "WR", "coder": "CO"}


class MultiBotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="at-mb-"))
        self.tg = FakeTelegram(BOTS)

    def tearDown(self):
        self.tg.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def sched(self, **over):
        cfg = make_cfg(self.tmp, **over)
        clients = {r: TelegramClient(tok, api_base=self.tg.base) for r, tok in ROLE_TOKEN.items()}
        s = Scheduler(cfg, clients)
        s.usernames = {r: BOTS[tok] for r, tok in ROLE_TOKEN.items()}
        return s

    def by_bot(self, needle):
        return {m["bot"] for m in self.tg.sent if needle in m["text"]}

    def test_mention_routes_to_worker_and_replies_by_role(self):
        s = self.sched()
        mid, _ = self.tg.push_message("@writer_bot 写一篇产品更新说明")
        run_until(self, s, lambda: s.store.ticket("T-0001") and s.store.ticket("T-0001")["state"] == "done", 80)
        self.assertEqual(s.store.conn().execute("SELECT COUNT(*) FROM tickets").fetchone()[0], 1)
        st = {x["name"]: x for x in s.store.stages("T-0001")}
        self.assertEqual((st["work"]["role"], st["work"]["executor"]), ("writer", "codex"))
        self.assertEqual((st["review"]["role"], st["review"]["executor"]), ("pm", "claude"))
        self.assertEqual(self.by_bot("已受理 T-0001"), {"WR"})
        self.assertEqual(self.by_bot("「执行任务」完成"), {"WR"})
        self.assertEqual(self.by_bot("「独立质检」完成"), {"PM"})
        self.assertTrue(all(m["reply_to"] == mid for m in self.tg.sent if "T-0001" in m["text"]))

    def test_pm_mention_triage(self):
        s = self.sched()
        self.tg.push_message("@pm_bot 让写手整理一份会议纪要")
        run_until(self, s, lambda: s.store.ticket("T-0001") and s.store.ticket("T-0001")["state"] == "done", 80)
        self.assertEqual(s.store.ticket("T-0001")["robot"], "writer")
        self.assertEqual(self.by_bot("拆解完成"), {"PM"})

    def test_quota_limited_waits_and_tells_when(self):
        s = self.sched(force_quota_limited=["codex"])
        self.tg.push_message("/writer 额度用完时排队")
        for _ in range(6):
            s.poll_once(timeout=0)
            s.dispatch()
            for th in list(s.threads.values()):
                th.join(timeout=30)
            s.flush_outbox()
        self.assertEqual(s.store.latest_stage("T-0001", "work")["status"], "queued")
        self.assertTrue(s.in_cooldown("codex"))
        self.assertTrue(any("额度受限" in m["text"] for m in self.tg.sent))
        self.assertTrue(any("排队等待" in m["text"] for m in self.tg.sent))

    def test_plain_reply_to_bot_gets_hint_not_ticket(self):
        s = self.sched()
        self.tg.push_message("/help")
        run_until(self, s, lambda: bool(self.tg.sent))
        self.tg.push_message("好的谢谢", reply_to=self.tg.sent[-1]["message_id"])
        run_until(self, s, lambda: any("要派新单请" in m["text"] for m in self.tg.sent))
        self.assertIsNone(s.store.ticket("T-0001"))


if __name__ == "__main__":
    unittest.main()
