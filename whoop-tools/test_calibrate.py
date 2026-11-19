#!/usr/bin/env python3
"""Tests for calibrate.py on a small synthetic WHOOP archive made from known numbers: the fit
gets them back, beats generic-v1, is stored under a new id and selected.
   .venv/bin/python -m unittest test_calibrate"""
import contextlib, io, json, math, os, random, tempfile, time, unittest

_tmp = tempfile.mkdtemp()
os.environ["WHOOP_DB"] = os.path.join(_tmp, "t.db")
import calibrate, db, scores  # noqa: E402

TRUE = {"weights": [0.4, 1.0, 3.0, 5.0, 8.0, 12.0], "slope": 0.7, "offset": 4.5, "zone0": [0.5, 0.02],
        "baseline": 7.2, "carry": 0.3, "recovery": [58.0, -3.0, 13.0, 6.0], "max_hr": 190.0, "rest": 55.0}
ZN = calibrate.ZN


def iso(t):
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(t))


def build(c, days=150, seed=1):
    """One WHOOP cycle a day from 2024-12-01 (04:00 to 04:00 UTC), a workout most days, a sleep at
    each cycle's end and a recovery the next morning, all scored with TRUE (plus a little noise)."""
    rnd = random.Random(seed)
    m = {"weights": TRUE["weights"], "slope": TRUE["slope"], "offset": TRUE["offset"]}
    t0 = calendar_t("2024-12-01T04:00:00")
    debt, prev_strain, recs = 0.0, None, []
    for i in range(days):
        a, b = t0 + i * 86400, t0 + (i + 1) * 86400
        mins = [0.0] * 6
        if rnd.random() < 0.8:
            mins = [rnd.uniform(0, 20), rnd.uniform(5, 40), rnd.uniform(0, 30), rnd.uniform(0, 20),
                    rnd.uniform(0, 8), rnd.choice([0, 0, rnd.uniform(0, 4)])]
            top = max(k for k in range(6) if mins[k] > 0)
            hr = TRUE["rest"] + (0.4 + 0.1 * top + rnd.uniform(0, 0.09)) * (TRUE["max_hr"] - TRUE["rest"])
            w_strain = scores.strain_map(sum(x * y for x, y in zip(TRUE["weights"], mins)), m) + rnd.gauss(0, 0.2)
            c.execute("INSERT INTO api_workout(id,start,end,strain,max_hr,zone_raw) VALUES(?,?,?,?,?,?)",
                      (f"w{i}", iso(a + 13 * 3600), iso(a + 14 * 3600), round(max(0.1, w_strain), 2), round(hr),
                       json.dumps({k: v * 6e4 for k, v in zip(ZN, mins)})))
        avg = rnd.uniform(60, 85)
        rest_min = (b - a) / 60 - sum(mins)
        share = min(1.0, max(0.0, TRUE["zone0"][0] + TRUE["zone0"][1] * (avg - 70)))
        load = sum(x * y for x, y in zip(TRUE["weights"][1:], mins[1:])) + (mins[0] + rest_min * share) * TRUE["weights"][0]
        strain = round(scores.strain_map(load, m) + rnd.gauss(0, 0.2), 2)
        c.execute("INSERT INTO api_cycle(id,start,end,strain,avg_hr) VALUES(?,?,?,?,?)", (i, iso(a), iso(b), strain, avg))
        # the night after this cycle
        asleep = rnd.uniform(5.5, 9.0)
        need = TRUE["baseline"] + scores.need_from_strain(strain) + debt
        perf = round(min(100.0, 100 * asleep / need) + rnd.gauss(0, 0.5))
        debt = TRUE["carry"] * max(0.0, need - asleep)
        in_bed = asleep + rnd.uniform(0.3, 0.8)
        c.execute("INSERT INTO api_sleep(id,start,end,nap,sleep_perf,total_in_bed_ms,total_awake_ms,sleep_efficiency,resp_rate) "
                  "VALUES(?,?,?,0,?,?,?,?,15.0)", (f"s{i}", iso(b + 300), iso(b + 300 + in_bed * 3600), perf,
                                                     int(in_bed * 3.6e6), int((in_bed - asleep) * 3.6e6), 100 * asleep / in_bed))
        recs.append((i, f"s{i}", iso(b + 300 + in_bed * 3600), rnd.gauss(TRUE["rest"], 3), math.exp(rnd.gauss(math.log(60), 0.2)), perf))
    # recovery from the true weights, on the same features calibrate builds
    for i, sid, t, rhr, hrv, sp in recs:
        c.execute("INSERT INTO api_recovery(cycle_id,sleep_id,created_at,recovery_score,resting_hr,hrv_rmssd_ms) "
                  "VALUES(?,?,?,?,?,?)", (i, sid, t, 50, round(rhr, 1), round(hrv, 1)))
    c.commit()
    feats = {}
    rows = [dict(r) for r in c.execute("SELECT cycle_id, created_at FROM api_recovery ORDER BY created_at")]
    for (y, zr, zh, s), r in zip(calibrate._recovery_rows(c), rows[10:]):
        feats[r["cycle_id"]] = (zr, zh, s)
    a0, b0, w0, s0 = TRUE["recovery"]
    for cid, (zr, zh, s) in feats.items():
        y = min(99.0, max(1.0, a0 + b0 * zr + w0 * zh + s0 * s + rnd.gauss(0, 1.5)))
        c.execute("UPDATE api_recovery SET recovery_score=? WHERE cycle_id=?", (round(y), cid))
    c.commit()


def calendar_t(s):
    return calibrate._t(s)


class Calibrate(unittest.TestCase):
    def setUp(self):
        if os.path.exists(db.DB_PATH):
            os.remove(db.DB_PATH)
        self.c = db.connect()

    def tearDown(self):
        self.c.close()

    def run_main(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            calibrate.main(list(args))
        return out.getvalue()

    def test_numerics(self):
        self.assertEqual([round(v, 6) for v in calibrate.lstsq([[1, 0], [1, 1], [1, 2]], [1, 3, 5])], [1.0, 2.0])
        p = calibrate.lm(lambda p: [p[0] * math.exp(p[1] * x) - 2 * math.exp(0.3 * x) for x in range(10)], [1, 0])
        self.assertAlmostEqual(p[0], 2, places=4)
        self.assertAlmostEqual(p[1], 0.3, places=4)

    def test_no_archive_says_so_and_stays_generic(self):
        out = self.run_main()
        self.assertIn("No WHOOP archive", out)
        self.assertEqual(scores.current(self.c), "generic-v1")

    def test_fit_recovers_the_numbers_and_beats_generic(self):
        build(self.c)
        params, val, fits = calibrate.calibrate(self.c)
        st, sl, rc = params["strain"], params["sleep"], params["recovery"]
        for got, want in zip(fits["strain"]["params"]["weights"], TRUE["weights"]):
            self.assertAlmostEqual(got, want, delta=max(0.15 * want, 0.15))   # zone 0 is barely used
        self.assertAlmostEqual(fits["strain"]["params"]["slope"], TRUE["slope"], delta=0.05)
        self.assertAlmostEqual(fits["strain"]["params"]["offset"], TRUE["offset"], delta=0.1)
        self.assertAlmostEqual(st["max_hr"], TRUE["max_hr"], delta=4)
        self.assertAlmostEqual(sl["baseline_h"], TRUE["baseline"], delta=0.1)
        self.assertAlmostEqual(sl["debt_carry"], TRUE["carry"], delta=0.05)
        for got, want in zip(rc["with_sleep"], TRUE["recovery"]):
            self.assertAlmostEqual(got, want, delta=1.5)
        self.assertIn("sleep", val["parts_better_than_generic"])
        self.assertIn("recovery", val["parts_better_than_generic"])
        self.assertTrue(rc["validation"]["fitted_to_you"])
        self.assertLess(fits["recovery"]["mae"], fits["recovery"]["generic_mae"])

    def test_main_stores_selects_and_never_overwrites(self):
        build(self.c)
        out = self.run_main()
        mid = "fit-" + time.strftime("%Y-%m-%d")
        self.assertIn("Held-out days", out)
        self.assertIn(f"Selected: this install now uses {mid}", out)
        self.assertEqual(scores.current(self.c), mid)
        m = scores.get_model(self.c, mid)
        self.assertEqual(m["fingerprint"], scores.fingerprint(self.c))
        self.run_main("--dry-run")
        self.assertEqual(self.c.execute("SELECT COUNT(*) n FROM score_models").fetchone()["n"], 1)
        out = self.run_main()                                   # again: a new id, the first one kept
        self.assertIn(f"{mid}-2", out)
        self.assertEqual(self.c.execute("SELECT COUNT(*) n FROM score_models").fetchone()["n"], 2)
        self.run_main("--use", "generic-v1")
        self.assertEqual(scores.current(self.c), "generic-v1")

    def test_not_selected_on_v1s_own_archive(self):
        build(self.c)
        fp = scores.MODELS["v1"]["fingerprint"]
        scores.MODELS["v1"]["fingerprint"] = scores.fingerprint(self.c)
        try:
            out = self.run_main()
            self.assertIn("keeps v1", out)
            self.assertEqual(scores.current(self.c), "v1")
        finally:
            scores.MODELS["v1"]["fingerprint"] = fp

    def test_age_is_saved_for_max_heart_rate(self):
        self.run_main("--age", "40")
        self.assertEqual(scores.profile_age(self.c), 40.0)
        self.assertAlmostEqual(scores.personal_max_hr(self.c, scores.MODELS["generic-v1"]["strain"])[0], 180.0)


if __name__ == "__main__":
    unittest.main()
