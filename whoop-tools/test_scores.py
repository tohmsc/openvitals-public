#!/usr/bin/env python3
"""Tests for scores.py: the strain curve and zones, the checks that hold an estimate back,
and that versions sit side by side.   .venv/bin/python -m unittest test_scores"""
import json, math, os, tempfile, time, unittest

_tmp = tempfile.mkdtemp()
os.environ["WHOOP_DB"] = os.path.join(_tmp, "t.db")
import db, scores  # noqa: E402

M = scores.MODELS["v1"]


def at(date, hour):
    return time.mktime(time.strptime(date, "%Y-%m-%d")) + hour * 3600


class Base(unittest.TestCase):
    def setUp(self):
        if os.path.exists(db.DB_PATH):
            os.remove(db.DB_PATH)
        self.c = db.connect()
        self.c.execute(scores.SCHEMA)

    def tearDown(self):
        self.c.close()

    def hr(self, a, b, bpm, rr=None):
        self.c.executemany("INSERT OR REPLACE INTO ble_hr(ts,hr,rr_ms) VALUES(?,?,?)",
                           [(float(t), bpm(t) if callable(bpm) else bpm, rr) for t in range(int(a), int(b))])

    def night(self, date, onset_h=-1.0, wake_h=7.0, asleep_h=7.5, status="done"):
        self.c.execute("""CREATE TABLE IF NOT EXISTS sleep_night (date TEXT PRIMARY KEY, onset REAL, wake REAL,
            asleep_s REAL, awake_s REAL, wakeups INTEGER, rhr REAL, hrv REAL, usual_s REAL, status TEXT,
            headline TEXT, detail TEXT, segments TEXT, computed_at REAL)""")
        on, wk = at(date, onset_h), at(date, wake_h)
        self.c.execute("INSERT OR REPLACE INTO sleep_night(date,onset,wake,asleep_s,awake_s,wakeups,status) VALUES(?,?,?,?,?,?,?)",
                       (date, on, wk, asleep_h * 3600, (wk - on) - asleep_h * 3600, 2, status))


class Curve(unittest.TestCase):
    def test_strain_map_bounds_and_order(self):
        vals = [scores.strain_map(x, M["strain"]) for x in (0, 1, 50, 150, 400, 2000, 1e6)]
        self.assertEqual(vals[0], 0.0)
        self.assertTrue(all(a < b for a, b in zip(vals[1:], vals[2:])))   # more load, more strain
        self.assertLess(vals[-1], 21.0)

    def test_need_from_strain_matches_whoop_api_example(self):
        # WHOOP's API docs: strain 5.2951527 -> need_from_recent_strain_milli 208595
        self.assertAlmostEqual(scores.need_from_strain(5.2951527) * 3600e3, 208595, delta=1)

    def test_workout_example(self):
        # one of the archive workouts: 25 min, mostly zones 1-3, WHOOP said 8.08
        mins = [0, 519131 / 6e4, 722904 / 6e4, 276861 / 6e4, 10574 / 6e4, 0]
        load = sum(w * m for w, m in zip(M["strain"]["weights"], mins))
        self.assertAlmostEqual(scores.strain_map(load, M["strain"]), 8.08, delta=1.0)


class Strain(Base):
    def test_zones_use_heart_rate_reserve(self):
        d = "2026-11-10"
        rest, top = 50.0, 200.0                       # zone 1 starts at 125 bpm, zone 5 at 185
        self.hr(at(d, 10), at(d, 10) + 600, 130)      # 10 min in zone 1
        self.hr(at(d, 11), at(d, 11) + 300, 190)      # 5 min in zone 5
        z = scores.heart_zones(self.c, at(d, 0), at(d, 24), rest, top)
        self.assertAlmostEqual(z["mins"][1], 10, delta=0.1)
        self.assertAlmostEqual(z["mins"][5], 5, delta=0.1)
        self.assertAlmostEqual(z["recorded_s"], 900, delta=2)

    def test_held_back_when_strap_was_off(self):
        d = "2026-11-10"
        self.hr(at(d, 8), at(d, 12), 80)              # 4 h of a 24 h day
        e = scores.strain(self.c, d, 50.0, M["strain"], now=at(d, 30))
        self.assertFalse(e["ok"])
        self.assertIn("recorded", e["why"])

    def test_full_day_is_trusted_and_harder_day_scores_higher(self):
        d, e2 = "2026-11-10", "2026-11-11"
        self.hr(at(d, 4), at(d, 28), 70)
        self.hr(at(e2, 4), at(e2, 28), lambda t: 160 if at(e2, 17) <= t < at(e2, 18) else 70)
        a = scores.strain(self.c, d, 50.0, M["strain"], now=at(d, 40))
        b = scores.strain(self.c, e2, 50.0, M["strain"], now=at(e2, 40))
        self.assertTrue(a["ok"] and b["ok"])
        self.assertGreater(b["value"], a["value"] + 2)
        self.assertLessEqual(b["value"], 21)


class SleepAndRecovery(Base):
    def test_sleep_needs_a_finished_night(self):
        d = "2026-11-10"
        self.night(d, status="ongoing")
        self.assertIsNone(scores.sleep(self.c, d, 10.0, None, M["sleep"]))

    def test_sleep_performance_and_need(self):
        d = "2026-11-10"
        self.night(d, -1.0, 7.0, asleep_h=7.5)
        self.hr(at(d, -1), at(d, 7), 50)
        e = scores.sleep(self.c, d, 14.0, None, M["sleep"])
        self.assertTrue(e["ok"])
        self.assertAlmostEqual(e["inputs"]["need_h"], M["sleep"]["baseline_h"] + scores.need_from_strain(14.0), places=2)
        self.assertTrue(0 <= e["value"] <= 100)
        self.assertEqual(e["row"]["total_rem_ms"], None)          # stages are not estimated

    def test_impossible_night_is_held_back(self):
        d = "2026-11-10"
        self.night(d, 5.0, 6.0, asleep_h=0.8)
        self.hr(at(d, 5), at(d, 6), 50)
        e = scores.sleep(self.c, d, 10.0, None, M["sleep"])
        self.assertFalse(e["ok"])

    def test_recovery_waits_for_own_baseline(self):
        d = "2026-11-10"
        self.hr(at(d, -1), at(d, 7), 50, "[1200, 1210]")
        e = scores.recovery(self.c, d, None, M["recovery"])
        self.assertFalse(e["ok"])
        self.assertIn("usual", e["why"])


class Versions(Base):
    def test_versions_side_by_side_and_only_trusted_rows_shown(self):
        good = {"value": 12.0, "ok": True, "why": None, "row": {"start": "x", "strain": 12.0}, "inputs": {}}
        held = {"value": 3.0, "ok": False, "why": "strap off", "row": {"start": "y", "strain": 3.0}, "inputs": {}}
        scores._put(self.c, "2026-11-10", "strain", "v1", good)
        scores._put(self.c, "2026-11-10", "strain", "v2", dict(good, value=13.0, row={"start": "x", "strain": 13.0}))
        scores._put(self.c, "2026-11-11", "strain", "v1", held)
        n = self.c.execute("SELECT COUNT(*) n FROM estimates").fetchone()["n"]
        self.assertEqual(n, 3)                                         # nothing overwritten across versions
        rows = scores.for_app(self.c, "v1")["cycle"]
        self.assertEqual([r["strain"] for r in rows], [12.0])          # v1 only, held-back row hidden
        self.assertEqual(rows[0]["est"], "v1")

    def test_update_runs_on_an_empty_strap(self):
        self.assertEqual(scores.update(self.c), {})


def archive(c, days=5, start="2025-01-01"):
    """A tiny WHOOP archive: one cycle, sleep, recovery and workout a day."""
    t0 = time.mktime(time.strptime(start, "%Y-%m-%d"))
    for i in range(days):
        d = time.strftime("%Y-%m-%d", time.gmtime(t0 + i * 86400 + 43200))
        c.execute("INSERT INTO api_cycle(id,start,end,strain) VALUES(?,?,?,?)", (i, d + "T04:00:00.000Z", d + "T23:00:00.000Z", 10.0))
        c.execute("INSERT INTO api_sleep(id,start,nap) VALUES(?,?,0)", (f"s{i}", d + "T23:10:00.000Z"))
        c.execute("INSERT INTO api_recovery(cycle_id,created_at,recovery_score) VALUES(?,?,?)", (i, d + "T07:00:00.000Z", 60))
        c.execute("INSERT INTO api_workout(id,start,strain) VALUES(?,?,?)", (f"w{i}", d + "T17:00:00.000Z", 8.0))
    c.commit()


class Selection(Base):
    def setUp(self):
        super().setUp()
        self._fp = scores.MODELS["v1"]["fingerprint"]

    def tearDown(self):
        scores.MODELS["v1"]["fingerprint"] = self._fp
        super().tearDown()

    def test_no_archive_means_generic(self):
        self.assertIsNone(scores.fingerprint(self.c))
        self.assertEqual(scores.current(self.c), "generic-v1")

    def test_someone_elses_archive_means_generic(self):
        archive(self.c)
        self.assertEqual(scores.current(self.c), "generic-v1")       # v1's fingerprint is the owner's

    def test_owners_archive_means_v1_even_after_new_rows(self):
        archive(self.c)
        scores.MODELS["v1"]["fingerprint"] = scores.fingerprint(self.c)
        self.assertEqual(scores.which(self.c)[0], "v1")
        self.c.execute("INSERT INTO api_cycle(id,start,end,strain) VALUES(99,'2025-02-01T04:00:00.000Z',NULL,5)")
        self.assertEqual(scores.current(self.c), "v1")               # later rows: still the same archive
        self.c.execute("DELETE FROM api_cycle WHERE id=0")            # ...but a changed one isn't
        self.assertEqual(scores.current(self.c), "generic-v1")

    def test_later_rows_keep_the_fingerprint(self):
        archive(self.c)
        fp = scores.fingerprint(self.c)
        self.c.execute("INSERT INTO api_cycle(id,start,end,strain) VALUES(99,'2025-03-01T04:00:00.000Z',NULL,5)")
        self.assertTrue(scores.fitted_here(self.c, fp))
        self.assertFalse(scores.fitted_here(self.c, {"until": fp["until"], "sha": "0" * 16}))

    def test_chosen_model_wins_and_unknown_choice_falls_back(self):
        db.set_state(self.c, "scores_model", "v1")
        self.assertEqual(scores.current(self.c), "v1")
        db.set_state(self.c, "scores_model", "fit-nope")
        mid, why = scores.which(self.c)
        self.assertEqual(mid, "generic-v1")
        self.assertIn("doesn't exist", why)

    def test_fitted_models_live_in_the_db_and_are_never_overwritten(self):
        params = json.loads(json.dumps({k: scores.MODELS["generic-v1"][k] for k in ("strain", "sleep", "recovery")}))
        params["recovery"]["with_sleep"] = [60.0, -1.0, 12.0, 5.0]
        scores.save_model(self.c, "fit-test", params, {"parts_fitted": ["recovery"]}, {"until": "x", "sha": "y"})
        with self.assertRaises(ValueError):
            scores.save_model(self.c, "fit-test", params, {}, None)
        with self.assertRaises(ValueError):
            scores.save_model(self.c, "v1", params, {}, None)          # built-ins can't be shadowed
        db.set_state(self.c, "scores_model", "fit-test")
        self.assertEqual(scores.current(self.c), "fit-test")
        self.assertEqual(scores.get_model(self.c, "fit-test")["recovery"]["with_sleep"][2], 12.0)

    def test_for_app_follows_the_install(self):
        good = {"value": 12.0, "ok": True, "why": None, "row": {"start": "x", "strain": 12.0}, "inputs": {}}
        scores._put(self.c, "2026-11-10", "strain", "v1", good)
        scores._put(self.c, "2026-11-10", "strain", "generic-v1", dict(good, row={"start": "x", "strain": 9.0}))
        self.assertEqual(scores.for_app(self.c)["cycle"][0]["strain"], 9.0)     # no archive: generic
        db.set_state(self.c, "scores_model", "v1")
        self.assertEqual(scores.for_app(self.c)["cycle"][0]["strain"], 12.0)


class Generic(Base):
    G = scores.MODELS["generic-v1"]

    def test_says_it_is_not_fitted(self):
        for part in ("strain", "sleep", "recovery"):
            self.assertFalse(self.G[part]["validation"]["fitted_to_you"])
            self.assertIn("not fitted to you", self.G[part]["validation"]["note"])

    def test_max_hr_from_age_then_from_what_the_strap_sees(self):
        top, src = scores.personal_max_hr(self.c, self.G["strain"])
        self.assertEqual(top, 180.0)                                   # no age: Tanaka at 40
        self.c.execute("INSERT INTO api_profile(key,value) VALUES('age','30')")
        top, src = scores.personal_max_hr(self.c, self.G["strain"])
        self.assertAlmostEqual(top, 208 - 0.7 * 30)
        self.assertIn("Tanaka", src)
        d = "2026-11-10"
        self.hr(at(d, 10), at(d, 10) + 5, 205)                        # 5 s spike: not sustained
        self.hr(at(d, 11), at(d, 11) + 30, lambda t: 196 + int(t) % 3) # 30 s at 196-198
        self.hr(at(d, 12), at(d, 12) + 20, 240)                        # not a heart
        top, src = scores.personal_max_hr(self.c, self.G["strain"])
        self.assertEqual(top, 196.0)
        self.assertIn("strap", src)

    def test_birthday_gives_an_age(self):
        self.c.execute("INSERT INTO api_profile(key,value) VALUES('birthday','1990-06-15')")
        self.assertEqual(scores.profile_age(self.c, "2026-06-14"), 35.0)
        self.assertEqual(scores.profile_age(self.c, "2026-06-15"), 36.0)

    def test_sleep_baseline_default_then_own_median_within_limits(self):
        base, src = scores.personal_sleep_baseline(self.c, "2026-11-20", self.G["sleep"])
        self.assertEqual(base, 8.0)
        for i in range(13):
            self.night(scores._shift("2026-11-19", -i), asleep_h=7.6)
        self.assertEqual(scores.personal_sleep_baseline(self.c, "2026-11-20", self.G["sleep"])[0], 8.0)  # 13 nights
        self.night(scores._shift("2026-11-19", -13), asleep_h=7.6)
        self.assertAlmostEqual(scores.personal_sleep_baseline(self.c, "2026-11-20", self.G["sleep"])[0], 7.6)
        for i in range(14):
            self.night(scores._shift("2026-11-19", -i), asleep_h=5.5)   # short sleeper: need stays >= 7 h
        base, src = scores.personal_sleep_baseline(self.c, "2026-11-20", self.G["sleep"])
        self.assertEqual(base, 7.0)
        self.assertIn("kept within", src)

    def test_whole_pipeline_gives_sane_values_without_an_archive(self):
        # four nights and days of strap data, no WHOOP archive, no profile
        days = ["2026-11-07", "2026-11-08", "2026-11-09", "2026-11-10"]
        for d in days:
            self.night(d, -1.0, 7.0, asleep_h=7.3)
            self.hr(at(d, -1), at(d, 7), 52, "[1100, 1150, 1120]")
            self.hr(at(d, 7), at(d, 23), lambda t, d=d: 150 if at(d, 17) <= t < at(d, 17.75) else 75)
            self.c.execute("INSERT OR REPLACE INTO daily_night(date,rhr,hrv) VALUES(?,?,?)", (d, 52.0, 55.0))
        self.assertEqual(scores.current(self.c), "generic-v1")
        out = scores.update(self.c, days=4, now=at("2026-11-11", 12))
        st, sl = out["2026-11-09"]["strain"], out["2026-11-10"]["sleep"]
        self.assertTrue(st["ok"])
        self.assertTrue(5 < st["value"] < 18, st["value"])
        self.assertEqual(st["inputs"]["max_hr"], 180.0)
        self.assertTrue(sl["ok"])
        self.assertEqual(sl["inputs"]["basis"], "hours vs need")
        self.assertTrue(70 <= sl["value"] <= 100, sl["value"])
        rc = out["2026-11-10"]["recovery"]
        self.assertTrue(rc["value"] is None or 1 <= rc["value"] <= 99)
        rows = self.c.execute("SELECT DISTINCT version FROM estimates").fetchall()
        self.assertEqual([r["version"] for r in rows], ["generic-v1"])

    def test_generic_recovery_is_led_by_hrv(self):
        a, b, w, s = self.G["recovery"]["with_sleep"]
        self.assertGreater(abs(w), abs(s) + abs(b))
        self.assertTrue(34 <= a <= 99)                                # a usual night isn't red


if __name__ == "__main__":
    unittest.main()
