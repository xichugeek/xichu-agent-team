import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from xichu_agent.telegram import TelegramClient, TelegramError, message_attachments


class AttachmentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.received = []
        self.remote_path = "documents/test.txt"
        self.remote_size = 5
        outer = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_GET(self):
                outer.received.append((self.path, b""))
                self.send_response(200); self.end_headers(); self.wfile.write(b"hello")
            def do_POST(self):
                body = self.rfile.read(int(self.headers['Content-Length']))
                outer.received.append((self.path, body))
                value = ({"file_path": outer.remote_path, "file_size": outer.remote_size} if self.path.endswith("getFile") else {"message_id": 99})
                data = json.dumps({"ok": True, "result": value}).encode()
                self.send_response(200); self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True); self.thread.start()
        self.client = TelegramClient('TEST', api_base=f'http://127.0.0.1:{self.server.server_port}')

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(); self.temp.cleanup()

    def test_download_uses_receiving_bot_and_actual_bytes(self):
        target = self.root / "inputs/file.txt"
        self.client.download_file("F1", target)
        self.assertEqual(target.read_bytes(), b"hello")
        self.assertEqual(self.received[-1][0], "/file/botTEST/documents/test.txt")

    def test_large_file_is_rejected_before_download(self):
        self.remote_size = 21_000_000
        with self.assertRaises(TelegramError): self.client.download_file("F1", self.root / "large")
        self.assertEqual(len(self.received), 1)

    def test_invalid_remote_path_cannot_leave_file_endpoint(self):
        self.remote_path = "../private"
        with self.assertRaises(TelegramError): self.client.download_file("F1", self.root / "bad")
        self.assertEqual(len(self.received), 1)

    def test_document_delivery_sends_real_bytes_and_original_reply(self):
        doc = self.root / "交接报告.md"; doc.write_text("actual handoff", encoding='utf-8')
        self.assertEqual(self.client.send_document(-100, doc, 42)['message_id'], 99)
        path, data = self.received[0]
        self.assertEqual(path, '/botTEST/sendDocument')
        self.assertIn(b'actual handoff', data)
        self.assertIn(b'"message_id": 42', data)
        self.assertIn('交接报告.md'.encode(), data)

    def test_photo_selects_largest_and_keeps_receiving_bot(self):
        items = message_attachments({'message_id': 3, 'photo': [{'file_id': 'small'}, {'file_id': 'big'}]}, 'video')
        self.assertEqual(items[0]['file_id'], 'big')
        self.assertEqual(items[0]['bot'], 'video')


if __name__ == '__main__': unittest.main()
