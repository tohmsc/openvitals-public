#!/usr/bin/env python3
"""Tests for metrics.resp_rate (breathing rate from beat intervals) and its place in the sleep
estimate, on synthetic beat series with a known breathing rate.

Run:  .venv/bin/python -m unittest test_resp -v
"""
import math, os, random, sqlite3, tempfile, time, unittest

_tmp = tempfile.mkdtemp(prefix="whoop-test-")
os.environ["WHOOP_DB"] = os.path.join(_tmp, "t.db")
import db, metrics, scores  # noqa: E402

T0 = time.mktime(time.strptime("2026-11-10", "%Y-%m-%d")) - 3600     # 23:00 the night before


def strap_rows(start, seconds, breaths_per_min, base_ms=1100.0, rsa_ms=45.0, seed=1, miss=0.03):
    """ble_hr rows like the strap's: beats whose intervals swing with breathing, a little
    timing noise, a few missed beats, intervals grouped into the second they ended in."""
    rng = random.Random(seed)
    f = breaths_per_min / 60.0
    beats, t = [], start
    while t < start + seconds:
        rr = base_ms + rsa_ms * math.sin(2 * math.pi * f * (t - start)) + rng.gauss(0, 12)
        rr += 60 * math.sin(2 * math.pi * 0.03 * (t - start))          # a slow drift as well
        t += rr / 1000.0
        if rng.random() >= miss:
            beats.append(t)
    rows, j, prev = [], 0, None
    for s in range(int(start) + 1, int(start + seconds) + 1):
        xs = []
        while j < len(beats) and beats[j] <= s:
            if prev is not None:
                xs.append(round((beats[j] - prev) * 1000, 1))
            prev = beats[j]
            j += 1
        rows.append((float(s), round(60000 / base_ms), " ".join(str(x) for x in xs)))
    return rows


def memdb(rows):
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("CREATE TABLE ble_hr (ts REAL PRIMARY KEY, hr INTEGER, rr_ms TEXT)")
    c.executemany("INSERT INTO ble_hr VALUES (?,?,?)", rows)
    return c


class RespRate(unittest.TestCase):
    def test_recovers_a_known_breathing_rate(self):
        for bpm, seed in ((15.0, 1), (12.0, 2), (19.0, 3)):
            c = memdb(strap_rows(T0, 2 * 3600, bpm, seed=seed))
            self.addCleanup(c.close)
            r = metrics.resp_rate(c, T0, T0 + 2 * 3600)
            self.assertIsNotNone(r["bpm"], (bpm, r))
            self.assertAlmostEqual(r["bpm"], bpm, delta=1.0)
            self.assertGreaterEqual(r["windows"], metrics.RESP["min_windows"])

    def test_too_little_data_gives_none(self):
        c = memdb(strap_rows(T0, 5 * 60, 15.0))                           # five minutes
        self.addCleanup(c.close)
        r = metrics.resp_rate(c, T0, T0 + 3600)
        self.assertIsNone(r["bpm"])
        self.assertLess(r["windows"], metrics.RESP["min_windows"])

    def test_heart_rate_without_beat_intervals_gives_none(self):
        c = memdb([(float(T0 + s), 55, "") for s in range(3 * 3600)])
        self.addCleanup(c.close)
        self.assertIsNone(metrics.resp_rate(c, T0, T0 + 3 * 3600)["bpm"])

    def test_scores_uses_the_frozen_parameters(self):
        self.assertEqual(scores.RESP_RATE["r1"]["params"], metrics.RESP)


class InSleepEstimate(unittest.TestCase):
    def setUp(self):
        if os.path.exists(db.DB_PATH):
            os.remove(db.DB_PATH)
        self.c = db.connect()
        self.c.execute(scores.SCHEMA)
        self.c.execute("""CREATE TABLE IF NOT EXISTS sleep_night (date TEXT PRIMARY KEY, onset REAL, wake REAL,
            asleep_s REAL, awake_s REAL, wakeups INTEGER, rhr REAL, hrv REAL, usual_s REAL, status TEXT,
            headline TEXT, detail TEXT, segments TEXT, computed_at REAL)""")

    def tearDown(self):
        self.c.close()

    def night(self, date, rows):
        on, wk = rows[0][0], rows[-1][0] + 1
        self.c.executemany("INSERT OR REPLACE INTO ble_hr(ts,hr,rr_ms) VALUES(?,?,?)", rows)
        self.c.execute("INSERT INTO sleep_night(date,onset,wake,asleep_s,awake_s,wakeups,status) VALUES(?,?,?,?,?,?,?)",
                       (date, on, wk, (wk - on) * 0.9, (wk - on) * 0.1, 2, "done"))

    def test_sleep_row_has_breathing_rate(self):
        d = "2026-11-10"
        self.night(d, strap_rows(T0, 7 * 3600, 16.0))
        e = scores.sleep(self.c, d, 10.0, None, scores.MODELS["v1"]["sleep"])
        self.assertAlmostEqual(e["row"]["resp_rate"], 16.0, delta=1.0)
        self.assertEqual(e["inputs"]["resp_version"], "r1")
        self.assertGreaterEqual(e["inputs"]["resp_windows"], metrics.RESP["min_windows"])

    def test_no_breathing_rate_without_beat_intervals(self):
        d = "2026-11-10"
        self.night(d, [(float(T0 + s), 55, None) for s in range(7 * 3600)])
        e = scores.sleep(self.c, d, 10.0, None, scores.MODELS["v1"]["sleep"])
        self.assertIsNone(e["row"]["resp_rate"])
        self.assertTrue(e["ok"])                     # the sleep score itself doesn't need it


if __name__ == "__main__":
    unittest.main()
