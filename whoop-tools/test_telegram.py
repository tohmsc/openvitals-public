#!/usr/bin/env python3
"""Tests for telegram_report.py with a fake Telegram. No network.

Run:  .venv/bin/python -m unittest test_telegram -v
"""
import json, os, tempfile, time, unittest

_TMP = tempfile.mkdtemp(prefix="whoop-telegram-test-")
os.environ["WHOOP_DB"] = os.path.join(_TMP, "test.db")

import db, decode, telegram_report as tr  # noqa: E402


class FakeTelegram:
    def __init__(self, updates=()):
        self.updates, self.sent, self.fail = list(updates), [], False

    def __call__(self, token, method, **p):
        if method == "getUpdates":
            return [u for u in self.updates if u["update_id"] >= p.get("offset", 0)]
        if method == "sendMessage":
            if self.fail:
                raise RuntimeError("Telegram sendMessage: network down")
            self.sent.append((p["chat_id"], p["text"]))
            return {}
        raise AssertionError(method)


def msg(uid, chat, text):
    return {"update_id": uid, "message": {"chat": {"id": chat}, "text": text}}


class TelegramTest(unittest.TestCase):
    def setUp(self):
        self.c = db.connect()
        self.c.executescript(decode.EXTRA)
        for t in ("ble_hr", "api_recovery", "api_sleep", "api_profile", "device_soc", "daily_night", "clock_sync",
                  "ble_raw", "raw_archive", "sync_state"):
            self.c.execute(f"DELETE FROM {t}")
        self.c.commit()
        tr.ENV = os.path.join(_TMP, f"env-{self._testMethodName}")
        with open(tr.ENV, "w") as f:
            f.write("TELEGRAM_BOT_TOKEN=1:test\n")

    def tearDown(self):
        self.c.rollback()
        self.c.close()

    def day(self, offset=-1):
        """An hour at 60 bpm then an hour at 100 bpm, 1 Hz, with steady 1000/1040 ms beats."""
        start, _ = tr.day_bounds(offset)
        rows = []
        for i in range(7200):
            hr = 60 if i < 3600 else 100
            rows.append((start + 3600 + i, hr, "1000.0" if i % 2 else "1040.0"))
        self.c.executemany("INSERT INTO ble_hr(ts,hr,rr_ms) VALUES(?,?,?)", rows)
        self.c.commit()
        return start

    def test_stats_match_the_data(self):
        start = self.day()
        s = tr.day_stats(self.c, start, start + 86400)
        self.assertAlmostEqual(s["recorded_s"], 7199, delta=1)
        self.assertEqual((s["min"], s["max"]), (60, 100))
        self.assertAlmostEqual(s["avg"], 80, delta=0.1)
        self.assertAlmostEqual(s["resting"], 60, delta=0.1)          # calmest 5 minutes
        self.assertAlmostEqual(s["rmssd"], 40, delta=0.1)            # alternating 1000/1040 ms
        self.assertIsNone(tr.day_stats(self.c, start - 86400, start))

    def test_implausible_beats_are_ignored(self):
        start, _ = tr.day_bounds(-1)
        rr = ["1000.0", "1735.4 463.9 325.2", "1010.0", "90.0", "1000.0"] * 20   # noise as the strap really sends
        self.c.executemany("INSERT INTO ble_hr(ts,hr,rr_ms) VALUES(?,?,?)",
                           [(start + i, 70, x) for i, x in enumerate(rr)])
        self.c.commit()
        s = tr.day_stats(self.c, start, start + 86400)
        self.assertTrue(s["rmssd"] is None or s["rmssd"] < 120)     # jumps over 220 ms never counted

    def usual(self, rhr=55, hrv=50):
        for i in range(12):
            self.c.execute("INSERT INTO api_recovery(cycle_id,created_at,recovery_score,resting_hr,hrv_rmssd_ms) "
                           "VALUES(?,?,?,?,?)", (i, time.strftime("%Y-%m-%dT06:00:00Z", time.gmtime(time.time() - (i + 2) * 86400)),
                                                 60, rhr, hrv))
        self.c.commit()

    def soc(self, pct):
        self.c.execute("INSERT OR REPLACE INTO sync_state(key,value) VALUES('battery_now',?)",
                       (json.dumps({"soc": pct, "ts": time.time(), "src": "strap"}),))
        self.c.commit()

    def test_morning_message_reads_like_a_person(self):
        self.day(-1)                                                 # yesterday's daytime
        morning = self.day(0)                                        # last night: 01:00-03:00 today
        self.usual(rhr=60, hrv=40)
        self.soc(57.25)
        text = tr.compose(self.c, morning - 86400, morning, "Yesterday", morning)
        lines = text.split("\n")
        self.assertTrue(lines[0].startswith("🟢 "))
        self.assertIn(lines[0][2:], tr.SAY["green"])                 # a plain sentence, no date, no table
        self.assertEqual(lines[1], "")
        self.assertEqual(lines[2], "• Night: resting HR 60, right at your usual. HRV 40 ms, normal for you. Recovery about 70%.")
        self.assertEqual(lines[3], "• Yesterday was a normal day: avg HR 80, peak 100. Only 1h 59m recorded.")
        self.assertEqual(len(lines), 4)                              # battery is fine: not mentioned
        self.soc(18)
        text = tr.compose(self.c, morning - 86400, morning, "Yesterday", morning)
        self.assertTrue(text.endswith("• Strap battery 18%, charge it today."))
        self.assertLess(len(text), 360)

    def test_strained_after_a_big_day(self):
        morning = self.day(0)                                        # night: resting 60, HRV 40
        self.usual(rhr=50, hrv=70)                                   # far above usual -> strained
        y, _ = tr.day_bounds(-1)
        self.c.executemany("INSERT INTO ble_hr(ts,hr,rr_ms) VALUES(?,?,'')",
                           [(y + 10 * 3600 + i, 155, ) for i in range(3000)])   # 50 min hard yesterday
        self.c.commit()
        text = tr.compose(self.c, y, morning, "Yesterday", morning)
        head = text.split("\n")[0]
        self.assertTrue(head.startswith("🔴 "))
        self.assertTrue(head.endswith("Yesterday was a big day, so that tracks."))
        self.assertIn("• Night: resting HR 60, 10 above your usual. HRV 40 ms, lower than usual. Recovery about 5%.", text)
        self.assertIn("• Yesterday was a big day: 49m with your heart rate up, peak 155.", text)

    def test_the_call_follows_last_night(self):
        night = lambda rhr, hrv: {"resting": rhr, "rmssd": hrv}
        usual = {"rhr": 50, "hrv": 70}
        self.assertEqual(tr.call_for(night(50, 72), usual), "green")
        self.assertEqual(tr.call_for(night(50, 95), usual), "green")         # HRV well up: a good night
        self.assertEqual(tr.call_for(night(54, 70), usual), "yellow")        # resting HR a bit up
        self.assertEqual(tr.call_for(night(51, 60), usual), "yellow")        # HRV clearly down
        self.assertEqual(tr.call_for(night(55, 40), usual), "red")           # both well off
        self.assertEqual(tr.call_for(night(65, None), usual), "red")         # no HRV: resting HR decides
        self.assertEqual(tr.call_for(None, usual), "none")
        self.assertEqual(tr.call_for(night(50, 70), {"rhr": None, "hrv": None}), "new")
        self.assertEqual(tr.call_for(dict(night(140, 70), suspect=True), usual), "suspect")
        pct, basis = tr.learn.recovery(night(50, 70), usual)
        self.assertEqual((round(pct), basis), (70, "hrv+rhr"))               # a usual night: just into green
        self.assertEqual(tr.learn.recovery(dict(night(50, 70), n_rr=20), usual)[1], "rhr")   # too few clean beats

    def test_no_night_data_means_no_call(self):
        morning = self.day(-1) + 86400                               # only yesterday's daytime exists
        self.c.execute("DELETE FROM ble_hr WHERE ts >= ?", (morning - 2 * 3600,))
        self.usual()
        text = tr.compose(self.c, morning - 86400, morning, "Yesterday", morning)
        self.assertTrue(text.startswith("⚪ No read on you today"))
        self.assertNotIn("Night:", text)
        self.assertIn("• Yesterday was a normal day: avg HR 80", text)
        empty = tr.compose(self.c, morning - 3 * 86400, morning - 2 * 86400, "Yesterday", morning)
        self.assertIn("• Nothing recorded yesterday.", empty)

    def test_usual_switches_to_your_own_nights(self):
        morning = self.day(0)
        self.usual(rhr=40, hrv=90)                                   # old WHOOP archive says 40 / 90
        text = tr.compose(self.c, morning - 86400, morning, "Yesterday", morning)
        self.assertIn("20 above your usual", text)
        self.assertEqual(self.c.execute("SELECT rhr FROM daily_night").fetchone()["rhr"], 60)   # tonight remembered
        for i in range(1, 6):                                        # five earlier strap nights at 61 / 42
            d = time.strftime("%Y-%m-%d", time.localtime(morning - i * 86400 + 3600))
            self.c.execute("INSERT OR REPLACE INTO daily_night(date,rhr,hrv) VALUES(?,?,?)", (d, 61, 42))
        self.c.commit()
        text = tr.compose(self.c, morning - 86400, morning, "Yesterday", morning)
        self.assertIn("resting HR 60, right at your usual. HRV 40 ms, normal for you.", text)
        self.assertTrue(text.startswith("🟢"))

    # ---- nudges ----
    def linked(self):
        with open(tr.ENV, "a") as f:
            f.write("TELEGRAM_CHAT_ID=333\n")
        db.set_state(self.c, "telegram_last_report", time.strftime("%Y-%m-%d"))   # morning message already out
        return tr.day_bounds(0)[0] + 12 * 3600                       # noon today

    def recent(self, seconds_ago=5):
        self.c.execute("INSERT OR REPLACE INTO ble_hr(ts,hr,rr_ms) VALUES(?,70,'')", (time.time() - seconds_ago,))
        self.c.commit()

    def test_battery_nudge_once_then_charged(self):
        noon = self.linked(); self.recent(); tg = FakeTelegram()
        self.soc(45)
        self.assertIsNone(tr.run_due(api=tg, now=noon, uptime=3600))
        self.soc(17)
        self.assertEqual(tr.run_due(api=tg, now=noon, uptime=3600), "nudged")
        self.assertIn("Strap battery is at 17%", tg.sent[-1][1])
        self.soc(15); self.assertIsNone(tr.run_due(api=tg, now=noon, uptime=3600))      # not again
        self.soc(60); self.assertIsNone(tr.run_due(api=tg, now=noon, uptime=3600))      # charging: quiet
        self.soc(96)
        self.assertEqual(tr.run_due(api=tg, now=noon, uptime=3600), "nudged")
        self.assertIn("Strap's charged (96%)", tg.sent[-1][1])
        self.assertEqual(len(tg.sent), 2)

    def test_strap_missing_and_back(self):
        noon = self.linked(); tg = FakeTelegram()
        self.recent(seconds_ago=7 * 3600)
        self.assertIsNone(tr.run_due(api=tg, now=noon, uptime=120))          # daemon just started: wait
        self.assertEqual(tr.run_due(api=tg, now=noon, uptime=3600), "nudged")
        self.assertIn("haven't seen your strap for 7 hours", tg.sent[-1][1])
        self.assertIsNone(tr.run_due(api=tg, now=noon, uptime=3600))
        self.recent()
        self.assertEqual(tr.run_due(api=tg, now=noon, uptime=3600), "nudged")
        self.assertIn("Strap's back", tg.sent[-1][1])

    def test_clock_reset_nudge_and_quiet_hours(self):
        noon = self.linked(); self.recent(); tg = FakeTelegram()
        self.c.execute("INSERT INTO clock_sync(ts,rtc,offset) VALUES(?,?,?)", (time.time(), 31_500_000, 1.7e9))
        self.c.commit()
        night = tr.day_bounds(0)[0] + 23 * 3600
        self.assertIsNone(tr.run_due(api=tg, now=night, uptime=3600))        # 23:00: stay quiet
        self.assertEqual(tr.run_due(api=tg, now=noon, uptime=3600), "nudged")
        self.assertIn("clock has reset", tg.sent[-1][1])

    def test_status_and_now_commands(self):
        noon = self.linked(); self.recent(); self.soc(64)
        db.set_state(self.c, "live_hr_now", '{"hr": 72, "ts": %f}' % time.time())
        tg = FakeTelegram([msg(1, 999, "/status"), msg(2, 333, "/status"), msg(3, 333, "/now"), msg(4, 333, "hi")])
        self.assertEqual(tr.run_due(api=tg, now=noon, uptime=3600), "replied")
        self.assertEqual([c for c, _ in tg.sent], ["333", "333", "333"])     # the stranger (999) gets nothing
        self.assertIn("• Strap connected, heart rate 72 right now.", tg.sent[0][1])
        self.assertIn("• Battery 64%.", tg.sent[0][1])
        self.assertIn("Today so far", tg.sent[1][1])
        self.assertIn("/status", tg.sent[2][1])
        self.assertIsNone(tr.run_due(api=tg, now=noon, uptime=3600))         # answered once

    def test_only_the_code_sender_gets_paired(self):
        code = tr.pair_code(self.c)
        self.assertEqual(code, tr.pair_code(self.c))                 # stable until used
        tg = FakeTelegram([msg(1, 111, "/start"), msg(2, 222, "hello bot")])
        self.assertIsNone(tr.run_due(api=tg))                        # strangers are ignored
        self.assertEqual(tg.sent, [])
        self.assertNotIn("TELEGRAM_CHAT_ID", tr.env())
        tg.updates.append(msg(3, 333, f"my code is {code}"))
        self.day(0)
        self.assertEqual(tr.run_due(api=tg), "paired")
        self.assertEqual(tr.env()["TELEGRAM_CHAT_ID"], "333")
        self.assertEqual([c for c, _ in tg.sent], [333, 333])        # greeting + today so far
        self.assertIn("Today so far", tg.sent[1][1])

    def test_sends_once_a_day_after_the_hour(self):
        with open(tr.ENV, "a") as f:
            f.write("TELEGRAM_CHAT_ID=333\nTELEGRAM_DAILY_HOUR=8\n")
        self.day(-1)
        tg = FakeTelegram()
        midnight, _ = tr.day_bounds(0)
        self.assertIsNone(tr.run_due(api=tg, now=midnight + 7 * 3600, uptime=60))       # 07:00: too early
        self.assertEqual(tr.run_due(api=tg, now=midnight + 8 * 3600 + 5, uptime=60), "sent")
        self.assertIsNone(tr.run_due(api=tg, now=midnight + 9 * 3600, uptime=60))       # not twice
        self.assertIsNone(tr.run_due(api=tg, now=midnight + 23 * 3600, uptime=60))
        self.assertEqual(len(tg.sent), 1)
        self.assertEqual(tg.sent[0][0], "333")
        self.assertIn("yesterday", tg.sent[0][1].lower())
        self.assertEqual(tr.run_due(api=tg, now=midnight + 86400 + 15 * 3600, uptime=60), "sent")   # next day, laptop woke late

    def test_failed_send_is_retried(self):
        with open(tr.ENV, "a") as f:
            f.write("TELEGRAM_CHAT_ID=333\n")
        tg = FakeTelegram(); tg.fail = True
        midnight, _ = tr.day_bounds(0)
        with self.assertRaises(RuntimeError):
            tr.run_due(api=tg, now=midnight + 9 * 3600, uptime=60)
        tg.fail = False
        self.assertEqual(tr.run_due(api=tg, now=midnight + 9 * 3600 + 60, uptime=60), "sent")

    def test_coverage_explains_each_gap(self):
        import metrics
        c = self.c
        c.execute("DELETE FROM host_log")
        a = int(time.time()) - 20000
        rows = [(a + s, 60, "") for s in list(range(0, 1000)) + list(range(4600, 5000))]
        c.executemany("INSERT INTO ble_hr(ts,hr,rr_ms) VALUES(?,?,?)", rows)
        c.executemany("INSERT INTO host_log(start,end,state) VALUES(?,?,?)",
                      [(a, a + 900, "linked"), (a + 900, a + 4500, "asleep"), (a + 4500, a + 10000, "linked")])
        db.set_state(c, "history_ts", a + 2000)              # the strap has been read up to here
        cov = metrics.coverage(c, a, a + 10000)
        self.assertEqual(cov["recorded_s"], 1400)
        self.assertAlmostEqual(cov["share"], 0.14)
        (big, why_big), (small, why_small) = [((g["start"] - a, g["end"] - a), g["why"]) for g in cov["gaps"]]
        self.assertEqual((big, small), ((5000, 10000), (1000, 4600)))   # longest first
        self.assertEqual(why_big, "still on the strap, comes in at the next sync")
        self.assertEqual(why_small, "still on the strap, comes in at the next sync (the Mac was asleep)")
        self.assertAlmostEqual(cov["pending_s"], 5000 + 2600)
        self.assertAlmostEqual(cov["nothing_s"], 1000)
        db.set_state(c, "history_ts", a + 20000)             # read past both: nothing more will come
        cov = metrics.coverage(c, a, a + 10000)
        whys = [g["why"] for g in cov["gaps"]]
        self.assertEqual(whys[0], "the strap was connected but sent no heart rate (off the wrist?)")
        self.assertTrue(whys[1].endswith("; the Mac was asleep"))
        self.assertEqual(cov["pending_s"], 0)
        self.assertEqual(metrics.coverage(c, a + 30000, a + 40000)["span_s"], 0)   # the future isn't a gap

    def test_morning_message_says_why_strain_was_held_back(self):
        import scores
        c = self.c
        c.execute(scores.SCHEMA)
        c.execute("DELETE FROM estimates")
        y, morning = tr.day_bounds(-1)
        c.executemany("INSERT INTO ble_hr(ts,hr,rr_ms) VALUES(?,?,'')",
                      [(y + 10 * 3600 + i, 70) for i in range(7200)])          # 10:00-12:00 only
        date = time.strftime("%Y-%m-%d", time.localtime(y + 43200))
        c.execute("INSERT INTO estimates(date,kind,version,value,ok,why,row,inputs,computed_at) "
                  "VALUES(?,?,?,?,?,?,?,?,?)", (date, "strain", scores.current(c), 5.0, 0, "too little", "{}", "{}", 0))
        db.set_state(c, "history_ts", time.time())
        text = tr.compose(c, y, morning, "Yesterday", morning)
        line = next(l for l in text.split("\n") if "Strain held back" in l)
        self.assertIn("the strap recorded 8% of yesterday (needs 85%)", line)
        self.assertIn("Biggest gap 12:00-04:00 (16h 0m): the strap had nothing stored for it", line)
        c.execute("UPDATE estimates SET ok = 1")                          # shown: nothing to explain
        self.assertNotIn("Strain held back", tr.compose(c, y, morning, "Yesterday", morning))

    def test_morning_message_waits_for_last_night_to_arrive(self):
        # laptop slept all night: at 08:00 the strap's stored history has only been read up to 23:00
        tg = FakeTelegram()
        with open(tr.ENV, "a") as f:
            f.write("TELEGRAM_CHAT_ID=333\n")
        midnight = tr.day_bounds(0)[0]
        db.set_state(self.c, "history_ts", midnight - 3600)
        self.assertIsNone(tr.run_due(api=tg, now=midnight + 8 * 3600 + 5, uptime=60))       # wait for it
        self.assertIsNone(tr.run_due(api=tg, now=midnight + 8 * 3600 + 600, uptime=60))
        db.set_state(self.c, "history_ts", midnight + 8 * 3600)                              # the night is in
        self.assertEqual(tr.run_due(api=tg, now=midnight + 8 * 3600 + 630, uptime=60), "sent")
        self.assertTrue(tg.sent[-1][1].endswith(tr.ASK))                                     # asks how you feel

    def test_morning_message_goes_out_anyway_if_the_strap_never_shows(self):
        tg = FakeTelegram()
        with open(tr.ENV, "a") as f:
            f.write("TELEGRAM_CHAT_ID=333\n")
        midnight = tr.day_bounds(0)[0]
        db.set_state(self.c, "history_ts", midnight - 3600)
        self.assertIsNone(tr.run_due(api=tg, now=midnight + 8 * 3600, uptime=60))
        self.assertIsNone(tr.run_due(api=tg, now=midnight + 8 * 3600 + tr.REPORT_GRACE - 60, uptime=60))
        self.assertEqual(tr.run_due(api=tg, now=midnight + 8 * 3600 + tr.REPORT_GRACE + 1, uptime=60), "sent")

    def test_waking_the_laptop_counts_as_a_fresh_start(self):
        # the "strap missing" nudge gives a fresh daemon time to connect; a laptop that just woke needs the same
        tr._awake.update(since=time.time() - 9 * 3600, tick=time.time() - 8 * 3600)          # last tick 8 h ago
        self.assertLess(tr.awake_for(), 5)
        tr._awake.update(since=time.time() - 9 * 3600, tick=time.time() - 30)                # ticking all along
        self.assertGreater(tr.awake_for(), 8 * 3600)

    def test_old_whoop_scores_are_not_your_usual(self):
        morning = self.day(0)
        self.c.execute("INSERT INTO api_recovery(cycle_id,created_at,recovery_score,resting_hr,hrv_rmssd_ms) "
                       "VALUES(1,'2025-06-20T06:00:00Z',60,40,90)")                         # 17 months old
        self.c.commit()
        text = tr.compose(self.c, morning - 86400, morning, "Yesterday", morning)
        self.assertTrue(text.startswith("⚪ Still learning your normal"))
        self.assertNotIn("your usual", text)

    def test_feedback_tunes_the_call(self):
        tg = FakeTelegram([msg(1, 333, "/good")])
        noon = self.linked()
        today = time.strftime("%Y-%m-%d")
        # this morning: recovery estimated at 33% (red, just under the line), and you say you feel good
        self.c.execute("INSERT INTO daily_night(date,rhr,hrv,score,call) VALUES(?,?,?,?,?)", (today, 60, 40, 33.0, "red"))
        self.c.commit()
        self.assertEqual(tr.run_due(api=tg, now=noon, uptime=3600), "replied")
        self.assertIn("you feel good", tg.sent[-1][1])
        self.assertIn("less cautious", tg.sent[-1][1])
        th = tr.learn.thresholds(self.c)
        self.assertEqual((th["red_max"], th["feedback"]), (32.0, 1))
        self.assertEqual(tr.learn.zone(33.0, th["green_min"], th["red_max"]), "yellow")      # same night, next time
        # ...but today's own call does not flip after the fact
        self.assertEqual(tr.learn.thresholds(self.c, before=today)["red_max"], 34.0)
        # changing your mind replaces the answer instead of stacking
        tg.updates.append(msg(2, 333, "rough"))
        tr.run_due(api=tg, now=noon, uptime=3600)
        self.assertIn("so we agree", tg.sent[-1][1])
        self.assertEqual(tr.learn.thresholds(self.c)["red_max"], 34.0)
        tg.updates.append(msg(3, 333, "/learned"))
        tr.run_due(api=tg, now=noon, uptime=3600)
        self.assertIn("You've told me how you felt on 1 day", tg.sent[-1][1])

    def test_usual_adapts_to_how_steady_you_are(self):
        # same +2 bpm night: off for a steady heart, ordinary for one that swings
        night = {"resting": 57, "rmssd": None}
        days = [time.strftime("%Y-%m-%d", time.localtime(time.time() - i * 86400)) for i in range(1, 11)]
        today = time.strftime("%Y-%m-%d")
        for d, rhr in zip(days, [55, 55, 55, 56, 54, 55, 55, 56, 54, 55]):
            self.c.execute("INSERT INTO daily_night(date,rhr,hrv) VALUES(?,?,?)", (d, rhr, 50))
        steady = tr.learn.usual(self.c, today)
        self.assertEqual((steady["rhr"], steady["source"], steady["nights"]), (55, "own", 10))
        self.assertEqual(tr.call_for(night, steady), "yellow")
        self.c.execute("DELETE FROM daily_night")
        for d, rhr in zip(days, [48, 62, 55, 50, 60, 55, 47, 63, 55, 55]):
            self.c.execute("INSERT INTO daily_night(date,rhr,hrv) VALUES(?,?,?)", (d, rhr, 50))
        self.assertEqual(tr.call_for(night, tr.learn.usual(self.c, today)), "green")
        self.c.commit()

    def test_noisy_beats_while_moving_do_not_count_as_hrv(self):
        # at 100 bpm a beat takes ~600 ms: the 1000/1040 ms "beats" here are missed-beat artefacts
        start, _ = tr.day_bounds(-1)
        self.c.executemany("INSERT INTO ble_hr(ts,hr,rr_ms) VALUES(?,?,?)",
                           [(start + i, 100, "1000.0" if i % 2 else "1040.0") for i in range(600)])
        self.c.commit()
        s = tr.day_stats(self.c, start, start + 86400)
        self.assertEqual((s["n_rr"], s["rmssd"]), (0, None))

    def archive(self, days=120):
        """WHOOP-scored days where recovery follows HRV (mostly) and resting HR, as it does for real."""
        import math, random
        rnd = random.Random(7)
        now = time.time()
        for i in range(days):
            hrv, rhr = 80 * math.exp(rnd.gauss(0, 0.2)), 46 + rnd.gauss(0, 2.5)
            score = max(1, min(99, 65 + 55 * math.log(hrv / 80) - 1.5 * (rhr - 46) + rnd.gauss(0, 6)))
            when = time.strftime("%Y-%m-%dT06:00:00Z", time.gmtime(now - (500 + days - i) * 86400))   # over a year ago
            self.c.execute("INSERT INTO api_recovery(cycle_id,created_at,recovery_score,resting_hr,hrv_rmssd_ms) "
                           "VALUES(?,?,?,?,?)", (1000 + i, when, score, rhr, hrv))
        self.c.commit()

    def test_recovery_model_is_fitted_on_your_whoop_history(self):
        self.archive()
        m = tr.learn.model(self.c)
        self.assertEqual(m["source"], "archive")
        self.assertGreater(m["both"][2], 5)                  # HRV up -> recovery up
        self.assertLess(m["both"][1], 0)                     # resting HR up -> recovery down
        self.assertGreater(m["zone_match"], 0.6)             # measured on days the fit never saw
        self.assertLess(m["error"], 12)
        usual = {"rhr": 55, "hrv": 50}                       # strap-era usual, on the strap's own scale
        good, bad = tr.learn.recovery({"resting": 54, "rmssd": 62}, usual, m), tr.learn.recovery({"resting": 60, "rmssd": 32}, usual, m)
        self.assertGreater(good[0], 67); self.assertLess(bad[0], 40)
        # the archive is old: it is shown for comparison, never used as your usual
        u = tr.learn.usual(self.c, time.strftime("%Y-%m-%d"))
        self.assertEqual((u["source"], u["rhr"]), (None, None))
        self.assertAlmostEqual(u["then"]["rhr"], 46, delta=1.5)
        morning = self.day(0)
        text = tr.compose(self.c, morning - 86400, morning, "Yesterday", morning)
        self.assertRegex(text, r"Night: resting HR 60 \(4\d on WHOOP in 20\d\d\)\.")
        self.assertIn("modelled on", tr.learn.learned_text(self.c))

    def test_resting_hr_up_several_mornings_running_is_called_out(self):
        morning = self.day(0)                                # last night: resting 60
        for i in range(1, 8):                                # usual 52; the last two mornings were 60 too
            d = time.strftime("%Y-%m-%d", time.localtime(morning - i * 86400 + 3600))
            self.c.execute("INSERT INTO daily_night(date,rhr,hrv) VALUES(?,?,?)", (d, 60 if i <= 2 else 52, 40))
        self.c.commit()
        text = tr.compose(self.c, morning - 86400, morning, "Yesterday", morning)
        self.assertIn("Resting HR has been up 3 mornings in a row", text)

    def test_unbelievable_reading_is_not_a_verdict(self):
        start, _ = tr.day_bounds(0)
        self.c.executemany("INSERT INTO ble_hr(ts,hr,rr_ms) VALUES(?,?,'')", [(start + 3600 + i, 140, ) for i in range(7200)])
        self.c.commit()
        self.usual()
        text = tr.compose(self.c, start - 86400, start, "Yesterday", start)
        self.assertTrue(text.startswith("⚪ No read on you today: last night's numbers don't look like a real measurement."))
        self.assertIsNone(self.c.execute("SELECT 1 FROM daily_night").fetchone())    # and it is not learned from

    def test_night_uses_your_own_hours_and_the_message_waits_for_them(self):
        # you sleep 01:00-09:30: the default 22:00-06:00 would cut your night in half
        for i in range(40):
            day = time.strftime("%Y-%m-%d", time.gmtime(time.time() - (400 + i) * 86400))
            self.c.execute("INSERT INTO api_sleep(id,start,end,nap,total_in_bed_ms,raw) VALUES(?,?,?,0,1,?)",
                           (f"s{i}", f"{day}T01:00:00.000Z", f"{day}T09:30:00.000Z", '{"timezone_offset": "+00:00"}'))
        self.c.commit()
        a, b = tr.learn.night_window(self.c)
        self.assertEqual((a, b), (0.0, 9.5))
        tg = FakeTelegram()
        with open(tr.ENV, "a") as f:
            f.write("TELEGRAM_CHAT_ID=333\n")
        midnight = tr.day_bounds(0)[0]
        self.assertIsNone(tr.run_due(api=tg, now=midnight + 8 * 3600 + 5, uptime=60))        # 08:00: still your night
        self.assertEqual(tr.run_due(api=tg, now=midnight + 9.6 * 3600, uptime=60), "sent")

    def test_does_nothing_without_a_token(self):
        open(tr.ENV, "w").close()
        self.assertFalse(tr.configured())
        self.assertIsNone(tr.run_due(api=FakeTelegram()))


if __name__ == "__main__":
    unittest.main()
