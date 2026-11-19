#!/usr/bin/env python3
"""Tests for the strap signals beyond heart rate: parsing the version-24 record (whoop.py),
the per-minute table and off-wrist stretches (decode.py), the coverage helpers (metrics.py)
and the nightly skin temperature (signals.py). Synthetic frames only.

Run:  .venv/bin/python -m unittest test_signals -v
"""
import os, struct, tempfile, time, unittest

_TMP = tempfile.mkdtemp(prefix="whoop-test-")
os.environ["WHOOP_DB"] = os.path.join(_TMP, "test.db")

import db, decode, metrics, signals, whoop  # noqa: E402

T = 1_795_000_020          # a minute boundary
TABLES = ("ble_raw", "ble_hr", "sync_state", "motion_min", "strap_min", "offwrist")


def rec(i, ts=None, seq=None, **kw):
    return whoop.history_record(seq if seq is not None else 1000 + i, ts if ts is not None else T + i, **kw)


class ParseTest(unittest.TestCase):
    def test_version_24_fields(self):
        p = whoop.parse_historical(whoop.history_record(77, T, hr=52, rr=(1100,), accel=(0.5, -0.25, 0.75),
                                                        optical=(601, 912), temp=343, steps=4321))
        self.assertEqual((p["seq"], p["ts"], p["hr"], p["rr_count"]), (77, T, 52, 1))
        self.assertEqual(p["rr_ms"], [1100, 0, 0, 0])
        self.assertEqual(p["accel_g"], [0.5, -0.25, 0.75])
        self.assertEqual(p["optical"], [601, 912])
        self.assertEqual((p["skin_temp_raw"], p["steps_total"]), (343, 4321))
        self.assertNotIn("skin_contact", p)         # d48 is part of a float, not a contact flag

    def test_offsets_match_a_real_record_layout(self):
        # a stored record from the strap (sleeping, 04:46): temp 333 at d69, steps 4304 at d85
        d = bytearray(93)
        struct.pack_into("<HHHHHH", d, 61, 600, 635, 848, 633, 333, 2976)
        struct.pack_into("<H", d, 85, 4304)
        p = whoop.parse_historical(bytes([0x2F, 24, 5]) + bytes(d))
        self.assertEqual((p["optical"], p["skin_temp_raw"], p["steps_total"]), ([600, 848], 333, 4304))

    def test_other_versions_get_no_sensor_fields(self):
        inner = bytearray(whoop.history_record(5, T)); inner[1] = 25
        p = whoop.parse_historical(bytes(inner))
        for k in ("accel_g", "optical", "skin_temp_raw", "steps_total"):
            self.assertNotIn(k, p)


class DecodeTest(unittest.TestCase):
    def setUp(self):
        self.c = db.connect()
        self.c.executescript(decode.EXTRA)
        for t in TABLES:
            self.c.execute(f"DELETE FROM {t}")
        self.c.commit()

    def tearDown(self):
        self.c.rollback()
        for t in TABLES:
            self.c.execute(f"DELETE FROM {t}")
        self.c.commit()
        self.c.close()

    def load(self, frames):
        for f in frames:
            self.c.execute("INSERT INTO ble_raw(ts,channel,ptype,hex) VALUES(?,?,?,?)", (0, "0005", f[0], f.hex()))
        self.c.commit()

    def table(self):
        return [tuple(r) for r in self.c.execute(
            "SELECT minute,n,temp_sum,temp_n,opt_a_sum,opt_b_sum,steps FROM strap_min ORDER BY minute")]

    def night(self):
        """Two minutes worn, a 5-minute stretch off the wrist, then one more minute."""
        frames, steps = [], 100
        for i in range(120):                                  # walking for the first 30 s
            steps += 2 if i < 30 else 0
            frames.append(rec(i, temp=330 + i // 60, steps=steps, optical=(600, 900)))
        steps += 40                                           # counted while off the wrist
        for i in range(60):                                   # record counter carries straight on
            frames.append(rec(120 + i, ts=T + 420 + i, temp=300, steps=steps))
        return frames

    def test_minutes_steps_and_off_wrist(self):
        self.load(self.night())
        decode.run(quiet=True)
        rows = self.table()
        self.assertEqual([r[0] for r in rows], [T, T + 60, T + 420])
        self.assertEqual([r[1] for r in rows], [60, 60, 60])                   # seconds worn
        self.assertEqual([r[2] / r[3] for r in rows], [330, 331, 300])         # mean raw temperature
        self.assertEqual(rows[0][4] / rows[0][1], 600)
        self.assertEqual([r[6] for r in rows], [58, 0, 0])   # 29 steps of 2; not the 40 while off
        off = [tuple(r) for r in self.c.execute("SELECT start, end FROM offwrist")]
        self.assertEqual(off, [(T + 120.0, T + 420.0)])

    def test_runs_and_repeats_add_up_to_the_same(self):
        frames = self.night()
        self.load(frames[:50])
        decode.run(quiet=True)
        self.load(frames[40:130])                             # overlaps the first run
        decode.run(quiet=True)
        self.load(frames[125:] + frames[:3])                  # and repeats old ones again
        decode.run(quiet=True)
        step = (self.table(), [tuple(r) for r in self.c.execute("SELECT * FROM offwrist")])
        decode.run(full=True, quiet=True)
        full = (self.table(), [tuple(r) for r in self.c.execute("SELECT * FROM offwrist")])
        self.assertEqual(step, full)
        self.assertEqual([r[1] for r in full[0]], [60, 60, 60])
        self.assertEqual(len(full[1]), 1)

    def test_lost_records_are_not_off_wrist(self):
        # a jump in the record counter means records we never got, not a strap that was taken off
        self.load([rec(0), rec(1), rec(2, ts=T + 600, seq=5000)])
        decode.run(quiet=True)
        self.assertEqual(self.c.execute("SELECT COUNT(*) n FROM offwrist").fetchone()["n"], 0)

    def test_counter_reset_and_jumps_are_not_steps(self):
        self.load([rec(0, steps=56246), rec(1, steps=28), rec(2, steps=40), rec(3, steps=40 + 500)])
        decode.run(quiet=True)
        self.assertEqual(self.table()[0][6], 12)              # only 28 -> 40 (the strap adds ~12 at once)

    def test_bad_temperature_and_unset_clock_are_skipped(self):
        self.load([rec(0, temp=0), rec(1, temp=340), rec(2, ts=31_500_000)])
        decode.run(quiet=True)
        rows = self.table()
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0][1], rows[0][2], rows[0][3]), (2, 340, 1))

    def test_backfill_from_frames_decoded_before_the_table_existed(self):
        self.load(self.night())
        decode.run(quiet=True)
        before = self.table()
        self.c.execute("DELETE FROM strap_min"); self.c.execute("DELETE FROM offwrist")
        self.c.execute("DELETE FROM sync_state WHERE key LIKE 'strap%'"); self.c.commit()
        self.load([rec(400, ts=T + 470)])                     # the next decode run has new frames
        decode.run(quiet=True)
        after = self.table()
        self.assertEqual(after[:2], before[:2])
        self.assertEqual(after[2][1], 61)
        self.assertEqual(self.c.execute("SELECT COUNT(*) n FROM offwrist").fetchone()["n"], 1)

    def test_coverage_tells_off_wrist_from_unknown(self):
        self.load(self.night())
        decode.run(quiet=True)
        cov = metrics.wear_coverage(self.c, T, T + 600)
        self.assertEqual(cov, {"worn_s": 180.0, "off_s": 300.0, "unknown_s": 120.0})
        self.assertEqual(metrics.worn_seconds(self.c, T + 30, T + 90), 60.0)    # half of two minutes
        self.assertEqual(metrics.off_wrist_seconds(self.c, T + 400, T + 1000), 20.0)
        self.assertEqual(metrics.steps(self.c, T, T + 600), 58)
        self.assertIsNone(metrics.steps(self.c, T + 3600, T + 7200))


class NightTest(unittest.TestCase):
    def setUp(self):
        self.c = db.connect()
        self.c.executescript(decode.EXTRA)
        self.c.execute(signals.SCHEMA)
        import sleep
        self.c.execute(sleep.SCHEMA)
        for t in ("strap_min", "sleep_night", "skin_night"):
            self.c.execute(f"DELETE FROM {t}")
        self.c.commit()

    def tearDown(self):
        self.c.rollback()
        for t in ("strap_min", "sleep_night", "skin_night"):
            self.c.execute(f"DELETE FROM {t}")
        self.c.commit()
        self.c.close()

    def add_night(self, days_ago, temp, hours=6.0):
        wake = (int(time.time()) // 86400 - days_ago) * 86400 + 6 * 3600
        onset = wake - 7 * 3600
        date = time.strftime("%Y-%m-%d", time.gmtime(wake))
        self.c.execute("INSERT INTO sleep_night(date,onset,wake,status) VALUES(?,?,?,?)", (date, onset, wake, "done"))
        for m in range(onset, onset + int(hours * 3600), 60):
            self.c.execute("INSERT INTO strap_min(minute,n,temp_sum,temp_n) VALUES(?,?,?,?)", (m, 60, temp * 60, 60))
        self.c.commit()
        return date

    def test_learns_then_shows_the_change(self):
        for d, t in ((5, 340), (4, 342), (3, 341)):
            self.add_night(d, t)
        warm = self.add_night(2, 349)
        rows = {r["date"]: r for r in signals.update(self.c)}
        self.assertEqual([r["status"] for r in sorted(rows.values(), key=lambda r: r["date"])],
                         ["learning", "learning", "learning", "ok"])
        self.assertAlmostEqual(rows[warm]["usual_raw"], 341)
        self.assertAlmostEqual(rows[warm]["dev_raw"], 8)
        self.assertAlmostEqual(rows[warm]["dev_c"], 0.8)

    def test_a_short_night_is_held_back(self):
        d = self.add_night(1, 345, hours=2)
        r = signals.update(self.c)[0]
        self.assertEqual((r["date"], r["status"], r["temp_raw"], r["dev_raw"]), (d, "too little", None, None))

    def test_off_wrist_minutes_do_not_count(self):
        d = self.add_night(1, 345)
        self.c.execute("UPDATE strap_min SET n=10, temp_n=10, temp_sum=3000 WHERE minute % 120 = 0")  # half the minutes
        self.c.commit()
        r = signals.update(self.c)[0]
        self.assertEqual((r["date"], r["minutes"], r["temp_raw"]), (d, 180, 345))


if __name__ == "__main__":
    unittest.main()
