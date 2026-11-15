import json
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path

from tests.fake_telegram import FakeTelegram
from xichu_agent import config as config_mod
from xichu_agent.executors import Executor, Outcome
from xichu_agent.scheduler import Scheduler
from xichu_agent.telegram import TelegramClient

OWNER, GROUP = 42, -100123
STUDIO_FF = Path("C:/Projects/YourStudio/tools/ffmpeg-9.0.2-essentials_build/bin")


def _tool(name):
    found = shutil.which(name)
    if found:
        return found
    exe = STUDIO_FF / f"{name}.exe"
    return str(exe) if exe.is_file() else None


FFMPEG, FFPROBE = _tool("ffmpeg"), _tool("ffprobe")
REAL_VID = "5dtMo0QiJv0"


def make_cfg(tmp: Path, **over):
    raw = {"telegram": {"owner_user_id": OWNER, "group_chat_id": GROUP}, "data_dir": str(tmp / "rt"),
           "ffmpeg_path": FFMPEG, "ffprobe_path": FFPROBE, "site_repo": str(tmp / "no_site"),
           "studio_root": str(tmp / "no_studio"), "simulate": "all", "heartbeat_minutes": 0}
    raw.update(over)
    return config_mod.load(tmp / "absent.json", overrides=raw)


def fake_check(vid):
    return {"ok": True, "status": 200, "title": "合成验收标题", "checked": "fake"}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="xa-test-"))
        self.tg = FakeTelegram()
        self.cfg = make_cfg(self.tmp)
        self.s = self.new_sched()

    def tearDown(self):
        self.s.stop.set()
        self.tg.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def new_sched(self, executors=None, cfg=None):
        client = TelegramClient("TEST", api_base=self.tg.base)
        s = Scheduler(cfg or self.cfg, client, executors=executors, youtube_check=fake_check)
        s.bot_username = "xichu_test_bot"
        return s

    def pump(self, s=None, until=None, rounds=60):
        s = s or self.s
        for _ in range(rounds):
            s.poll_once(timeout=0)
            s.dispatch()
            for th in list(s.threads.values()):
                th.join(timeout=60)
            s.flush_outbox()
            if until and until():
                return
        if until:
            self.fail("pump 条件未满足")

    def ticket(self, tid="T-0001", s=None):
        return (s or self.s).store.ticket(tid)

    def texts(self):
        return [m["text"] for m in self.tg.sent]


@unittest.skipUnless(FFMPEG and FFPROBE, "需要 ffmpeg/ffprobe")
class FlowTests(Base):
    def test_full_video_chain_replies_under_original(self):
        mid, _ = self.tg.push_message("/video 合成验收：做一条 5 秒测试片")
        self.pump(until=lambda: self.ticket() and self.ticket()["state"] == "waiting_publish_link")
        stages = self.s.store.stages("T-0001")
        self.assertEqual([st["name"] for st in stages], ["produce", "review"])
        self.assertTrue(all(st["simulated"] for st in stages))
        bot_msg = self.tg.sent[-1]["message_id"]
        # 回复机器人的进度消息确认发布链接
        self.tg.push_message(f"/published https://youtu.be/{REAL_VID}", reply_to=bot_msg)
        self.pump(until=lambda: self.ticket()["state"] == "draft_ready")
        t = self.ticket()
        self.assertEqual(json.loads(t["youtube_info"])["video_id"], REAL_VID)
        ticket_replies = [m for m in self.tg.sent if "T-0001" in m["text"]]
        self.assertTrue(all(m["reply_to"] == mid for m in ticket_replies), [m["reply_to"] for m in ticket_replies])
        self.assertTrue(any("【模拟】" in x and "官网更新草稿" in x for x in self.texts()))
        draft = Path(self.s.store.latest_stage("T-0001", "site_draft")["workdir"])
        self.assertTrue((draft / "draft/content/episodes/TEST_T-0001/episode.json").is_file())
        # 预览是真实 ffmpeg 合成并经 ffprobe 核验
        prod = json.loads(self.s.store.latest_stage("T-0001", "produce")["result"])
        self.assertTrue(prod["validation"]["facts"]["preview_probe"]["decodable"])
        # 审核方拿到调度器抽的首/中/尾三帧
        frames = Path(self.s.store.latest_stage("T-0001", "review")["workdir"]) / "inputs" / "frames"
        self.assertEqual(len(list(frames.glob("frame_*.png"))), 3)

    def test_review_reject_auto_rework(self):
        self.tg.push_message("/video 模拟驳回 验收返工")
        self.pump(until=lambda: self.ticket() and self.ticket()["state"] == "waiting_publish_link")
        names = [(st["name"], st["round"]) for st in self.s.store.stages("T-0001")]
        self.assertEqual(names, [("produce", 1), ("review", 1), ("produce", 2), ("review", 2)])
        self.assertTrue(any("自动退回" in x for x in self.texts()))

    def test_review_only_and_site_only(self):
        self.tg.push_message("/review 看一下这个合成工单")
        self.tg.push_message("/site 合成：给首页加一个占位草稿")
        self.pump(until=lambda: self.ticket("T-0002") and self.ticket("T-0001")["state"] == "done"
                  and self.ticket("T-0002")["state"] == "done")


class GuardTests(Base):
    def test_only_owner_in_group(self):
        self.tg.push_message("/video 陌生人派单", user_id=7)
        self.tg.push_message("/video 别的群", chat_id=-100999)
        self.tg.push_message("/video 私聊", chat_id=OWNER, chat_type="private")
        self.pump(rounds=2)
        self.assertIsNone(self.ticket())
        self.assertEqual(self.tg.sent, [])

    def test_duplicate_update_and_message(self):
        mid, uid = self.tg.push_message("/site 合成：同一任务")
        self.pump(rounds=1)
        # 同一 update 被重新投递（例如 offset 未确认）
        self.s.handle_update({"update_id": uid, "message": {"message_id": mid}})
        # 同一条消息换了 update_id 再来
        self.tg.push_message("/site 合成：同一任务", message_id=mid)
        # 内容相同的新消息（误触重发）
        self.tg.push_message("/site   合成：同一任务 ")
        self.pump(rounds=3)
        n = self.s.store.conn().execute("SELECT COUNT(*) FROM tickets").fetchone()[0]
        self.assertEqual(n, 1)
        self.assertTrue(any("重复派单" in x for x in self.texts()))
        self.assertEqual(sum("已受理" in x for x in self.texts()), 1)

    def test_edited_message_not_executed(self):
        self.tg.push_message("/site 合成：编辑过的", edited=True)
        self.pump(rounds=1)
        self.assertIsNone(self.ticket())

    def test_published_guards(self):
        mid, _ = self.tg.push_message("/site 合成：不是视频工单")
        self.pump(rounds=1)
        self.tg.push_message("/published https://example.com/x", reply_to=mid)
        self.tg.push_message(f"/published https://youtu.be/{REAL_VID}", reply_to=mid)
        self.pump(rounds=2)
        self.assertTrue(any("没识别到有效的 YouTube" in x for x in self.texts()))
        self.assertTrue(any("质检通过后才接收发布链接" in x for x in self.texts()))


class SlowExec(Executor):
    simulated = True
    label = "【模拟】慢执行器"

    def __init__(self, inner, gate):
        self.inner, self.gate, self.calls = inner, gate, 0

    def run(self, job):
        self.calls += 1
        self.gate.wait(30)
        return self.inner.run(job)


@unittest.skipUnless(FFMPEG and FFPROBE, "需要 ffmpeg/ffprobe")
class RestartTests(Base):
    def test_restart_requeues_running_stage(self):
        gate = threading.Event()
        first = self.new_sched()
        slow = SlowExec(first.executors["codex_a"], gate)
        first.executors["codex_a"] = slow
        self.tg.push_message("/video 合成：重启验收")
        first.poll_once(timeout=0)
        first.dispatch()
        time.sleep(0.5)
        self.assertEqual(first.store.latest_stage("T-0001", "produce")["status"], "running")
        # 模拟进程崩溃：旧实例不再推进，未发送的回复留在库里
        first.stop.set()
        gate.set()
        first.threads["codex_a"].join(10)
        self.assertEqual(first.store.latest_stage("T-0001", "produce")["status"], "running")
        second = self.new_sched()
        self.assertEqual(second.recover(), 1)
        self.pump(s=second, until=lambda: self.ticket(s=second)["state"] == "waiting_publish_link")
        prod = second.store.latest_stage("T-0001", "produce")
        self.assertEqual((prod["status"], prod["attempt"]), ("ok", 2))
        self.assertTrue(any("调度器重启" in x for x in self.texts()))
        self.assertEqual(sum("已受理" in x for x in self.texts()), 1)
        n = second.store.conn().execute("SELECT COUNT(*) FROM tickets").fetchone()[0]
        self.assertEqual(n, 1)


class FailingExec(Executor):
    label = "失败执行器"

    def run(self, job):
        (job.workdir / "result.json").write_text(json.dumps({"status": "fail", "reasons": ["合成失败原因"]}),
                                                 encoding="utf-8")
        return Outcome(True)


@unittest.skipUnless(FFMPEG and FFPROBE, "需要 ffmpeg/ffprobe")
class FailureTests(Base):
    def test_failure_reason_reported_and_retry(self):
        s = self.new_sched()
        good = s.executors["codex_a"]
        s.executors["codex_a"] = FailingExec()
        self.tg.push_message("/site 合成：会失败的任务")
        self.pump(s=s, until=lambda: self.ticket(s=s) and self.ticket(s=s)["state"] == "failed")
        self.assertTrue(any("合成失败原因" in x for x in self.texts()))
        s.executors["codex_a"] = good
        self.tg.push_message("/retry T-0001")
        self.pump(s=s, until=lambda: self.ticket(s=s)["state"] == "done")

    def test_outbox_survives_send_failure(self):
        self.tg.fail_send = 2
        self.tg.push_message("/help")
        self.pump(rounds=4)
        self.assertTrue(any("指挥中心" in x for x in self.texts()))


if __name__ == "__main__":
    unittest.main()


BOTS = {"PM": "pm_bot", "VID": "video_bot", "SITE": "site_bot"}
ROLE_TOKEN = {"pm": "PM", "video": "VID", "site": "SITE"}


@unittest.skipUnless(FFMPEG and FFPROBE, "需要 ffmpeg/ffprobe")
class MultiBotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="xa-mb-"))
        self.tg = FakeTelegram(BOTS)

    def tearDown(self):
        self.tg.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def sched(self, **over):
        cfg = make_cfg(self.tmp, **over)
        clients = {r: TelegramClient(tok, api_base=self.tg.base) for r, tok in ROLE_TOKEN.items()}
        s = Scheduler(cfg, clients, youtube_check=fake_check)
        s.usernames = {r: BOTS[tok] for r, tok in ROLE_TOKEN.items()}
        return s

    def pump(self, s, until, rounds=80):
        for _ in range(rounds):
            s.poll_once(timeout=0)
            s.dispatch()
            for th in list(s.threads.values()):
                th.join(timeout=60)
            s.flush_outbox()
            if until():
                return
        self.fail("pump 条件未满足")

    def by_bot(self, needle):
        return {m["bot"] for m in self.tg.sent if needle in m["text"]}

    def test_mention_routes_to_video_and_replies_by_role(self):
        s = self.sched()
        mid, _ = self.tg.push_message("@video_bot 合成：做一条 5 秒测试片")
        self.pump(s, lambda: s.store.ticket("T-0001") and s.store.ticket("T-0001")["state"] == "waiting_publish_link")
        # 三个机器人都收到同一条消息，只建一张单
        self.assertEqual(s.store.conn().execute("SELECT COUNT(*) FROM tickets").fetchone()[0], 1)
        st = {x["name"]: x for x in s.store.stages("T-0001")}
        self.assertEqual((st["produce"]["role"], st["produce"]["executor"]), ("video", "codex_a"))
        self.assertEqual((st["review"]["role"], st["review"]["executor"]), ("pm", "codex_a"))
        self.assertEqual(self.by_bot("已受理 T-0001"), {"VID"})
        self.assertEqual(self.by_bot("「视频制作」完成"), {"VID"})
        self.assertEqual(self.by_bot("「独立质检」完成"), {"PM"})
        self.assertTrue(all(m["reply_to"] == mid for m in self.tg.sent if "T-0001" in m["text"]))
        # 回复视频制作机器人的消息确认发布 → 官网运维机器人接手并回复
        last = [m for m in self.tg.sent if m["bot"] == "PM"][-1]["message_id"]
        self.tg.push_message(f"/published https://youtu.be/{REAL_VID}", reply_to=last)
        self.pump(s, lambda: s.store.ticket("T-0001")["state"] == "draft_ready")
        self.assertEqual(self.by_bot("收到你确认的发布链接"), {"SITE"})
        self.assertEqual(s.store.latest_stage("T-0001", "site_draft")["executor"], "codex_a")

    def test_pm_triage_routes_to_site(self):
        s = self.sched()
        self.tg.push_message("@pm_bot 帮我准备一下官网系列导航的更新草稿")
        self.pump(s, lambda: s.store.ticket("T-0001") and s.store.ticket("T-0001")["state"] == "done")
        names = [x["name"] for x in s.store.stages("T-0001")]
        self.assertEqual(names, ["triage", "site_ops"])
        self.assertEqual(s.store.ticket("T-0001")["robot"], "site")
        self.assertEqual(self.by_bot("拆解完成"), {"PM"})
        self.assertEqual(self.by_bot("「官网运维草稿」完成"), {"SITE"})

    def test_pm_and_site_failover_from_current_codex(self):
        s = self.sched(force_quota_limited=["codex_a"])
        self.tg.push_message("@pm_bot 帮我准备官网导航更新草稿，测试项目经理备用账号")
        self.pump(s, lambda: s.store.ticket("T-0001") and s.store.ticket("T-0001")["state"] == "done")
        stages = s.store.stages("T-0001")
        self.assertEqual([st["name"] for st in stages], ["triage", "site_ops"])
        self.assertTrue(all(st["executor"] == "codex_b" for st in stages))
        self.assertEqual(stages[0]["attempt"], 2)
        self.assertEqual(set(s.executors), {"codex_a", "codex_b"})

    def test_pm_and_video_share_account_lanes(self):
        s = self.sched()
        self.tg.push_message("@pm_bot 准备官网栏目草稿，检查共享车道")
        self.tg.push_message("@video_bot 合成：检查项目经理和视频制作共用账号池")
        s.poll_once(timeout=0)
        started = s.dispatch()
        self.assertEqual(len(started), 2)
        rows = [s.store.stage(i) for i in started]
        self.assertEqual({st["executor"] for st in rows}, {"codex_a", "codex_b"})
        for th in list(s.threads.values()):
            th.join(timeout=60)

    def test_third_account_can_take_over_all_three_roles(self):
        roles = {r: {"pool": ["codex_a", "codex_b", "codex_c"]} for r in ("pm", "video", "site")}
        s = self.sched(roles=roles, force_quota_limited=["codex_a", "codex_b"])
        self.tg.push_message("@pm_bot 合成：做一条 5 秒测试视频，验收第三个独立账号")
        self.pump(s, lambda: s.store.ticket("T-0001") and s.store.ticket("T-0001")["state"] == "waiting_publish_link")
        self.tg.push_message(f"/published T-0001 https://youtu.be/{REAL_VID}")
        self.pump(s, lambda: s.store.ticket("T-0001")["state"] == "draft_ready")
        stages = s.store.stages("T-0001")
        self.assertEqual([st["name"] for st in stages], ["triage", "produce", "review", "site_draft"])
        self.assertTrue(all(st["executor"] == "codex_c" for st in stages))
        self.assertEqual(stages[0]["attempt"], 3)
        self.assertEqual(set(s.executors), {"codex_a", "codex_b", "codex_c"})

    def test_pm_triage_clarify(self):
        s = self.sched()
        self.tg.push_message("@pm_bot 这个需求我也不清楚")
        self.pump(s, lambda: s.store.ticket("T-0001") and s.store.ticket("T-0001")["state"] == "needs_info")
        self.assertTrue(any("需要你补充" in m["text"] for m in self.tg.sent))

    def test_quota_failover_to_backup_account(self):
        s = self.sched(force_quota_limited=["codex_a"])
        self.tg.push_message("/video 合成：额度切换验收")
        self.pump(s, lambda: s.store.ticket("T-0001") and s.store.ticket("T-0001")["state"] == "waiting_publish_link")
        prod = s.store.latest_stage("T-0001", "produce")
        self.assertEqual((prod["executor"], prod["attempt"], prod["status"]), ("codex_b", 2, "ok"))
        self.assertTrue(s.in_cooldown("codex_a"))
        self.assertTrue(any("Codex 账号 A 订阅额度受限" in m["text"] and m["bot"] == "VID" for m in self.tg.sent))
        self.assertTrue(any("Codex 账号 A 额度冷却中" in m["text"] and "Codex 账号 B 接手）" in m["text"]
                            for m in self.tg.sent))

    def test_all_accounts_cooling_waits(self):
        s = self.sched(force_quota_limited=["codex_a", "codex_b"])
        self.tg.push_message("/site 合成：两个账号都没额度")
        for _ in range(6):
            s.poll_once(timeout=0)
            s.dispatch()
            for th in list(s.threads.values()):
                th.join(timeout=30)
            s.flush_outbox()
        self.assertEqual(s.store.latest_stage("T-0001", "site_ops")["status"], "queued")
        self.assertTrue(any("账号池都在额度冷却中" in m["text"] for m in self.tg.sent))

    def test_two_videos_run_in_parallel_on_both_accounts(self):
        s = self.sched()
        self.tg.push_message("/video 合成：并行一")
        self.tg.push_message("/video 合成：并行二")
        s.poll_once(timeout=0)
        started = s.dispatch()
        self.assertEqual(len(started), 2)
        execs = {s.store.stage(i)["executor"] for i in started}
        self.assertEqual(execs, {"codex_a", "codex_b"})
        for th in list(s.threads.values()):
            th.join(timeout=60)

    def test_plain_reply_to_bot_gets_hint_not_ticket(self):
        s = self.sched()
        self.tg.push_message("/help")
        self.pump(s, lambda: bool(self.tg.sent))
        self.tg.push_message("好的谢谢", reply_to=self.tg.sent[-1]["message_id"])
        self.pump(s, lambda: any("要派新单请" in m["text"] for m in self.tg.sent))
        self.assertIsNone(s.store.ticket("T-0001"))
