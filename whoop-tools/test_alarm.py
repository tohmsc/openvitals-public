#!/usr/bin/env python3
"""Tests for the alarm logic and the haptics/alarm frames (no Bluetooth).

Run:  .venv/bin/python -m unittest test_alarm -v
"""
import datetime as dt, json, os, struct, tempfile, time, unittest

_TMP = tempfile.mkdtemp(prefix="whoop-test-")
os.environ["WHOOP_DB"] = os.path.join(_TMP, "test.db")

import db, whoop, alarm  # noqa: E402


def at(h, m, s=0, days=0):
    d = dt.datetime.now().replace(hour=h, minute=m, second=s, microsecond=0) + dt.timedelta(days=days)
    return d.timestamp()


class Frames(unittest.TestCase):
    def test_buzz_frame_matches_the_one_the_strap_obeyed(self):
        self.assertEqual(whoop.cmd(whoop.C_RUN_HAPTICS, whoop.haptics_payload(2, 1)).hex(),
                         "aa0c00fc23004f02010000006036a92e")

    def test_buzz_limits(self):
        with self.assertRaises(ValueError):
            whoop.haptics_payload(2, 0)
        with self.assertRaises(ValueError):
            whoop.haptics_payload(2, 9)          # keep test buzzes short

    def test_alarm_payload(self):
        p = whoop.alarm_payload(1795077253.5)
        self.assertEqual(len(p), 9)
        self.assertEqual(struct.unpack("<BIHH", p), (1, 1795077253, 16384, 0))

    def test_alarm_readback_as_the_strap_sent_it(self):
        # body of the real GET_ALARM_TIME reply after arming 1795080778 (2026-11-19)
        self.assertEqual(whoop.parse_alarm(bytes.fromhex("01014ac2fe6a0000")), 1795080778)
        self.assertIsNone(whoop.parse_alarm(bytes.fromhex("0100000000000000")))   # not armed
        self.assertIsNone(whoop.parse_alarm(bytes.fromhex("0100")))                # too short

    def test_response(self):
        r = whoop.parse_response(bytes.fromhex("24124f0002000020"))
        self.assertEqual((r["op"], r["status"], r["ok"]), (0x4F, 2, False))

    def test_dispatch_resolves_the_waiting_request_and_reports_events(self):
        w = whoop.Whoop.__new__(whoop.Whoop)
        w.raw_sink = w.on_realtime = w.on_historical = w.on_meta = None
        w.verbose, seen = False, []
        w.on_event = lambda eid, inner: seen.append(eid)
        import asyncio

        async def go():
            fut = asyncio.get_running_loop().create_future()
            w._waiting = {0x43: fut}
            w._dispatch(whoop.CH_RESP, bytes.fromhex("2401430001" + "01014ac2fe6a0000"))
            w._dispatch(whoop.CH_EVENT, bytes([0x30, 0, whoop.E_HAPTICS_FIRED, 0]))
            return fut.result()
        r = asyncio.run(go())
        self.assertEqual(whoop.parse_alarm(r["body"]), 1795080778)
        self.assertEqual(seen, [whoop.E_HAPTICS_FIRED])


class Logic(unittest.TestCase):
    def setUp(self):
        self.c = db.connect()
        self.c.execute("DELETE FROM sync_state")
        self.c.execute("DELETE FROM strap_cmd")
        self.c.commit()

    def test_parse_hhmm(self):
        self.assertEqual(alarm.parse_hhmm("7:05"), (7, 5))
        for bad in ("24:00", "7", "ab:cd", "07:60"):
            with self.assertRaises(ValueError):
                alarm.parse_hhmm(bad)

    def test_next_time(self):
        self.assertEqual(alarm.next_time(7, 0, at(6, 0)), at(7, 0))
        self.assertEqual(alarm.next_time(7, 0, at(7, 0)), at(7, 0, days=1))   # just rang: tomorrow
        self.assertEqual(alarm.next_time(7, 0, at(23, 0)), at(7, 0, days=1))

    def test_daily_and_once(self):
        alarm.set_alarm(self.c, "07:00", now=at(6, 0))
        self.assertEqual(alarm.desired(alarm.get(self.c), at(6, 0)), at(7, 0))
        self.assertEqual(alarm.desired(alarm.get(self.c), at(8, 0)), at(7, 0, days=1))
        alarm.set_alarm(self.c, "07:00", daily=False, now=at(6, 0))
        self.assertEqual(alarm.desired(alarm.get(self.c), at(6, 0)), at(7, 0))
        self.assertIsNone(alarm.desired(alarm.get(self.c), at(8, 0)))           # once: done after
        alarm.turn_off(self.c)
        self.assertIsNone(alarm.desired(alarm.get(self.c), at(6, 0)))

    def test_setting_queues_a_sync_for_the_daemon(self):
        alarm.set_alarm(self.c, "07:00")
        r = self.c.execute("SELECT kind,status FROM strap_cmd").fetchone()
        self.assertEqual((r["kind"], r["status"]), ("alarm", "pending"))

    def test_due_for_sync(self):
        now = at(6, 0)
        self.assertFalse(alarm.due_for_sync(self.c, now))                    # off and nothing armed
        alarm.set_alarm(self.c, "07:00", now=now)
        self.assertTrue(alarm.due_for_sync(self.c, now))
        db.set_state(self.c, alarm.ARMED, json.dumps({"epoch": at(7, 0), "ok": True, "checked_ts": now}))
        self.assertFalse(alarm.due_for_sync(self.c, now))
        self.assertTrue(alarm.due_for_sync(self.c, now + alarm.RECHECK + 1))  # periodic read-back
        self.assertTrue(alarm.due_for_sync(self.c, at(7, 1)))                 # rang: arm tomorrow

    def test_ring_due_only_when_the_strap_did_not_take_it(self):
        e = at(7, 0)
        db.set_state(self.c, alarm.ARMED, json.dumps({"epoch": e, "ok": True}))
        self.assertFalse(alarm.ring_due(self.c, e + 1))
        db.set_state(self.c, alarm.ARMED, json.dumps({"epoch": e, "ok": False}))
        self.assertFalse(alarm.ring_due(self.c, e - 1))
        self.assertTrue(alarm.ring_due(self.c, e + 1))
        self.assertFalse(alarm.ring_due(self.c, e + alarm.RING_LATE + 1))     # too late: don't
        alarm.mark_rang(self.c, e + 1)
        self.assertFalse(alarm.ring_due(self.c, e + 2))
        db.set_state(self.c, alarm.ARMED, json.dumps({"epoch": e, "ok": False, "clearing": True}))
        self.assertFalse(alarm.ring_due(self.c, e + 1))                       # you turned it off


if __name__ == "__main__":
    unittest.main()
