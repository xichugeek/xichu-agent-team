import hashlib
import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from xichu_agent.history_tools import MAX_RESPONSE_CHARS


SERVER = Path(__file__).resolve().parents[1] / 'xichu_agent/history_tools.py'


def write_snapshot(folder, rows):
    records = folder / 'records.jsonl'
    records.write_text(''.join(json.dumps(row, ensure_ascii=False) + '\n' for row in rows), encoding='utf-8')
    manifest = folder / 'manifest.json'
    manifest.write_text(json.dumps({'version': 1, 'records_path': records.name,
        'records_sha256': hashlib.sha256(records.read_bytes()).hexdigest(),
        'ticket_id': 'T-0001', 'owner_id': 1, 'chat_id': -10}), encoding='utf-8')
    return manifest, records


class StdioClient:
    def __init__(self, manifest):
        self.proc = subprocess.Popen([sys.executable, str(SERVER), '--manifest', str(manifest)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env={**os.environ, 'PYTHONIOENCODING': 'cp936'})
        self.lines = queue.Queue()
        self.serial = 0
        self.raw_lengths = []
        self.thread = threading.Thread(target=self._reader, daemon=True)
        self.thread.start()

    def _reader(self):
        for line in self.proc.stdout:
            self.lines.put(line)

    def request(self, method, params=None):
        self.serial += 1
        request = {'jsonrpc': '2.0', 'id': self.serial, 'method': method}
        if params is not None:
            request['params'] = params
        self.proc.stdin.write((json.dumps(request, ensure_ascii=False) + '\n').encode('utf-8'))
        self.proc.stdin.flush()
        raw = self.lines.get(timeout=10).decode('utf-8').rstrip('\r\n')
        self.raw_lengths.append(len(raw))
        response = json.loads(raw)
        if response['id'] != self.serial:
            raise AssertionError('MCP stdout contained an unexpected response or log')
        return response['result']

    def call(self, name, **args):
        result = self.request('tools/call', {'name': name, 'arguments': args})
        return result, json.loads(result['content'][0]['text'])

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.proc.stdin.close()
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=5)
        self.thread.join(timeout=2)
        self.proc.stdout.close()
        self.proc.stderr.close()


class HistoryToolsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def row(self, rid='r1', text='原始记录：禁止发布，只做只读验收。', **extra):
        return {'id': rid, 'kind': 'owner_followup', 'ticket_id': 'T-0001', 'text': text, **extra}

    def test_real_stdio_utf8_pagination_reconstructs_exact_text_and_hash(self):
        text = ('早期限制：禁止发布、不得改原件。\n多行原文 😀\\路径\t保持。\u2028行分隔\u2029段分隔\n' * 310) + '末尾：确认完毕。'
        manifest, _ = write_snapshot(self.root, [self.row(text=text, stage_id=27)])
        with StdioClient(manifest) as client:
            init = client.request('initialize', {'protocolVersion': '2024-11-05'})
            self.assertEqual(init['serverInfo']['name'], 'xichugeek-ticket-history')
            listed = client.request('tools/list')['tools']
            self.assertEqual({t['name'] for t in listed}, {'search_history', 'read_history'})
            pieces, offset = [], 0
            for _ in range(30):
                response, page = client.call('read_history', record_id='r1', offset=offset)
                self.assertFalse(response['isError'], page)
                self.assertEqual(page['total_chars'], len(text))
                self.assertEqual(page['sha256'], hashlib.sha256(text.encode('utf-8')).hexdigest())
                self.assertEqual(page['stage_id'], 27)
                self.assertLessEqual(len(page['text']), 4000)
                pieces.append(page['text'])
                if not page['has_more']:
                    self.assertIsNone(page['next_offset'])
                    break
                self.assertGreater(page['next_offset'], offset)
                offset = page['next_offset']
            self.assertEqual(''.join(pieces), text)
            self.assertTrue(all(n <= MAX_RESPONSE_CHARS for n in client.raw_lengths))
            self.assertEqual(client.request('ping'), {})

    def test_search_pages_find_early_constraint_and_literal_query(self):
        rows = [self.row('early', '铺垫' * 500 + '禁止发布[仅本地]。' + '尾巴' * 500, stage_id=1)]
        rows += [self.row(f'n{i}', f'普通补充{i}') for i in range(13)]
        rows += [self.row('linked', '禁止发布[仅本地]。', kind='stage_result', ticket_id='T-0002')]
        manifest, _ = write_snapshot(self.root, rows)
        with StdioClient(manifest) as client:
            response, found = client.call('search_history', query='禁止发布[', ticket_id='T-0001')
            self.assertFalse(response['isError'])
            self.assertEqual(found['total_matches'], 1)
            hit = found['records'][0]
            self.assertEqual(hit['id'], 'early')
            self.assertIn('禁止发布[', hit['snippet'])
            self.assertGreater(hit['snippet_offset'], 0)
            self.assertLessEqual(len(hit['snippet']), 600)
            ids, offset = [], 0
            while True:
                _, page = client.call('search_history', query='', kind='owner_followup', offset=offset, limit=5)
                self.assertLessEqual(len(page['records']), 5)
                ids.extend(row['id'] for row in page['records'])
                if not page['has_more']:
                    break
                offset = page['next_offset']
            self.assertEqual(ids, [r['id'] for r in rows[:-1]])

    def test_unknown_record_and_bad_arguments_are_tool_errors_not_process_exit(self):
        manifest, _ = write_snapshot(self.root, [self.row()])
        with StdioClient(manifest) as client:
            for name, args in [
                ('read_history', {'record_id': 'missing'}),
                ('read_history', {'record_id': 'r1', 'offset': -1}),
                ('read_history', {'record_id': 'r1', 'offset': 999}),
                ('read_history', {'record_id': 'r1', 'max_chars': 4001}),
                ('search_history', {'limit': 6}),
                ('search_history', {'offset': True}),
                ('read_history', {'record_id': 'r1', 'path': '../anything'}),
            ]:
                result, data = client.call(name, **args)
                self.assertTrue(result['isError'], data)
                self.assertIn('error', data)
                self.assertEqual(client.request('ping'), {})

    def test_manifest_traversal_and_absolute_archive_paths_are_refused(self):
        manifest, _ = write_snapshot(self.root, [self.row()])
        original = json.loads(manifest.read_text(encoding='utf-8'))
        for path in ['../outside.jsonl', '..\\outside.jsonl', str((self.root / 'records.jsonl').resolve())]:
            with self.subTest(path=path):
                manifest.write_text(json.dumps({**original, 'records_path': path}), encoding='utf-8')
                with StdioClient(manifest) as client:
                    result, _ = client.call('search_history')
                    self.assertTrue(result['isError'])
                    self.assertEqual(client.request('ping'), {})

    def test_archive_or_manifest_tampering_after_startup_is_refused(self):
        manifest, records = write_snapshot(self.root, [self.row()])
        with StdioClient(manifest) as client:
            client.request('initialize')
            records.write_text(json.dumps(self.row(text='被篡改内容')) + '\n', encoding='utf-8')
            result, error = client.call('read_history', record_id='r1')
            self.assertTrue(result['isError'])
            self.assertIn('hash mismatch', error['error'])
            modified = json.loads(manifest.read_text(encoding='utf-8'))
            modified['records_sha256'] = hashlib.sha256(records.read_bytes()).hexdigest()
            manifest.write_text(json.dumps(modified), encoding='utf-8')
            result, error = client.call('search_history')
            self.assertTrue(result['isError'])
            self.assertIn('manifest changed', error['error'])
            self.assertEqual(client.request('ping'), {})

    def test_symlink_escape_is_refused(self):
        outside = self.root / 'outside.jsonl'
        outside.write_text(json.dumps(self.row()) + '\n', encoding='utf-8')
        inside = self.root / 'snapshot'
        inside.mkdir()
        manifest, records = write_snapshot(inside, [self.row()])
        records.unlink()
        try:
            records.symlink_to(outside)
        except OSError as exc:
            self.skipTest('OS did not permit creating a test symlink: ' + str(exc))
        data = json.loads(manifest.read_text(encoding='utf-8'))
        data['records_sha256'] = hashlib.sha256(outside.read_bytes()).hexdigest()
        manifest.write_text(json.dumps(data), encoding='utf-8')
        with StdioClient(manifest) as client:
            result, error = client.call('search_history')
            self.assertTrue(result['isError'])
            self.assertIn('outside', error['error'])

    def test_escaped_text_and_metadata_cannot_exceed_wire_response_budget(self):
        text = ('\\\t\x00\n\"' * 2600) + '终点'
        rows = [self.row(f'escape{i}', text, kind='\\' * 80, ticket_id='\\' * 64, stage_id='\\' * 80)
                for i in range(5)]
        manifest, _ = write_snapshot(self.root, rows)
        with StdioClient(manifest) as client:
            _, search = client.call('search_history')
            self.assertEqual(len(search['records']), 5)
            self.assertTrue(all(len(x['snippet']) <= 600 for x in search['records']))
            pieces, offset = [], 0
            for _ in range(100):
                result, page = client.call('read_history', record_id='escape0', offset=offset)
                self.assertFalse(result['isError'], page)
                pieces.append(page['text'])
                if not page['has_more']:
                    break
                self.assertGreater(page['next_offset'], offset)
                offset = page['next_offset']
            self.assertEqual(''.join(pieces), text)
            self.assertTrue(all(n <= MAX_RESPONSE_CHARS for n in client.raw_lengths))

    def test_oversized_metadata_is_rejected_with_bounded_error(self):
        manifest, _ = write_snapshot(self.root, [self.row('x' * 20000)])
        with StdioClient(manifest) as client:
            result, error = client.call('search_history')
            self.assertTrue(result['isError'])
            self.assertIn('oversized', error['error'])
            self.assertTrue(all(n <= MAX_RESPONSE_CHARS for n in client.raw_lengths))


if __name__ == '__main__':
    unittest.main()
