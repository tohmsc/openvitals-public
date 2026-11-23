#!/usr/bin/env python3
"""Network links (no real network): D1 push retries, Telegram errors keep the token out of logs.

Run:  .venv/bin/python -m unittest test_connections -v
"""
import os, tempfile, unittest
from unittest import mock

os.environ.setdefault("WHOOP_DB", os.path.join(tempfile.mkdtemp(prefix="whoop-test-"), "test.db"))

import requests  # noqa: E402
import cloud_sync, telegram_report  # noqa: E402


class Resp:
    def __init__(self, status=200, ok=True):
        self.status_code = status
        self.headers = {"content-type": "application/json"}
        self.text = ""
        self._j = {"success": ok, "result": []}

    def json(self):
        return self._j


class D1RetryTest(unittest.TestCase):
    def setUp(self):
        self.d1 = cloud_sync.D1("tok", "acct", "db")
        self.sleep = mock.patch.object(cloud_sync.time, "sleep").start()
        self.addCleanup(mock.patch.stopall)

    def test_timeout_then_success(self):
        self.d1.s.post = mock.Mock(side_effect=[requests.ReadTimeout("slow"), Resp()])
        self.assertTrue(self.d1.q("SELECT 1")["success"])
        self.assertEqual(self.d1.s.post.call_count, 2)

    def test_server_error_then_success(self):
        self.d1.s.post = mock.Mock(side_effect=[Resp(503, ok=False), Resp()])
        self.assertTrue(self.d1.q("SELECT 1")["success"])

    def test_gives_up_after_three_tries(self):
        self.d1.s.post = mock.Mock(side_effect=requests.ConnectionError("offline"))
        with self.assertRaises(requests.ConnectionError):
            self.d1.q("SELECT 1")
        self.assertEqual(self.d1.s.post.call_count, 3)

    def test_bad_sql_is_not_retried(self):
        self.d1.s.post = mock.Mock(return_value=Resp(400, ok=False))
        with self.assertRaises(RuntimeError):
            self.d1.q("SELEC 1")
        self.assertEqual(self.d1.s.post.call_count, 1)

    def test_esc_handles_non_finite_floats_and_malformed_json_response(self):
        self.assertEqual(cloud_sync.esc(float("nan")), "NULL")
        self.assertEqual(cloud_sync.esc(float("inf")), "NULL")
        self.assertEqual(cloud_sync.esc(True), "1")
        bad = Resp(200, ok=True)
        bad.json = mock.Mock(side_effect=ValueError("truncated"))
        self.d1.s.post = mock.Mock(return_value=bad)
        self.assertIsNone(self.d1.q("SELECT 1", ok_fail=True))


class WhoopApiRetryTest(unittest.TestCase):
    def test_429_retry_is_bounded(self):
        import whoop_api
        r429 = mock.Mock(status_code=429, headers={"Retry-After": "1"})
        r429.raise_for_status.side_effect = requests.HTTPError("429")
        with mock.patch.object(whoop_api, "access_token", return_value="tok"), \
             mock.patch.object(whoop_api.requests, "get", return_value=r429) as mget, \
             mock.patch.object(whoop_api.time, "sleep"):
            with self.assertRaises(requests.HTTPError):
                whoop_api.get("/v2/cycle", tries=3)
        self.assertEqual(mget.call_count, 3)


class TelegramErrorTest(unittest.TestCase):
    def test_token_is_not_in_the_error(self):
        token = "123456:SECRET-TOKEN"
        err = requests.ConnectionError(f"Max retries exceeded with url: /bot{token}/getUpdates")
        with mock.patch.object(telegram_report.requests, "post", side_effect=err):
            with self.assertRaises(RuntimeError) as cm:
                telegram_report.call(token, "getUpdates")
        self.assertNotIn("SECRET", str(cm.exception))
        self.assertIn("<token>", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
