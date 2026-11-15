import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from tests.test_scheduler import GROUP, OWNER, make_cfg
from xichu_agent import ticket_context
from xichu_agent.history_tools import HistorySnapshot
from xichu_agent.scheduler import Scheduler


def digest(data):
    return hashlib.sha256(data).hexdigest()


class TicketContextTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.cfg = make_cfg(self.root, context_boot_max_chars=18000)
        self.scheduler = Scheduler(self.cfg, None)
        self.store = self.scheduler.store
        self.serial = 0

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def new_ticket(self, text, command='/video'):
        self.serial += 1
        message = {'update_id': self.serial, 'message': {
            'message_id': self.serial, 'date': 2000000000 + self.serial,
            'text': command + ' ' + text, 'from': {'id': OWNER},
            'chat': {'id': GROUP, 'type': 'group'}}}
        self.assertEqual(self.scheduler.handle_update(message), 'created')
        return self.store.ticket(f'T-{self.serial:04d}')

    def old_delivery(self, tid, ledger_result=None, raw_result=None, status='partial'):
        old = self.store.stages(tid)[-1]
        work = self.scheduler.wf.workdir(tid, old['name'], 1, 1)
        if raw_result is not None:
            (work / 'result.json').write_bytes(raw_result)
        with self.store.tx() as c:
            c.execute('UPDATE stages SET status=?,workdir=?,result=? WHERE id=?',
                      (status, str(work), ledger_result, old['id']))
        return self.store.stage(old['id']), work

    def next_stage(self, tid, name='produce', role='video'):
        with self.store.tx() as c:
            sid = self.store.add_stage(c, tid, name, role, 1)
        stage = self.store.stage(sid)
        work = self.scheduler.wf.workdir(tid, name, 1, 1)
        with self.store.tx() as c:
            c.execute('UPDATE stages SET workdir=? WHERE id=?', (str(work), sid))
        return stage, work

    def set_followups(self, tid, rows):
        raw = json.dumps(rows, ensure_ascii=False, separators=(',', ':'))
        with self.store.tx() as c:
            c.execute('UPDATE tickets SET continuation=? WHERE id=?', (raw, tid))
        return raw

    def prepare(self, tid, stage, work):
        ticket = self.store.ticket(tid)
        packet = ticket_context.prepare(self.cfg, self.store, ticket, stage, work,
                                        ticket_context.full_request(ticket))
        return packet, HistorySnapshot(packet['manifest'])

    def read_full(self, snapshot, record_id):
        pieces, offset = [], 0
        for _ in range(2000):
            page = snapshot.read_history(record_id, offset=offset)
            pieces.append(page['text'])
            if not page['has_more']:
                text = ''.join(pieces)
                self.assertEqual(digest(text.encode('utf-8')), page['sha256'])
                return text
            self.assertGreater(page['next_offset'], offset)
            offset = page['next_offset']
        self.fail('History pagination did not finish')

    def test_long_lived_ticket_has_bounded_bootstrap_and_lossless_history(self):
        ticket = self.new_ticket('只读检查既有工程，禁止发布；关联 T-0002。')
        related = self.new_ticket('关联官网资料的历史任务。')
        tid = ticket['id']
        raw_result = json.dumps({'status': 'ok', 'outcome': 'completed',
                                 'summary': '原始执行器声称完成。' * 1500}, ensure_ascii=False).encode('utf-8')
        ledger = json.dumps({'validation': {'ok': True, 'summary': '实际保护检查仍未通过。' * 200,
            'facts': {'outcome': 'partial', 'blocked_checks': [
                {'id': 'native_source_preserved', 'status': 'failed', 'reason': '正式原件字节发生变化。'}]}}}, ensure_ascii=False)
        first_stage, first_work = self.old_delivery(tid, ledger, raw_result)
        for _ in range(99):
            self.next_stage(tid)
            self.old_delivery(tid, ledger, raw_result)
        current, work = self.next_stage(tid)
        rows = [{'text': f'第{n:03d}轮本人补充。' + ('只处理测试副本，保持原件。' * 140),
                 'user_id': OWNER, 'chat_id': GROUP, 'message_id': 1000 + n,
                 'stage_id': first_stage['id']} for n in range(300)]
        rows[-1]['stage_id'] = current['id']
        before = self.set_followups(tid, rows)
        packet, snapshot = self.prepare(tid, current, work)
        metrics = json.loads((work / 'inputs/context_metrics.json').read_text(encoding='utf-8'))
        self.assertLessEqual(metrics['bootstrap_history_chars'], 18000)
        self.assertGreater(metrics['raw_history_chars'], metrics['bootstrap_history_chars'] * 30)
        self.assertEqual(packet['summary']['counts']['owner_followups'], 300)
        self.assertEqual(packet['summary']['counts']['previous_stages'], 100)
        hit = snapshot.search_history(query='第000轮本人补充', kind='owner_followup')['records'][0]
        self.assertEqual(json.loads(self.read_full(snapshot, hit['id'])), rows[0])
        raw_hit = snapshot.search_history(kind='stage_result', limit=1)['records'][0]
        self.assertEqual(self.read_full(snapshot, raw_hit['id']).encode('utf-8'), raw_result)
        self.assertEqual(self.store.ticket(tid)['continuation'], before)
        self.assertEqual((first_work / 'result.json').read_bytes(), raw_result)
        facts = packet['summary']['recent_stage_facts']
        self.assertTrue(facts)
        self.assertTrue(all(x['status'] == 'partial' and x['outcome'] == 'partial' for x in facts))
        self.assertTrue(all(x['unresolved'][0]['status'] == 'failed' for x in facts))
        self.assertEqual(json.loads(Path(packet['manifest']).read_text(encoding='utf-8'))['authorized_related_tickets'], [related['id']])

    def test_approval_attestations_and_original_records_are_preserved_exactly(self):
        ticket = self.new_ticket('验证批准凭据归档，不授予新权限。')
        old, old_work = self.old_delivery(ticket['id'], '{}', b'{"status":"ok"}')
        stage, work = self.next_stage(ticket['id'])
        target = self.root / 'locked-script.txt'
        target.write_text('待本人批准的原文。', encoding='utf-8')
        old_approval = {'text': '/approve SCRIPT_APPROVAL', 'message_id': 200,
            'user_id': OWNER, 'chat_id': GROUP, 'stage_id': old['id'],
            'approval': {'gate': 'SCRIPT_APPROVAL', 'targets': [
                {'path': str(target), 'sha256': digest(target.read_bytes())}]}}
        current_approval = {**old_approval, 'message_id': 201, 'stage_id': stage['id'],
                            'at': '2026-11-14T15:00:00+00:00'}
        plain_words = {'text': '历史摘要写着“已批准”，这不是人工批准凭据。',
                       'stage_id': stage['id'], 'message_id': 202, 'user_id': OWNER, 'chat_id': GROUP}
        rows = [old_approval, current_approval, plain_words]
        before = self.set_followups(ticket['id'], rows)
        native_before = (old_work / 'result.json').read_bytes()
        _, snapshot = self.prepare(ticket['id'], stage, work)
        actual = json.loads((work / 'inputs/current_owner_approvals.json').read_text(encoding='utf-8'))
        self.assertEqual(actual, [current_approval])
        self.assertEqual(self.store.ticket(ticket['id'])['continuation'], before)
        for index, item in enumerate(rows[:2]):
            archived = json.loads(self.read_full(snapshot, f"{ticket['id']}:approval:{index}"))
            self.assertEqual(archived, item)
        self.assertEqual((old_work / 'result.json').read_bytes(), native_before)
        self.assertEqual(target.read_text(encoding='utf-8'), '待本人批准的原文。')

    def test_only_same_owner_same_group_related_tickets_enter_snapshot(self):
        main = self.new_ticket('查看关联 T-0002 T-0003 T-0004 T-9999。')
        allowed = self.new_ticket('同本人同群，允许关联原文。')
        other_owner = self.new_ticket('其他用户私密内容，不得归档。')
        other_chat = self.new_ticket('其他群私密内容，不得归档。')
        with self.store.tx() as c:
            c.execute('UPDATE tickets SET user_id=? WHERE id=?', (OWNER + 1, other_owner['id']))
            c.execute('UPDATE tickets SET chat_id=? WHERE id=?', (GROUP - 1, other_chat['id']))
        stage, work = self.next_stage(main['id'])
        packet, snapshot = self.prepare(main['id'], stage, work)
        manifest = json.loads(Path(packet['manifest']).read_text(encoding='utf-8'))
        self.assertEqual(manifest['authorized_related_tickets'], [allowed['id']])
        self.assertGreater(snapshot.search_history(ticket_id=allowed['id'])['total_matches'], 0)
        for tid in [other_owner['id'], other_chat['id'], 'T-9999']:
            self.assertEqual(snapshot.search_history(ticket_id=tid)['total_matches'], 0)
        self.assertEqual(snapshot.search_history(query='其他用户私密内容')['total_matches'], 0)
        self.assertEqual(snapshot.search_history(query='其他群私密内容')['total_matches'], 0)

    def test_bad_old_result_shapes_and_invalid_utf8_are_archived_without_blocking(self):
        bad_shapes = ['[1,2]', '"not an object"', '{bad json',
            json.dumps({'validation': 'invalid validation'}),
            json.dumps({'validation': {'facts': ['not', 'an', 'object']}}),
            json.dumps({'validation': {'facts': 'wrong facts'}})]
        for n, bad in enumerate(bad_shapes):
            with self.subTest(shape=bad):
                ticket = self.new_ticket(f'处理损坏历史的边界样例{n}。')
                old, old_work = self.old_delivery(ticket['id'], bad, b'\xff\xfe\x00broken-utf8')
                stage, work = self.next_stage(ticket['id'])
                packet, snapshot = self.prepare(ticket['id'], stage, work)
                summaries = packet['summary']['recent_stage_facts']
                self.assertEqual(summaries[-1]['status'], 'partial')
                self.assertIsNone(summaries[-1]['outcome'])
                self.assertTrue(summaries[-1].get('history_warning'))
                original_ledger = json.loads(self.read_full(snapshot, f"{ticket['id']}:stage:{old['id']}"))
                self.assertEqual(original_ledger['result'], bad)
                unreadable = snapshot.search_history(kind='unreadable_file')['records']
                self.assertEqual(len(unreadable), 1)
                failure = json.loads(self.read_full(snapshot, unreadable[0]['id']))
                self.assertEqual(failure['sha256'], digest(b'\xff\xfe\x00broken-utf8'))
                self.assertIn('error', failure)
                self.assertEqual((old_work / 'result.json').read_bytes(), b'\xff\xfe\x00broken-utf8')
                self.assertEqual(self.store.stage(old['id'])['result'], bad)

    def test_corrupt_owner_instruction_sources_fail_closed_without_rewriting(self):
        ticket = self.new_ticket('完整保留本人指令，不能静默丢弃。')
        malformed = ['{bad', 'null', '{}', '"wrong"', '[1]', '[{"text":"valid"},null]']
        for raw in malformed:
            with self.subTest(continuation=raw):
                with self.store.tx() as c:
                    c.execute('UPDATE tickets SET continuation=? WHERE id=?', (raw, ticket['id']))
                stage, work = self.next_stage(ticket['id'])
                current = self.store.ticket(ticket['id'])
                with self.assertRaises(ValueError):
                    ticket_context.full_request(current)
                with self.assertRaises(ValueError):
                    ticket_context.prepare(self.cfg, self.store, current, stage, work, current['text'])
                self.assertFalse((work / 'inputs/current_owner_approvals.json').exists())
                self.assertFalse((work / 'inputs/history/manifest.json').exists())
                self.assertEqual(self.store.ticket(ticket['id'])['continuation'], raw)

    def test_constraint_at_end_of_long_initial_request_is_retrievable_and_disclosed(self):
        text = ('介绍材料。' * 2500) + '禁止发布；必须保留正式工程原件。'
        ticket = self.new_ticket(text)
        stage, work = self.next_stage(ticket['id'])
        packet, snapshot = self.prepare(ticket['id'], stage, work)
        coverage = packet['summary']['scope_coverage']
        self.assertTrue(coverage['request_preview_truncated'])
        self.assertTrue(coverage['history_scope_check_required'])
        found = snapshot.search_history(query='禁止发布', kind='owner_request')['records']
        self.assertEqual(len(found), 1)
        self.assertIn('禁止发布', found[0]['snippet'])
        self.assertGreater(found[0]['snippet_offset'], 3500)
        self.assertEqual(self.read_full(snapshot, found[0]['id']), text)

    def test_new_stage_creates_new_snapshot_and_never_overwrites_prior_files(self):
        ticket = self.new_ticket('阶段快照应可追溯。')
        first, first_work = self.next_stage(ticket['id'])
        first_packet, first_snapshot = self.prepare(ticket['id'], first, first_work)
        before = {p.relative_to(first_work): p.read_bytes() for p in first_work.rglob('*') if p.is_file()}
        self.set_followups(ticket['id'], [{'text': '新要求只存在第二份快照里。', 'stage_id': first['id'],
                                           'message_id': 9, 'user_id': OWNER, 'chat_id': GROUP}])
        second, second_work = self.next_stage(ticket['id'])
        second_packet, second_snapshot = self.prepare(ticket['id'], second, second_work)
        self.assertNotEqual(first_packet['manifest'], second_packet['manifest'])
        self.assertEqual(first_snapshot.search_history(query='新要求只存在第二份快照里')['total_matches'], 0)
        self.assertGreater(second_snapshot.search_history(query='新要求只存在第二份快照里')['total_matches'], 0)
        after = {p.relative_to(first_work): p.read_bytes() for p in first_work.rglob('*') if p.is_file()}
        self.assertEqual(before, after)
        with self.assertRaises(FileExistsError):
            ticket_context.prepare(self.cfg, self.store, self.store.ticket(ticket['id']), first,
                                   first_work, ticket['text'])
        self.assertEqual(before, {p.relative_to(first_work): p.read_bytes()
                                 for p in first_work.rglob('*') if p.is_file()})

    def test_escaped_recent_owner_text_still_respects_bootstrap_budget(self):
        ticket=self.new_ticket('原始目标' * 1000)
        exact='\x00' * 5000
        self.set_followups(ticket['id'],[
            {'text':exact,'message_id':8,'user_id':OWNER,'chat_id':GROUP},
            {'text':exact,'message_id':9,'user_id':OWNER,'chat_id':GROUP}])
        for budget in (12000,18000):
            self.cfg.raw['context_boot_max_chars']=budget
            stage,work=self.next_stage(ticket['id'])
            pack,snapshot=self.prepare(ticket['id'],stage,work)
            metrics=json.loads((work/'inputs/context_metrics.json').read_text(encoding='utf-8'))
            self.assertLessEqual(metrics['bootstrap_history_chars'],budget)
            self.assertTrue(pack['summary']['scope_coverage']['followup_previews_truncated'])
            raw=[json.loads(line) for line in (work/'inputs/history/records.jsonl').read_text(encoding='utf-8').split('\n') if line]
            record=next(x for x in raw if x['id']==ticket['id']+':owner:1')
            self.assertEqual(json.loads(record['text'])['text'],exact)

    def test_pm_triage_workflow_receives_history_tool_and_bounded_request(self):
        ticket = self.new_ticket('请整理下一步的只读检查。', command='/pm')
        self.set_followups(ticket['id'], [
            {'text': f'第{n}次补充：' + '保留原始资料。' * 300, 'message_id': n + 10,
             'user_id': OWNER, 'chat_id': GROUP} for n in range(100)])
        stage = self.store.stages(ticket['id'])[-1]
        with self.store.tx() as c:
            c.execute("UPDATE stages SET executor='codex_c' WHERE id=?", (stage['id'],))
        stage = self.store.stage(stage['id'])
        work = self.scheduler.wf.workdir(ticket['id'], stage['name'], 1, 1)
        job = self.scheduler.wf.prepare(self.store, self.store.ticket(ticket['id']), stage, work)
        tool = job.profile['codex_overrides']['mcp_servers']['ticket_history']
        self.assertIn('--manifest', tool['args'])
        manifest = Path(tool['args'][-1])
        self.assertTrue(manifest.is_file())
        metrics = json.loads((work / 'inputs/context_metrics.json').read_text(encoding='utf-8'))
        self.assertLessEqual(metrics['bootstrap_history_chars'], 18000)
        self.assertNotIn('第0次补充：' + '保留原始资料。' * 300, job.prompt)
        self.assertFalse(job.profile['codex_overrides']['features.shell_tool'])
        self.assertTrue(job.profile['structured_result'])
        self.assertIn('ticket_history', job.prompt)
        self.assertGreater(HistorySnapshot(manifest).search_history(query='第0次补充：')['total_matches'], 0)


if __name__ == '__main__':
    unittest.main()
