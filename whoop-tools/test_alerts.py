#!/usr/bin/env python3
"""Tests for alerts.py (health alerts) on synthetic nights, and that Telegram sends each once.

Run:  .venv/bin/python -m unittest test_alerts -v
"""
import os, tempfile, time, unittest

_TMP = tempfile.mkdtemp(prefix="whoop-alerts-test-")
os.environ["WHOOP_DB"] = os.path.join(_TMP, "test.db")

import alerts, db, decode, scores, sleep, telegram_report as tr  # noqa: E402

TODAY = "2026-11-19"
TABLES = ("ble_hr", "daily_night", "sleep_night", "estimates", "api_recovery", "api_sleep", "api_profile",
          "sync_state", "clock_sync", "device_soc")


def date(k):
    """TODAY minus k days."""
    return alerts._shift(TODAY, -k)


class AlertsTest(unittest.TestCase):
    def setUp(self):
        self.c = db.connect()
        self.c.executescript(decode.EXTRA)
        self.c.execute(sleep.SCHEMA)
        self.c.execute(scores.SCHEMA)
        self.clear()

    def tearDown(self):                     # other test modules share this database
        self.clear()
        if "resp_rate" in alerts._cols(self.c, "sleep_night"):
            self.c.execute("ALTER TABLE sleep_night DROP COLUMN resp_rate")
        self.c.close()

    def clear(self):
        for t in TABLES:
            self.c.execute(f"DELETE FROM {t}")
        self.c.commit()

    def nights(self, values, col="rhr", **fixed):
        """daily_night rows, oldest first, the last one TODAY. None = no row (missing or suspect)."""
        base = dict({"rhr": 50.0, "hrv": 60.0, "n_rr": 500}, **fixed)
        for k, v in enumerate(reversed(values)):
            if v is None:
                continue
            row = dict(base, **({col: v} if not isinstance(v, dict) else v))
            self.c.execute("INSERT INTO daily_night(date,rhr,hrv,n_rr) VALUES(?,?,?,?)",
                           (date(k), row["rhr"], row["hrv"], row["n_rr"]))
        self.c.commit()

    def on(self):
        return {k for k, a in alerts.evaluate(self.c, TODAY).items() if a}

    # ---- resting heart rate (learn.trend) ----
    def test_resting_hr_up_three_nights_fires_with_numbers(self):
        self.nights([50, 51, 49, 50, 50, 51, 50, 58, 59, 58])
        a = alerts.evaluate(self.c, TODAY)["rhr_up"]
        self.assertIsNotNone(a)
        self.assertEqual(a["nights"], 3)
        self.assertIn("58, 59, 58 bpm", a["why"])
        self.assertIn("usual: 50 bpm", a["why"])
        self.assertIn("54 bpm", a["threshold"])
        self.assertIn("not a diagnosis", a["note"])

    def test_resting_hr_single_bad_night_does_not_fire(self):
        self.nights([50, 51, 49, 50, 50, 51, 50, 50, 50, 62])
        self.assertNotIn("rhr_up", self.on())

    def test_resting_hr_suspect_night_breaks_the_run(self):
        # learn.remember never stores a suspect night, so the middle night is simply missing
        self.nights([50, 51, 49, 50, 50, 51, 50, 58, None, 58])
        self.assertNotIn("rhr_up", self.on())

    def test_no_alert_before_there_is_a_usual_of_your_own(self):
        self.nights([58, 59, 58])
        self.assertEqual(self.on(), set())

    def test_waits_for_this_mornings_night_instead_of_switching_off(self):
        self.nights([50, 51, 49, 50, 50, 51, 50, 58, 59, 58, None])   # last bad night was yesterday
        self.assertIn("rhr_up", self.on())

    # ---- HRV ----
    def test_hrv_low_three_nights_fires(self):
        self.nights([60, 62, 58, 61, 60, 59, 60, 40, 42, 41], col="hrv")
        a = alerts.evaluate(self.c, TODAY)["hrv_low"]
        self.assertIsNotNone(a)
        self.assertIn("40, 42, 41 ms", a["why"])
        self.assertIn("48 ms", a["threshold"])                     # 20% under 60

    def test_hrv_single_bad_night_does_not_fire(self):
        self.nights([60, 62, 58, 61, 60, 59, 60, 60, 61, 35], col="hrv")
        self.assertNotIn("hrv_low", self.on())

    def test_hrv_from_too_few_clean_beats_does_not_count(self):
        thin = {"hrv": 41, "n_rr": 50, "rhr": 50}
        self.nights([60, 62, 58, 61, 60, 59, 60, 40, thin, 41], col="hrv")
        self.assertNotIn("hrv_low", self.on())

    def test_hrv_outside_what_a_heart_does_does_not_count(self):
        self.nights([60, 62, 58, 61, 60, 59, 60, 5, 4, 5], col="hrv")
        self.assertNotIn("hrv_low", self.on())

    # ---- sleep ----
    def sleep_nights(self, hours, covered=True):
        """sleep_night rows, oldest first, usual 7.5 h; heart rate recorded through each night if covered."""
        for k, h in enumerate(reversed(hours)):
            if h is None:
                continue
            wake = time.mktime(time.strptime(date(k), "%Y-%m-%d")) + 7 * 3600
            onset = wake - h * 3600 - 1800
            self.c.execute("INSERT INTO sleep_night(date,onset,wake,asleep_s,awake_s,wakeups,usual_s,status) "
                           "VALUES(?,?,?,?,?,?,?,'done')", (date(k), onset, wake, h * 3600, 1800, 1, 7.5 * 3600))
            ok = covered if not isinstance(covered, list) else covered[len(hours) - 1 - k]
            if ok and h < 6:
                self.c.executemany("INSERT OR REPLACE INTO ble_hr(ts,hr,rr_ms) VALUES(?,55,'')",
                                   [(t,) for t in range(int(onset), int(wake))])
        self.c.commit()

    def test_short_sleep_three_nights_fires(self):
        self.sleep_nights([7.5, 7.4, 7.6, 4.0, 4.5, 4.2])
        a = alerts.evaluate(self.c, TODAY)["short_sleep"]
        self.assertIsNotNone(a)
        self.assertIn("4.0, 4.5, 4.2 h", a["why"])
        self.assertIn("6.4 h", a["threshold"])                     # 7.5 h less 15% (more than an hour)

    def test_short_sleep_single_night_does_not_fire(self):
        self.sleep_nights([7.5, 7.4, 7.6, 7.5, 7.3, 4.0])
        self.assertNotIn("short_sleep", self.on())

    def test_short_night_with_the_strap_off_does_not_count(self):
        self.sleep_nights([7.5, 7.4, 7.6, 4.0, 4.5, 4.2], covered=[True, True, True, True, False, True])
        self.assertNotIn("short_sleep", self.on())

    def test_a_too_short_to_be_real_night_does_not_count(self):
        self.sleep_nights([7.5, 7.4, 7.6, 4.0, 1.0, 4.2])
        self.assertNotIn("short_sleep", self.on())

    # ---- breathing rate: only if a value is stored ----
    def test_breathing_rate_is_silent_without_a_value(self):
        self.sleep_nights([7.5] * 10)
        self.assertIsNone(alerts.evaluate(self.c, TODAY)["resp_up"])

    def test_breathing_rate_up_three_nights_fires_once_the_value_exists(self):
        self.c.execute("ALTER TABLE sleep_night ADD COLUMN resp_rate REAL")
        self.sleep_nights([7.5] * 10)
        for k, v in enumerate(reversed([14.0, 14.2, 13.8, 14.1, 14.0, 13.9, 14.1, 16.0, 16.4, 16.2])):
            self.c.execute("UPDATE sleep_night SET resp_rate=? WHERE date=?", (v, date(k)))
        self.c.commit()
        a = alerts.evaluate(self.c, TODAY)["resp_up"]
        self.assertIsNotNone(a)
        self.assertIn("16.0, 16.4, 16.2", a["why"])
        self.c.execute("UPDATE sleep_night SET resp_rate=14 WHERE date=?", (date(1),))     # one normal night between
        self.c.commit()
        self.assertIsNone(alerts.evaluate(self.c, TODAY)["resp_up"])

    # ---- recovery ----
    def recoveries(self, values):
        for k, v in enumerate(reversed(values)):
            ok, val = (0, v[0]) if isinstance(v, tuple) else (1, v)
            self.c.execute("INSERT INTO estimates(date,kind,version,value,ok,why,row,inputs,computed_at) "
                           "VALUES(?,'recovery',?,?,?,NULL,'{}','{}',0)", (date(k), scores.current(self.c), val, ok))
        self.c.commit()

    def test_very_low_recovery_three_mornings_fires(self):
        self.recoveries([60, 70, 15, 12, 20])
        a = alerts.evaluate(self.c, TODAY)["low_recovery"]
        self.assertIsNotNone(a)
        self.assertIn("15, 12, 20%", a["why"])

    def test_very_low_recovery_single_morning_does_not_fire(self):
        self.recoveries([60, 70, 50, 55, 10])
        self.assertNotIn("low_recovery", self.on())

    def test_held_back_recovery_estimate_does_not_count(self):
        self.recoveries([60, 70, 15, (12,), 20])                   # (12,): failed its checks
        self.assertNotIn("low_recovery", self.on())

    # ---- Telegram: once per episode ----
    def test_telegram_sends_each_alert_once_per_episode(self):
        from test_telegram import FakeTelegram
        tg = FakeTelegram()
        real_eval = alerts.evaluate
        alerts.evaluate = lambda c, today=None: real_eval(c, TODAY)      # pin "today" to the synthetic nights
        self.addCleanup(setattr, alerts, "evaluate", real_eval)
        send = lambda: tr.send_nudges(self.c, "1:test", 42, {}, tg, uptime=0)
        health = lambda: [t for _, t in tg.sent if t.startswith("⚠️")]
        self.nights([50, 51, 49, 50, 50, 51, 50, 58, 59, 58])
        send(); send(); send()
        self.assertEqual(len(health()), 1)
        self.assertIn("Resting heart rate up", health()[0])
        self.assertIn("not a diagnosis", health()[0])
        self.c.execute("UPDATE daily_night SET rhr=50 WHERE date=?", (TODAY,))   # a normal night: episode over
        self.c.commit()
        send()
        self.assertEqual(len(health()), 1)                           # no message when it ends
        self.c.execute("UPDATE daily_night SET rhr=58 WHERE date=?", (TODAY,))
        self.c.commit()
        send()
        self.assertEqual(len(health()), 2)                           # a new episode is a new message

if __name__ == "__main__":
    unittest.main()
