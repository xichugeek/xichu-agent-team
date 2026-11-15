import hashlib
import json
import os
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

from xichu_agent.attachment_cache import fetch_attachment


class Downloader:
    def __init__(self):
        self.calls = 0
        self.lock = threading.Lock()
        self.body = b"real downloaded attachment\x00\xff"
        self.fail = False

    def download_file(self, file_id, dest):
        with self.lock:
            self.calls += 1
        time.sleep(.01)
        dest.write_bytes(self.body if not self.fail else b"partial")
        if self.fail:
            raise OSError("download interrupted")


class AttachmentCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.cfg = SimpleNamespace(data_dir=self.root / "private_runtime")
        self.client = Downloader()
        self.item = {"bot": "video", "file_id": "F1", "file_unique_id": "U1", "kind": "document"}

    def tearDown(self):
        self.temp.cleanup()

    def get(self, stage, item=None):
        return fetch_attachment(self.cfg, item or self.item, self.root / stage / "inputs/file.bin", self.client)

    def test_multiple_stages_reuse_verified_bytes_even_when_file_id_rotates(self):
        first = self.get("stage1")
        second = self.get("stage2", {**self.item, "file_id": "F2"})
        self.assertFalse(first["cache_hit"])
        self.assertTrue(second["cache_hit"])
        self.assertEqual(self.client.calls, 1)
        self.assertEqual(Path(second["path"]).read_bytes(), self.client.body)
        self.assertEqual(second["sha256"], hashlib.sha256(self.client.body).hexdigest())
        metadata = json.loads(next((self.cfg.data_dir / "attachment_cache").glob("*.json")).read_text())
        self.assertEqual(metadata["bytes"], len(self.client.body))
        self.assertEqual(metadata["sha256"], second["sha256"])

    def test_job_modification_does_not_change_cache_or_another_job(self):
        first = self.get("stage1")
        Path(first["path"]).write_bytes(b"job edited this file")
        second = self.get("stage2")
        blob = next((self.cfg.data_dir / "attachment_cache").glob("*.blob"))
        self.assertEqual(blob.read_bytes(), self.client.body)
        self.assertEqual(Path(second["path"]).read_bytes(), self.client.body)
        self.assertFalse(os.path.samefile(blob, second["path"]))
        self.assertEqual(self.client.calls, 1)

    def test_corrupt_blob_or_metadata_is_redownloaded(self):
        self.get("stage1")
        folder = self.cfg.data_dir / "attachment_cache"
        next(folder.glob("*.blob")).write_bytes(b"x" * len(self.client.body))
        recovered = self.get("stage2")
        self.assertFalse(recovered["cache_hit"])
        self.assertEqual(Path(recovered["path"]).read_bytes(), self.client.body)
        next(folder.glob("*.json")).write_text("broken JSON")
        self.assertFalse(self.get("stage3")["cache_hit"])
        self.assertEqual(self.client.calls, 3)

    def test_receiving_bots_are_separate_cache_namespaces(self):
        self.get("video")
        other = self.get("site", {**self.item, "bot": "site"})
        self.assertFalse(other["cache_hit"])
        self.assertEqual(self.client.calls, 2)
        self.assertEqual(len(list((self.cfg.data_dir / "attachment_cache").glob("*.blob"))), 2)

    def test_missing_unique_id_falls_back_to_file_id_and_ignores_unknown_fields(self):
        item = {**self.item, "file_unique_id": None, "cache_path": "outside", "sha256": "forged"}
        self.get("stage1", item)
        self.assertTrue(self.get("stage2", item)["cache_hit"])
        self.assertFalse(self.get("stage3", {**item, "file_id": "other"})["cache_hit"])
        self.assertEqual(self.client.calls, 2)

    def test_concurrent_same_key_downloads_once_with_independent_job_copies(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda n: self.get("stage" + str(n)), range(8)))
        self.assertEqual(self.client.calls, 1)
        self.assertEqual(sum(not r["cache_hit"] for r in results), 1)
        for result in results:
            self.assertEqual(Path(result["path"]).read_bytes(), self.client.body)

    def test_interrupted_download_does_not_install_partial_bytes(self):
        self.client.fail = True
        with self.assertRaises(OSError):
            self.get("stage1")
        self.assertEqual(list((self.cfg.data_dir / "attachment_cache").iterdir()), [])
        self.client.fail = False
        self.assertFalse(self.get("stage2")["cache_hit"])
        self.assertEqual(self.client.calls, 2)

    def test_wrong_download_length_is_not_cached_or_reported_success(self):
        with self.assertRaises(RuntimeError):
            self.get("stage1", {**self.item, "bytes": len(self.client.body) + 1})
        self.assertEqual(list((self.cfg.data_dir / "attachment_cache").iterdir()), [])


if __name__ == "__main__":
    unittest.main()
