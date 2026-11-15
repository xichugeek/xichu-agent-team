"""Earned reset safety and behavior, using synthetic accounts/RPC only."""
from contextlib import contextmanager
import json
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import Mock, patch

from control_center.codex_quota import QuotaCache, ResetRejected, consume_reset, normalize_limits
from control_center.quota_reset import ResetManager

ACCOUNT = {"codex_home": "synthetic-home", "enabled": True}
EMAIL = "synthetic@example.test"


def payload():
    return {"email": EMAIL, "credit_id": "SyntheticCredit_1", "idempotency_key": str(uuid.uuid4()), "confirmed": True}


def limits(count=1):
    return {"rateLimits": {"primary": {"usedPercent": 70, "windowDurationMins": 300}},
            "rateLimitResetCredits": {"availableCount": count, "credits": [{"id": "SyntheticCredit_1", "status": "available", "resetType": "codexRateLimits", "expiresAt": 4102444800}] if count else []}}


class ResetRPCTests(unittest.TestCase):
    def session(self, count=1, email=EMAIL, outcome="reset", fail_refresh=False):
        self.calls = []
        @contextmanager
        def factory(config, *args):
            self.assertEqual(config, ACCOUNT)
            def rpc(i, method, params=None):
                self.calls.append((method, params))
                if method == "account/read":
                    return {"account": {"type": "chatgpt", "email": email, "planType": "plus"}}
                if method == "account/rateLimitResetCredit/consume":
                    return {"outcome": outcome}
                if i == 4 and fail_refresh:
                    raise OSError("synthetic network error")
                return limits(count if i == 2 else 0)
            yield rpc
        return patch("control_center.codex_quota.app_session", factory)

    def test_normalize_available_counts_unknown_expired_and_capped_details(self):
        self.assertIsNone(normalize_limits({})["reset_credits"]["available_count"])
        raw = limits(4)
        raw["rateLimitResetCredits"]["credits"] += [{"id": "expired", "status": "available", "resetType": "codexRateLimits", "expiresAt": 1}, {"id": "used", "status": "redeemed", "resetType": "codexRateLimits"}]
        result = normalize_limits(raw)["reset_credits"]
        self.assertEqual(result["available_count"], 4)
        self.assertEqual([c["id"] for c in result["credits"]], ["SyntheticCredit_1"])
        raw["rateLimitResetCredits"]["credits"] = None
        self.assertEqual(normalize_limits(raw)["reset_credits"]["available_count"], 4)

    def test_reset_is_scoped_verified_and_refetches_actual_limits(self):
        request = payload()
        with self.session():
            result = consume_reset(ACCOUNT, request)
        self.assertEqual([m for m, _ in self.calls], ["account/read", "account/rateLimits/read", "account/rateLimitResetCredit/consume", "account/rateLimits/read"])
        self.assertEqual(self.calls[2][1], {"idempotencyKey": request["idempotency_key"], "creditId": "SyntheticCredit_1"})
        self.assertEqual(result["outcome"], "reset")
        self.assertEqual(result["data"]["reset_credits"]["available_count"], 0)
        self.assertEqual(result["data"]["windows"][0]["remaining_percent"], 30)

    def test_wrong_account_never_sends_consume(self):
        with self.session(email="other@example.test"), self.assertRaises(ResetRejected):
            consume_reset(ACCOUNT, payload())
        self.assertFalse(any("consume" in m for m, _ in self.calls))

    def test_zero_credits_fresh_request_does_not_consume(self):
        with self.session(count=0):
            result = consume_reset(ACCOUNT, payload())
        self.assertEqual(result["outcome"], "noCredit")
        self.assertFalse(any("consume" in m for m, _ in self.calls))

    def test_uncertain_retry_replays_same_key_even_if_credit_now_zero(self):
        request = payload()
        with self.session(count=0, outcome="alreadyRedeemed"):
            result = consume_reset(ACCOUNT, request, retry=True)
        self.assertEqual(result["outcome"], "alreadyRedeemed")
        self.assertEqual(self.calls[2][1]["idempotencyKey"], request["idempotency_key"])

    def test_success_followed_by_read_failure_does_not_guess_new_quota(self):
        with self.session(fail_refresh=True):
            result = consume_reset(ACCOUNT, payload())
        self.assertEqual(result["outcome"], "reset")
        self.assertIsNone(result["data"])

    def test_no_eligible_window_is_not_reported_as_successful_spend(self):
        with self.session(outcome="nothingToReset"):
            result = consume_reset(ACCOUNT, payload())
        self.assertEqual(result["outcome"], "nothingToReset")


class ResetJournalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "quota-resets.json"
        self.cache = Mock()

    def tearDown(self):
        self.temp.cleanup()

    def wait(self, manager, status):
        for _ in range(200):
            state = manager.state("codex_a")
            if state and state["status"] == status:
                return state
            time.sleep(.01)
        self.fail(f"Expected {status}; got {state}")

    def test_double_click_and_replay_consume_once_and_record_before_rpc(self):
        entered, release = threading.Event(), threading.Event()
        request = payload()
        def consumer(config, record, retry):
            journal = json.loads(self.path.read_text(encoding="utf-8"))
            self.assertEqual(journal[request["idempotency_key"]]["status"], "running")
            entered.set()
            release.wait(2)
            return {"outcome": "reset", "data": {"windows": []}}
        worker = Mock(side_effect=consumer)
        manager = ResetManager(self.path, self.cache, worker)
        self.assertEqual(manager.start("codex_a", ACCOUNT, request)[0], 202)
        self.assertTrue(entered.wait(1))
        self.assertEqual(manager.start("codex_a", ACCOUNT, request)[0], 202)
        self.assertEqual(manager.start("codex_a", ACCOUNT, payload())[0], 409)
        release.set()
        self.wait(manager, "completed")
        self.assertEqual(manager.start("codex_a", ACCOUNT, request)[0], 200)
        self.assertEqual(worker.call_count, 1)
        # Restarting the service retains the same completed operation.
        restarted = ResetManager(self.path, self.cache, worker)
        self.assertEqual(restarted.start("codex_a", ACCOUNT, request)[0], 200)
        self.assertEqual(worker.call_count, 1)

    def test_unknown_result_survives_restart_and_reuses_same_operation(self):
        request = payload()
        failed = ResetManager(self.path, self.cache, Mock(side_effect=OSError("private error")))
        failed.start("codex_a", ACCOUNT, request)
        self.wait(failed, "unknown")
        consumer = Mock(return_value={"outcome": "alreadyRedeemed", "data": {"windows": []}})
        restarted = ResetManager(self.path, self.cache, consumer)
        self.assertEqual(restarted.start("codex_a", ACCOUNT, payload())[0], 409)
        self.assertEqual(restarted.start("codex_a", ACCOUNT, request)[0], 202)
        state = self.wait(restarted, "completed")
        self.assertEqual(state["outcome"], "alreadyRedeemed")
        self.assertTrue(consumer.call_args.kwargs["retry"])
        self.assertEqual(consumer.call_args.args[1]["idempotency_key"], request["idempotency_key"])
        self.assertNotIn("private error", json.dumps(state))

    def test_failed_identity_check_during_uncertain_retry_keeps_original_pending(self):
        request = payload()
        manager = ResetManager(self.path, self.cache, Mock(side_effect=OSError()))
        manager.start("codex_a", ACCOUNT, request)
        self.wait(manager, "unknown")
        restarted = ResetManager(self.path, self.cache, Mock(side_effect=ResetRejected("Identity changed")))
        restarted.start("codex_a", ACCOUNT, request)
        self.wait(restarted, "unknown")
        self.assertEqual(restarted.start("codex_a", ACCOUNT, payload())[0], 409)

    def test_same_key_cannot_be_used_for_another_account_or_home(self):
        request = payload()
        manager = ResetManager(self.path, self.cache, Mock(return_value={"outcome": "noCredit", "data": {}}))
        manager.start("codex_a", ACCOUNT, request)
        self.wait(manager, "completed")
        self.assertEqual(manager.start("codex_b", ACCOUNT, request)[0], 409)
        self.assertEqual(manager.start("codex_a", {**ACCOUNT, "codex_home": "other-home"}, request)[0], 409)

    def test_corrupt_or_unwritable_journal_and_missing_confirmation_never_consume(self):
        worker = Mock()
        self.path.write_text("invalid journal", encoding="utf-8")
        manager = ResetManager(self.path, self.cache, worker)
        self.assertEqual(manager.start("codex_a", ACCOUNT, payload())[0], 503)
        self.path.unlink()
        manager = ResetManager(self.path, self.cache, worker)
        with patch.object(manager, "_save", side_effect=OSError()):
            self.assertEqual(manager.start("codex_a", ACCOUNT, payload())[0], 503)
        self.assertEqual(manager.start("codex_a", ACCOUNT, {**payload(), "confirmed": False})[0], 400)
        worker.assert_not_called()

    def test_inflight_old_quota_cannot_overwrite_post_reset_values(self):
        entered, release = threading.Event(), threading.Event()
        def reader(config):
            entered.set()
            release.wait(2)
            return {"email": EMAIL, "windows": [{"remaining_percent": 5}]}
        cache = QuotaCache(reader)
        cache.snapshot({"codex_a": ACCOUNT}, {})
        self.assertTrue(entered.wait(1))
        cache.after_reset("codex_a", ACCOUNT, {"email": EMAIL, "windows": [{"remaining_percent": 95}]})
        release.set()
        time.sleep(.03)
        self.assertEqual(cache.snapshot({"codex_a": ACCOUNT}, {})[0]["windows"][0]["remaining_percent"], 95)


if __name__ == "__main__":
    unittest.main()
