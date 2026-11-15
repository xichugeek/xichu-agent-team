"""Control Center fault-injection tests using temporary synthetic data only."""
from __future__ import annotations

import hashlib
import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from control_center.app import ControlCenter, Server
from control_center.pipeline import enrich
from control_center.status_reader import CONTRACTS, FORMAL_GATES, StatusReader, checked_path
from control_center.validate_worker import validate_existing

ROOT = Path(__file__).resolve().parents[1]


class ControlCenterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "Episodes").mkdir()
        config = yaml.safe_load((ROOT / "studio.config.yaml").read_text(encoding="utf-8"))
        (self.root / "studio.config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
        (self.root / "scripts").mkdir()
        (self.root / "scripts/studio.py").write_text("# synthetic metadata", encoding="utf-8")
        self.config = config
        self.ep, self.data = self.make_episode("005_synthetic")
        self.reader = StatusReader(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def make_episode(self, name):
        ep = self.root / "Episodes" / name
        ep.mkdir()
        for rel in CONTRACTS.values():
            text = "{}" if rel.endswith(".json") else "file,status,required\n" if rel.endswith(".csv") else "synthetic test only"
            (ep / rel).write_text(text, encoding="utf-8")
        data = {"schema_version": "0.1", "episode_id": name[:3], "title": "Synthetic episode",
                "test_only": name.startswith("TEST_"), "history": [],
                "artifacts": {k: {"path": rel, "status": "planned", "sha256": "0" * 64,
                                   "input_hashes": {p: "0" * 64 for p in self.config["dependencies"][k]},
                                   "updated_at": "2026-10-05T00:00:00+00:00"} for k, rel in CONTRACTS.items()},
                "gates": {g: {"status": "pending", "actor": None, "at": None} for g in FORMAL_GATES}}
        self.write_status(ep, data)
        return ep, data

    @staticmethod
    def write_status(ep, data):
        (ep / "production_status.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")

    def ready_native(self):
        for k in self.data["artifacts"]:
            self.data["artifacts"][k]["status"] = "approved"
        self.data["artifacts"]["render"]["status"] = "review_required"
        self.data["artifacts"]["qa"]["status"] = "review_required"
        self.data["artifacts"]["release"]["status"] = "review_required"
        self.data["gates"]["SCRIPT_APPROVAL"]["status"] = "approved"
        self.write_status(self.ep, self.data)
        for directory in ("Voice", "Assets/Cover", "Raw", "Edit/Jianying/V2", "Reviews"):
            (self.ep / directory).mkdir(parents=True, exist_ok=True)
        (self.ep / "Voice/master_audio.wav").write_bytes(b"synthetic audio metadata only")
        (self.ep / "Raw/shot.mp4").write_bytes(b"synthetic picture metadata only")
        (self.ep / "Assets/Cover/cover.png").write_bytes(b"synthetic image metadata only")
        (self.ep / "shot_plan.csv").write_text("file,status,required\nRaw/shot.mp4,selected,true\n", encoding="utf-8")
        drafts = self.root / "drafts"
        draft = drafts / "SyntheticDraft"
        draft.mkdir(parents=True)
        self.reader.draft_roots = [drafts]
        timeline = {"shots": [{"file": "Raw/shot.mp4"}], "main_audio": "Voice/master_audio.wav",
                    "captions": "captions.srt", "duration_seconds": 10, "fps": 60,
                    "native_draft_name": "SyntheticDraft", "candidate_status": "EDIT_READY_FOR_USER_EXPORT"}
        delivery = {"status": "EDIT_READY_FOR_USER_EXPORT", "native_saved_reopened": True,
                    "native_draft_name": "SyntheticDraft", "native_draft_path": str(draft),
                    "native_qa": "Reviews/jianying_synthetic_qa.json", "picture": {"width": 1920, "height": 1080, "fps": 60}}
        qa = {"status": "SAVED_AND_REOPENED", "draft_name": "SyntheticDraft", "captions": {"count": 3}, "audio": {"tracks": 1}}
        for rel, data in [("timeline_manifest.json", timeline), ("render_report.json", {"status": "not_rendered", "output": None}),
                          ("Edit/Jianying/V2/delivery_scope.json", delivery), ("Reviews/jianying_synthetic_qa.json", qa)]:
            (self.ep / rel).write_text(json.dumps(data), encoding="utf-8")

    def test_discovers_003_004_005_and_new_episode_automatically(self):
        self.make_episode("003_synthetic")
        self.make_episode("004_synthetic")
        refs = [x["ref"] for x in self.reader.listing()]
        self.assertEqual(len(refs), 3)
        self.assertIn("003_synthetic", refs)
        self.assertEqual(self.reader.resolve("005"), self.ep)
        self.make_episode("006_new")
        self.assertIn("006_new", [x["ref"] for x in self.reader.listing()])

    def test_missing_status_and_contract_files_do_not_crash(self):
        (self.ep / "production_status.yaml").unlink()
        (self.ep / "facts.md").unlink()
        detail = enrich(self.reader.read("005"))
        self.assertFalse(detail["status_exists"])
        self.assertEqual(detail["gates"]["SCRIPT_APPROVAL"]["status"], "unknown")
        self.assertEqual(len(detail["team"]), 8)

    def test_missing_yaml_fields_degrade_to_unknown(self):
        self.write_status(self.ep, {"episode_id": "005", "artifacts": {"brief": {}}, "gates": {}})
        detail = enrich(self.reader.read("005"))
        self.assertEqual(detail["artifacts"]["brief"]["status"], "unknown")
        self.assertTrue(all(g["status"] == "unknown" for g in detail["gates"].values()))
        self.assertEqual(detail["progress"], 0)

    def test_malformed_yaml_and_json_are_tolerated(self):
        (self.ep / "production_status.yaml").write_text("[broken", encoding="utf-8")
        (self.ep / "timeline_manifest.json").write_text("not json", encoding="utf-8")
        detail = self.reader.read("005")
        self.assertFalse(detail["status_parsed"])
        self.assertEqual(detail["stats"]["duration"], None)
        self.assertGreaterEqual(len(detail["warnings"]), 2)

    def test_fact_review_is_derived_and_formal_gates_are_not_added(self):
        for key in ("facts", "evidence"):
            self.data["artifacts"][key]["status"] = "approved"
        self.write_status(self.ep, self.data)
        before = (self.ep / "production_status.yaml").read_bytes()
        d = self.reader.read("005")
        self.assertEqual(d["gates"]["FACT_REVIEW"]["status"], "approved")
        self.assertFalse(d["gates"]["FACT_REVIEW"]["formal"])
        self.assertEqual(d["gates"]["FINAL_REVIEW"]["status"], "pending")
        self.assertEqual(before, (self.ep / "production_status.yaml").read_bytes())

    def test_native_ready_does_not_claim_old_export_is_final_mp4(self):
        self.ready_native()
        (self.ep / "Export").mkdir()
        (self.ep / "Export/old_silent.mp4").write_bytes(b"old synthetic preview")
        d = enrich(self.reader.read("005"))
        self.assertEqual(d["overall_status"], "EDIT_READY_FOR_USER_EXPORT")
        self.assertFalse(d["candidate"]["final_mp4_complete"])
        self.assertEqual(d["stage"], "qa")
        self.assertIn("QA", d["next_action"])
        self.assertEqual(d["progress"], 80)
        self.assertNotIn("old_silent", json.dumps(d))

    def test_missing_native_directory_or_stale_timeline_is_not_ready(self):
        self.ready_native()
        self.data["artifacts"]["timeline"]["status"] = "obsolete"
        self.write_status(self.ep, self.data)
        self.assertFalse(self.reader.read("005")["candidate"]["native_ready"])
        self.data["artifacts"]["timeline"]["status"] = "approved"
        self.write_status(self.ep, self.data)
        (self.root / "drafts/SyntheticDraft").rmdir()
        self.assertFalse(self.reader.read("005")["candidate"]["native_ready"])

    def test_upstream_obsolete_and_parent_fingerprint_are_visible(self):
        self.data["artifacts"]["voice"]["status"] = "obsolete"
        self.data["artifacts"]["script"]["input_hashes"]["facts"] = "1" * 64
        self.write_status(self.ep, self.data)
        d = enrich(self.reader.read("005"))
        self.assertIn("voice", d["obsolete"])
        self.assertIn("script", d["obsolete"])
        self.assertEqual(next(x for x in d["progress_breakdown"] if x["id"] == "voice")["points"], 0)

    def test_100_percent_does_not_publish(self):
        self.ready_native()
        for key in ("render", "qa", "release"):
            self.data["artifacts"][key]["status"] = "approved"
        for g in FORMAL_GATES:
            self.data["gates"][g]["status"] = "approved"
        self.write_status(self.ep, self.data)
        d = enrich(self.reader.read("005"))
        self.assertEqual(d["progress"], 100)
        self.assertFalse(d["published"])
        self.assertNotEqual(d["overall_status"], "PUBLISHED")
        self.assertIn("完成实际发布", d["next_action"])

    def test_planned_templates_do_not_prove_role_execution(self):
        d = enrich(self.reader.read("005"))
        self.assertTrue(all(role["execution"] == "未确认执行" for role in d["team"]))

    def test_captured_existing_media_is_not_misreported_as_missing(self):
        self.ready_native()
        (self.ep / "shot_plan.csv").write_text("file,status,required\nRaw/shot.mp4,captured,true\n", encoding="utf-8")
        d = enrich(self.reader.read("005"))
        recorder = next(x for x in d["pipeline"] if x["id"] == "recorder")
        self.assertEqual(recorder["status"], "captured")
        self.assertEqual(d["stats"]["required_shot_files"], 1)
        self.assertIn("已有素材", d["next_action"])
        self.assertFalse(d["candidate"]["native_ready"])

    def test_secret_extra_fields_are_not_returned(self):
        marker = "SYNTHETIC_SECRET_DO_NOT_EXPOSE"
        self.data["password"] = marker
        self.data["gates"]["SCRIPT_APPROVAL"]["actor"] = marker
        self.data["history"] = [{"action": "private", "note": marker}]
        self.write_status(self.ep, self.data)
        (self.ep / "render_report.json").write_text(json.dumps({"token": marker}), encoding="utf-8")
        self.assertNotIn(marker, json.dumps(enrich(self.reader.read("005"))))

    def test_refresh_never_opens_env_voice_profiles_media_or_markdown(self):
        self.ready_native()
        (self.ep / ".env").write_text("SYNTHETIC_SECRET", encoding="utf-8")
        (self.ep / "asset_manifest.csv").write_text("file,status\n.env,selected\nVoice/LocalTTS/profiles/user.json,selected\n", encoding="utf-8")
        original = Path.open
        opened = []
        def guarded(path, *args, **kwargs):
            opened.append(path)
            self.assertNotEqual(path.name, ".env")
            self.assertNotIn("LocalTTS", path.parts)
            self.assertNotIn(path.suffix, (".md", ".srt", ".mp4", ".wav", ".png"))
            return original(path, *args, **kwargs)
        with patch.object(Path, "open", guarded), patch("control_center.app.subprocess.run", side_effect=AssertionError("heavy operation")):
            d = self.reader.read("005")
            self.assertTrue(d["restricted_paths"])
        self.assertTrue(opened)

    def test_unsafe_episode_refs_and_artifact_paths_are_rejected(self):
        for rel in ("../outside.txt", "D:/secrets.txt", ".env", ".ssh/id_rsa", "credentials/server.txt"):
            self.assertIsNone(checked_path(self.ep, rel))
        with self.assertRaises(ValueError):
            self.reader.resolve("../005_synthetic")

    def test_refresh_is_read_only_and_does_not_invoke_heavy_tools(self):
        before = {p.relative_to(self.ep): hashlib.sha256(p.read_bytes()).hexdigest() for p in self.ep.iterdir() if p.is_file()}
        center = ControlCenter(self.root)
        with patch("control_center.app.subprocess.run", side_effect=AssertionError("heavy operation")):
            for _ in range(3):
                center.snapshot("005")
        after = {p.relative_to(self.ep): hashlib.sha256(p.read_bytes()).hexdigest() for p in self.ep.iterdir() if p.is_file()}
        self.assertEqual(before, after)

    def test_validate_cache_becomes_stale_after_metadata_change(self):
        center = ControlCenter(self.root)
        d = center.reader.read("005")
        center.validations[d["ref"]] = {"status": "PASS", "checked_at": "2026-10-05T00:00:00+00:00", "issues": [], "fingerprint": d["fingerprint"]}
        self.assertEqual(center.detail("005")["validate"]["status"], "PASS")
        (self.ep / "script_locked.md").write_text("changed synthetic data", encoding="utf-8")
        after = center.detail("005")
        self.assertEqual(after["validate"]["status"], "STALE")
        self.assertIn("Validate", after["next_action"])

    def test_validate_error_is_visible_without_mutating_source_status(self):
        self.ready_native()
        before = (self.ep / "production_status.yaml").read_bytes()
        d = enrich(self.reader.read("005"), {"status": "FAIL", "issues": ["synthetic invalid hash"], "checked_at": None})
        self.assertEqual(d["overall_status"], "BLOCKED")
        self.assertEqual(next(x for x in d["pipeline"] if x["current"])["status"], "validate_error")
        self.assertEqual(d["artifacts"]["qa"]["status"], "review_required")
        self.assertEqual(before, (self.ep / "production_status.yaml").read_bytes())

    def test_validate_is_manual_and_single_flight(self):
        center = ControlCenter(self.root)
        entered, release = threading.Event(), threading.Event()
        def wait_worker(*args):
            entered.set()
            release.wait(3)
        with patch.object(center, "_validate", side_effect=wait_worker) as worker:
            self.assertEqual(center.start_validation("005")["status"], "RUNNING")
            self.assertTrue(entered.wait(1))
            self.assertEqual(center.start_validation("005")["status"], "BUSY")
            self.assertEqual(worker.call_count, 1)
            release.set()

    def test_worker_uses_existing_validator_without_reading_secret_reference(self):
        from scripts import studio
        (self.ep / ".env").write_text("SYNTHETIC_SECRET", encoding="utf-8")
        (self.ep / "asset_manifest.csv").write_text("file\n.env\n", encoding="utf-8")
        original = Path.open
        def guarded(path, *args, **kwargs):
            self.assertNotEqual(path.name, ".env")
            return original(path, *args, **kwargs)
        before = (self.ep / "production_status.yaml").read_bytes()
        with patch.object(studio, "EPISODES", self.root / "Episodes"), patch.object(Path, "open", guarded):
            result = validate_existing("005")
        self.assertEqual(result["status"], "UNSAFE")
        self.assertEqual(before, (self.ep / "production_status.yaml").read_bytes())

    def test_active_episode_excludes_recent_test_episode(self):
        self.make_episode("TEST_NEW")
        center = ControlCenter(self.root)
        self.assertEqual(center.snapshot()["studio"]["active_episode"], "005_synthetic")

    def test_http_routes_read_only_and_host_restricted(self):
        center = ControlCenter(self.root)
        server = Server(0, center)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        def request(method, path, headers=None, body=None):
            conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
            conn.request(method, path, body=body, headers=headers or {})
            response = conn.getresponse()
            status, data = response.status, response.read()
            conn.close()
            return status, data
        try:
            code, html = request("GET", "/")
            self.assertEqual(code, 200)
            self.assertIn(b"XichuGeek Studio", html)
            self.assertEqual(request("GET", "/api/snapshot?episode=005")[0], 200)
            self.assertEqual(request("GET", "/static/studio.css")[0], 200)
            self.assertEqual(request("GET", "/static/agent.js")[0], 200)
            self.assertEqual(request("GET", "/static/navigation.js")[0], 200)
            self.assertEqual(request("GET", "/static/site.js")[0], 200)
            code, site = request("GET", "/api/site")
            self.assertEqual(code, 200)
            self.assertEqual(json.loads(site)["status"], "unconfigured")
            self.assertEqual(request("GET", "/api/site?ticket=../wp-config.php")[0], 404)
            self.assertEqual(request("GET", "/api/site", {"Host": "evil.example"})[0], 403)
            code, agent = request("GET", "/api/agent")
            self.assertEqual(code, 200)
            self.assertEqual(json.loads(agent)["status"], "unconfigured")
            self.assertEqual(request("GET", "/api/agent?ticket=../auth.json")[0], 404)
            self.assertEqual(request("GET", "/api/agent", {"Host": "evil.example"})[0], 403)
            self.assertEqual(request("GET", "/control_center.integration.json")[0], 404)
            self.assertEqual(request("POST", "/api/agent", {"X-Studio-Request": "validate"})[0], 405)
            reset_headers = {"Content-Type": "application/json", "Origin": f"http://127.0.0.1:{server.server_port}",
                             "X-Studio-Request": "quota-reset", "X-Studio-CSRF": center.reset_token}
            reset_body = json.dumps({"email": "synthetic@example.test", "credit_id": None,
                                     "idempotency_key": "00000000-0000-4000-8000-000000000001", "confirmed": True})
            with patch.object(center.agents, "account_configs", return_value={"codex_b": {"codex_home": "synthetic"}}), \
                 patch.object(center.resets, "start", return_value=(202, {"status": "running"})) as reset:
                for altered in ({"Origin": "http://evil.example"}, {"X-Studio-CSRF": "invalid"}, {"X-Studio-Request": "validate"}, {"Origin": ""}):
                    self.assertEqual(request("POST", "/api/accounts/codex_b/reset", {**reset_headers, **altered}, reset_body)[0], 403)
                reset.assert_not_called()
                self.assertEqual(request("POST", "/api/accounts/codex_unknown/reset", reset_headers, reset_body)[0], 404)
                self.assertEqual(request("POST", "/api/accounts/codex_b/reset", reset_headers, reset_body)[0], 202)
                self.assertEqual(reset.call_args.args[0], "codex_b")
                self.assertEqual(reset.call_count, 1)
            for route in ("/.env", "/production_status.yaml", "/static/../studio.config.yaml", "/api/episodes/../../outside"):
                self.assertEqual(request("GET", route)[0], 404)
            self.assertEqual(request("GET", "/api/snapshot", {"Host": "evil.example"})[0], 403)
            self.assertEqual(request("POST", "/api/episodes/005/validate")[0], 403)
            headers = {"X-Studio-Request": "validate"}
            for route in ("/api/episodes/005/approve_gate", "/api/episodes/005/sync", "/api/episodes/005/publish"):
                self.assertEqual(request("POST", route, headers)[0], 405)
            self.assertEqual(request("POST", "/api/episodes/005/validate", {**headers, "Origin": "http://evil.example"})[0], 403)
            self.assertEqual(request("PUT", "/api/episodes/005")[0], 405)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(2)


if __name__ == "__main__":
    unittest.main()
