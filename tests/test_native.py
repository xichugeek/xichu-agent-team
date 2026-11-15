import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from xichu_agent import native
from xichu_agent.executors import Executor, Outcome
from xichu_agent.scheduler import Scheduler
from xichu_agent.workflows import Validation, Workflows
from tests.test_scheduler import make_cfg, OWNER, GROUP


class NativeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.studio = self.root / "studio"
        self.ep = self.studio / "Episodes/025_ai"
        self.ep.mkdir(parents=True)
        self.target = self.ep / "script_locked.md"
        self.target.write_text("本人待核对稿", encoding="utf-8")
        self.cfg = make_cfg(self.root, studio_root=str(self.studio), video_mode="studio", site_mode="workspace")
        self.s = Scheduler(self.cfg, None)

    def tearDown(self):
        for th in self.s.threads.values():
            th.join(3)
        self.s.store.close()
        self.temp.cleanup()

    def message(self, text, mid=1, owner=OWNER):
        return {"update_id": mid, "message": {"message_id": mid, "text": text,
                "from": {"id": owner}, "chat": {"id": GROUP, "type": "group"}}}

    def waiting(self):
        self.s.handle_update(self.message("/video 接续第025期"))
        st = self.s.store.stages("T-0001")[0]
        w = self.s.wf.workdir("T-0001", "produce", 1, 1)
        (w / "output/handoff.md").write_text("请本人审稿", encoding="utf-8")
        pending = [{"gate": "SCRIPT_APPROVAL", "reason": "本人审稿", "targets": [{"path": str(self.target), "sha256": native.digest(self.target)}]}]
        with self.s.store.tx() as c:
            c.execute("UPDATE stages SET executor='codex_a',workdir=? WHERE id=?", (str(w), st["id"]))
        v = Validation(True, summary="稿件可供审查", facts={"native": True, "outcome": "waiting_approval", "pending": pending})
        self.s.complete(st["id"], Outcome(True), v)
        return st, w

    def test_real_gate_waits_without_automatic_next_stage(self):
        self.waiting()
        self.assertEqual(self.s.store.ticket("T-0001")["state"], "waiting_approval")
        self.assertEqual(len(self.s.store.stages("T-0001")), 1)

    def test_native_production_does_not_add_ai_review_by_default(self):
        self.s.handle_update(self.message('/video 交付视频',1))
        st=self.s.store.stages('T-0001')[0]
        w=self.s.wf.workdir('T-0001','produce',1,1)
        (w/'output/handoff.md').write_text('已交付',encoding='utf-8')
        with self.s.store.tx() as c:
            c.execute("update stages set executor='codex_a',workdir=? where id=?",(str(w),st['id']))
        self.s.complete(st['id'],Outcome(True),Validation(True,summary='就绪',facts={'native':True,'outcome':'ready_for_review'}))
        self.assertEqual(self.s.store.ticket('T-0001')['state'],'done')
        self.assertEqual(len(self.s.store.stages('T-0001')),1)
        self.assertEqual(self.s.store.conn().execute('select count(*) from outbox where document is not null').fetchone()[0],0)

    def test_explicit_review_remains_available_in_lightweight_mode(self):
        self.s.handle_update(self.message('/review 核对这份视频',1))
        stages=self.s.store.stages('T-0001')
        self.assertEqual(len(stages),1)
        self.assertEqual(stages[0]['name'],'review')

    def test_owner_approval_is_bound_to_artifact_and_message(self):
        self.waiting()
        self.assertEqual(self.s.handle_update(self.message("/approve T-0001 SCRIPT_APPROVAL", 2)), "continued")
        t = self.s.store.ticket("T-0001")
        item = json.loads(t["continuation"])[0]
        self.assertEqual(item["approval"]["targets"][0]["sha256"], native.digest(self.target))
        self.assertEqual(item["user_id"], OWNER)
        self.assertEqual(item["message_id"], 2)
        self.assertEqual(t["state"], "queued")

    def test_changed_artifact_rejects_old_approval(self):
        self.waiting()
        self.target.write_text("已改变的稿件", encoding="utf-8")
        self.assertEqual(self.s.handle_update(self.message("/approve T-0001 SCRIPT_APPROVAL", 2)), "approve_stale")
        self.assertEqual(len(self.s.store.stages("T-0001")), 1)

    def test_compacted_context_keeps_full_current_approval_separate(self):
        self.waiting()
        self.s.handle_update(self.message('/approve T-0001 SCRIPT_APPROVAL',2))
        t=self.s.store.ticket('T-0001'); st=self.s.store.stages('T-0001')[-1]
        st=dict(st,executor='codex_a')
        original=t['continuation']
        w=self.s.wf.workdir(t['id'],st['name'],1,1)
        with patch('xichu_agent.native.project_profile',return_value={}), patch.object(native,'studio_check',return_value={'returncode':0,'output':'PASS'}):
            self.s.wf.prepare(self.s.store,t,st,w)
        approval=json.loads((w/'inputs/current_owner_approvals.json').read_text(encoding='utf-8'))
        self.assertEqual(len(approval),1)
        self.assertEqual(approval[0]['message_id'],2)
        self.assertEqual(approval[0]['approval']['targets'][0]['sha256'],native.digest(self.target))
        self.assertEqual(self.s.store.ticket(t['id'])['continuation'],original)
        self.assertIsInstance(json.loads((w/'inputs/native_context.json').read_text(encoding='utf-8'))['current_owner_approvals'],dict)
        self.target.write_text('批准后变更',encoding='utf-8')
        second=self.s.wf.workdir(t['id'],st['name'],1,1)
        with patch('xichu_agent.native.project_profile',return_value={}), patch.object(native,'studio_check',return_value={'returncode':0,'output':'PASS'}):
            with self.assertRaisesRegex(RuntimeError,'人工确认的产物已变化'):
                self.s.wf.prepare(self.s.store,t,st,second)

    def test_compacted_context_does_not_reuse_old_approval_as_current(self):
        self.waiting()
        self.s.handle_update(self.message('/approve T-0001 SCRIPT_APPROVAL',2))
        approved=self.s.store.stages('T-0001')[-1]
        with self.s.store.tx() as c:
            c.execute("update stages set status='ok' where id=?",(approved['id'],))
            c.execute("update tickets set state='done' where id='T-0001'")
        self.s.handle_update(self.message('/continue T-0001 只核对文件，不作新批准',3))
        t=self.s.store.ticket('T-0001');st=self.s.store.stages('T-0001')[-1]
        st=dict(st,executor='codex_a')
        w=self.s.wf.workdir(t['id'],st['name'],1,1)
        with patch('xichu_agent.native.project_profile',return_value={}), patch.object(native,'studio_check',return_value={'returncode':0,'output':'PASS'}):
            self.s.wf.prepare(self.s.store,t,st,w)
        self.assertEqual(json.loads((w/'inputs/current_owner_approvals.json').read_text(encoding='utf-8')),[])
        self.assertIn('T-0001:approval:',(w/'inputs/history/records.jsonl').read_text(encoding='utf-8'))

    def test_owner_approved_retry_preserves_original_attestation_not_a_new_signature(self):
        self.waiting()
        self.s.handle_update(self.message('/approve T-0001 SCRIPT_APPROVAL',2))
        approved=self.s.store.stages('T-0001')[-1]
        with self.s.store.tx() as c:
            c.execute("update stages set status='failed' where id=?",(approved['id'],))
            c.execute("update tickets set state='failed' where id='T-0001'")
        self.assertEqual(self.s.handle_update(self.message('/retry T-0001',3)),'retry')
        latest=self.s.store.stages('T-0001')[-1]
        proof=json.loads(self.s.store.ticket('T-0001')['continuation'])[-1]
        self.assertEqual(proof['message_id'],2)
        self.assertEqual(proof['retry_message_id'],3)
        self.assertEqual(proof['stage_id'],latest['id'])
        self.assertEqual(proof['approval']['targets'][0]['sha256'],native.digest(self.target))

    def test_many_owner_continuations_have_independent_recovery_budgets(self):
        self.waiting()
        for mid in range(2,7):
            self.assertEqual(self.s.handle_update(self.message(f'/continue T-0001 补充{mid}',mid)),'continued')
            st=self.s.store.stages('T-0001')[-1]
            self.assertEqual(st['attempt'],1)
            with self.s.store.tx() as c:
                c.execute("update stages set status='ok' where id=?",(st['id'],))
                c.execute("update tickets set state='needs_info' where id='T-0001'")

    def test_other_user_cannot_approve(self):
        self.waiting()
        self.assertEqual(self.s.handle_update(self.message("/approve T-0001 SCRIPT_APPROVAL", 2, OWNER+1)), "unauthorized:not_owner")

    def test_continue_keeps_original_and_prior_delivery(self):
        _, w = self.waiting()
        self.assertEqual(self.s.handle_update(self.message("/continue T-0001 增加工具演示", 2)), "continued")
        self.assertTrue((w / "output/handoff.md").is_file())
        self.assertEqual(self.s.store.ticket("T-0001")["text"], "接续第025期")
        self.assertEqual(len(self.s.store.stages("T-0001")), 2)

    def test_retry_never_deletes_a_previous_workdir(self):
        wf = Workflows(self.cfg)
        a = wf.workdir("T-0001", "produce", 1, 1)
        (a / "output/test.txt").write_text("preserve")
        b = wf.workdir("T-0001", "produce", 1, 1)
        self.assertNotEqual(a, b)
        self.assertTrue((a / "output/test.txt").is_file())

    def test_episode_resolution_does_not_treat_resolution_or_port_as_episode(self):
        self.assertIsNone(native.episode(self.cfg, "端口025，1920x1080"))
        self.assertEqual(native.episode(self.cfg, "检查第25期"), self.ep)
        self.assertEqual(native.episode(self.cfg, "接续 E025"), self.ep)

    def test_native_validation_requires_real_evidence(self):
        w = self.s.wf.workdir("T-0001", "produce", 1, 1)
        (w / "output/handoff.md").write_text("证据", encoding="utf-8")
        v = Validation(True, artifacts=[{}])
        native.validate(self.cfg, {}, "produce", w, {"outcome": "completed", "evidence": [{"path": str(self.root / "missing")}]}, v)
        self.assertFalse(v.ok)

    def test_successful_ssh_receipt_accepts_empty_stderr_without_filling_it(self):
        self.s.handle_update(self.message('/site 只读登录测试',1))
        t=self.s.store.ticket('T-0001');st=self.s.store.stages('T-0001')[0]
        w=self.s.wf.workdir(t['id'],'site_ops',1,1)
        (w/'output/handoff.md').write_text('实际登录成功，错误流为空。',encoding='utf-8')
        (w/'output/attempt.stderr.txt').write_bytes(b'')
        receipt=w/'output/receipt.json';receipt.write_text('{"authenticated":true,"exit_code":0}',encoding='utf-8')
        result={'status':'ok','outcome':'completed','checks':[{'id':'ssh_login','status':'passed','required':True}],
                'artifacts':['output/handoff.md','output/receipt.json','output/attempt.stderr.txt'],
                'evidence':[{'path':str(receipt)}]}
        (w/'result.json').write_text(json.dumps(result),encoding='utf-8')
        v=self.s.wf.validate(t,st,w)
        self.assertTrue(v.ok,v.error)
        self.assertEqual(v.facts['outcome'],'completed')
        self.assertEqual((w/'output/attempt.stderr.txt').read_bytes(),b'')
        self.assertEqual(v.facts['empty_stderr_logs'],['output/attempt.stderr.txt'])
        (w/'output/preview.mp4').write_bytes(b'')
        result['artifacts'].append('output/preview.mp4')
        (w/'result.json').write_text(json.dumps(result),encoding='utf-8')
        failed=self.s.wf.validate(t,st,w)
        self.assertFalse(failed.ok)
        self.assertIn('preview.mp4',failed.error)

    def test_native_validation_detects_new_original_project_failures(self):
        w = self.s.wf.workdir("T-0001", "produce", 1, 1)
        (w / "output/handoff.md").write_text("证据", encoding="utf-8")
        (w / "inputs/native_context.json").write_text(json.dumps({"studio_validation_before": {"returncode": 1, "output": "FAIL\n- pending"}}))
        result = {"outcome": "completed", "episode": "025_ai", "evidence": [{"path": str(self.target)}]}
        v = Validation(True, artifacts=[{}])
        with patch.object(native, "studio_check", return_value={"returncode": 1, "output": "FAIL\n- pending\n- bad script hash"}):
            native.validate(self.cfg, {}, "produce", w, result, v)
        self.assertFalse(v.ok)
        self.assertIn("bad script hash", v.error)

    def test_linked_ticket_evidence_is_limited_to_same_owner_group_and_validated_stage(self):
        self.s.handle_update(self.message('/video 原始任务', 1))
        source = self.s.wf.workdir('T-0001', 'produce', 1, 1)
        proof = source / 'output/proof.txt'
        proof.write_text('真实前轮证据', encoding='utf-8')
        with self.s.store.tx() as c:
            c.execute("update stages set status='partial',workdir=? where ticket_id='T-0001'", (str(source),))
        self.s.handle_update(self.message('/review 复核 T-0001', 2))
        ticket = self.s.store.ticket('T-0002')
        w = self.s.wf.workdir('T-0002', 'review', 1, 1)
        (w / 'output/handoff.md').write_text('核验', encoding='utf-8')
        result = {'outcome':'partial','evidence':[{'path':str(proof)}]}
        def check(t):
            v=Validation(True, artifacts=[{}])
            native.validate(self.cfg, t, 'produce', w, result, v)
            return v
        self.assertTrue(check(ticket).ok)
        for key, value in [('user_id',OWNER+1), ('chat_id',GROUP-1), ('text','未引用工单')]:
            altered=dict(ticket); altered[key]=value
            self.assertFalse(check(altered).ok)
        with self.s.store.tx() as c:
            c.execute("update stages set status='failed' where ticket_id='T-0001'")
        self.assertFalse(check(ticket).ok)

    def test_continuation_can_reference_evidence_from_earlier_stage_of_same_ticket(self):
        first=self.s.wf.workdir('T-0001','produce',1,1)
        proof=first/'output/proof.txt'; proof.write_text('原证据',encoding='utf-8')
        current=self.s.wf.workdir('T-0001','produce',1,1)
        (current/'output/handoff.md').write_text('接续',encoding='utf-8')
        v=Validation(True,artifacts=[{}])
        native.validate(self.cfg, {'id':'T-0001'},'produce',current,
                        {'outcome':'completed','evidence':[{'path':str(proof)}]},v)
        self.assertTrue(v.ok)

    def test_report_delivery_does_not_mark_blocked_operation_complete(self):
        w = self.s.wf.workdir("T-0001", "produce", 1, 1)
        (w / "output/handoff.md").write_text("未通过项目", encoding="utf-8")
        v = Validation(True, artifacts=[{}])
        result = {"outcome": "completed", "checks": [{"id": "剪映实际读取", "status": "blocked", "required": True}],
                  "evidence": [{"path": str(self.target)}]}
        native.validate(self.cfg, {}, "produce", w, result, v)
        self.assertTrue(v.ok)
        self.assertEqual(v.facts["outcome"], "partial")

    def test_native_workspaces_are_serialized_across_accounts(self):
        release = threading.Event()
        class Blocking(Executor):
            label = "测试"
            def run(inner, job):
                release.wait(3)
                return Outcome(False, "测试结束")
        self.s.executors = {name: Blocking() for name in self.cfg.executor_names}
        self.s.handle_update(self.message("/video 检查第025期", 1))
        self.s.handle_update(self.message("/site 核查官网", 2))
        with patch.object(self.s.wf, "prepare", side_effect=lambda *args: type("Job", (), {})()):
            try:
                self.assertEqual(len(self.s.dispatch()), 1)
                self.assertEqual(self.s.dispatch(), [])
            finally:
                release.set()
                for th in self.s.threads.values():
                    th.join(3)

    def test_basic_group_bot_local_message_ids_share_one_ticket_and_correct_reply(self):
        class Client:
            def __init__(inner): inner.sent = []
            def send_message(inner, chat, text, reply_to):
                inner.sent.append(reply_to)
                return {"message_id": 201}
        pm, video = Client(), Client()
        self.s.tgs = {"pm": pm, "video": video}
        self.s.usernames = {"pm": "pm_test_bot", "video": "video_test_bot"}
        a = self.message("/video@video_test_bot 核查第025期", 100)
        a['message']['date'] = 2000000000
        b = json.loads(json.dumps(a)); b['update_id'] = 99; b['message']['message_id'] = 19
        self.assertEqual(self.s.handle_update(a, 'pm'), 'created')
        self.assertEqual(self.s.handle_update(b, 'video'), 'duplicate_message')
        self.s.flush_outbox()
        self.assertEqual(video.sent, [19])
        self.assertEqual(len(self.s.store.conn().execute('SELECT * FROM tickets').fetchall()), 1)

    def test_same_numeric_message_id_in_different_bot_accounts_is_not_a_duplicate(self):
        a = self.message('/video 第025期核查', 100); a['message']['date'] = 2000000000
        b = self.message('/site 官网巡检', 100); b['message']['date'] = 2000000001
        self.assertEqual(self.s.handle_update(a, 'pm'), 'created')
        self.assertEqual(self.s.handle_update(b, 'site'), 'created')
        self.assertEqual(len(self.s.store.conn().execute('SELECT * FROM tickets').fetchall()), 2)


if __name__ == "__main__":
    unittest.main()
