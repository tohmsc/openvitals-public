#!/usr/bin/env python3
"""Tests for energy.py and its hook in scores.py, on synthetic heart rate.
   .venv/bin/python -m unittest test_energy"""
import os, tempfile, time, unittest

_tmp = tempfile.mkdtemp()
os.environ["WHOOP_DB"] = os.path.join(_tmp, "t.db")
import db, energy, scores  # noqa: E402

E = scores.ENERGY_V1
# a made-up person, not anyone's real data
BODY = {"sex": "female", "weight_kg": 60.0, "height_cm": 165.0, "birth_year": 1990.0}


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

    def hr(self, a, b, bpm):
        self.c.executemany("INSERT OR REPLACE INTO ble_hr(ts,hr,rr_ms) VALUES(?,?,?)",
                           [(float(t), bpm(t) if callable(bpm) else bpm, None) for t in range(int(a), int(b))])

    def body(self, **kw):
        for k, v in dict(BODY, **kw).items():
            energy.set_profile(self.c, k, v)


class Formulas(unittest.TestCase):
    def test_mifflin_st_jeor(self):
        self.assertAlmostEqual(energy.bmr_kcal_day("male", 80, 180, 40), 800 + 1125 - 200 + 5)
        self.assertAlmostEqual(energy.bmr_kcal_day("female", 60, 165, 36), 600 + 1031.25 - 180 - 161)

    def test_keytel(self):
        # Keytel et al. 2005, kJ/min, without VO2max
        self.assertAlmostEqual(energy.keytel_kcal_min("male", 150, 80, 40) * 4.184,
                               -55.0969 + 0.6309 * 150 + 0.1988 * 80 + 0.2017 * 40, places=6)
        self.assertAlmostEqual(energy.keytel_kcal_min("female", 150, 60, 36) * 4.184,
                               -20.4022 + 0.4472 * 150 - 0.1263 * 60 + 0.074 * 36, places=6)

    def test_below_threshold_is_resting(self):
        rest, top = 50.0, 190.0
        low = rest + (E["threshold_hrr"] - 0.01) * (top - rest)
        self.assertEqual(energy.rate_kcal_min(low, rest, top, BODY, 36, E)[1], 0.0)
        self.assertGreater(energy.rate_kcal_min(150, rest, top, BODY, 36, E)[1], 0.0)


class Profile(Base):
    def test_missing_body_data_holds_back(self):
        body, missing = energy.profile(self.c)
        self.assertIsNone(body)
        self.assertEqual(missing, ["sex", "weight_kg", "height_cm", "birth_year"])
        kj, inp = energy.for_day(self.c, "2026-11-10", at("2026-11-10", 4), at("2026-11-11", 4), 50, 190, False, E, 0.85)
        self.assertIsNone(kj)
        self.assertIn("body data", inp["why"])

    def test_set_age_and_bad_values(self):
        k, v = energy.set_profile(self.c, "age", 30, now=at("2026-06-01", 12))
        self.assertEqual((k, v), ("birth_year", "1996"))
        self.assertEqual(energy.set_profile(self.c, "sex", "F"), ("sex", "female"))
        for key, val in (("weight_kg", 5), ("height_cm", "tall"), ("sex", "x"), ("shoe", 42), ("age", 300)):
            with self.assertRaises(ValueError):
                energy.set_profile(self.c, key, val)
        self.c.execute("INSERT OR REPLACE INTO api_profile(key,value) VALUES('weight_kg','abc')")
        self.assertIn("weight_kg", energy.profile(self.c)[1])

    def test_body_data_is_not_sent_to_the_cloud(self):
        import cloud_sync
        self.assertFalse(set(energy.FIELDS) & set(cloud_sync.PROFILE_KEYS))


class Day(Base):
    D = "2026-11-10"

    def test_resting_day_is_basal_rate(self):
        self.body()
        a, b = at(self.D, 4), at(self.D, 28)
        self.hr(a, b, 58)
        e = energy.energy(self.c, a, b, 50.0, 190.0, BODY, 36, E)
        bmr = energy.bmr_kcal_day("female", 60, 165, 36)
        self.assertAlmostEqual(e["kcal"], E["rest_factor"] * bmr, delta=1)
        self.assertEqual(e["active_kcal"], 0.0)

    def test_an_hour_of_exercise_adds_scaled_keytel(self):
        a, b = at(self.D, 4), at(self.D, 28)
        self.hr(a, b, lambda t: 150 if at(self.D, 17) <= t < at(self.D, 18) else 58)
        e = energy.energy(self.c, a, b, 50.0, 190.0, BODY, 36, E)
        base = energy.bmr_kcal_day("female", 60, 165, 36) / 1440
        want = E["active_scale"] * (energy.keytel_kcal_min("female", 150, 60, 36) - base) * 60
        self.assertAlmostEqual(e["active_kcal"], want, delta=0.5)

    def test_coverage_gate_and_ongoing_day(self):
        self.body()
        a, b = at(self.D, 4), at(self.D, 28)
        self.hr(at(self.D, 8), at(self.D, 12), 80)             # 4 h of 24
        kj, inp = energy.for_day(self.c, self.D, a, b, 50.0, 190.0, False, E, 0.85)
        self.assertIsNone(kj)
        self.assertIn("recorded", inp["why"])
        self.hr(a, b, 70)
        kj, inp = energy.for_day(self.c, self.D, a, b, 50.0, 190.0, True, E, 0.85)
        self.assertIsNone(kj)
        kj, inp = energy.for_day(self.c, self.D, a, b, 50.0, 190.0, False, E, 0.85)
        self.assertAlmostEqual(kj, inp["kcal"] * 4.184, delta=4.2)


class Hook(Base):
    D = "2026-11-10"

    def day(self):
        self.hr(at(self.D, 4), at(self.D, 28), lambda t: 155 if at(self.D, 17) <= t < at(self.D, 18) else 65)
        return scores.update(self.c, days=3, now=at(self.D, 40))[self.D]["strain"]

    def test_strain_row_gets_kilojoule_and_strain_is_unchanged(self):
        without = self.day()
        self.assertIsNone(without["row"]["kilojoule"])
        self.assertIn("body data", without["inputs"]["energy"]["why"])
        self.body()
        with_body = self.day()
        self.assertEqual(with_body["value"], without["value"])
        self.assertEqual(with_body["ok"], without["ok"])
        kcal = with_body["row"]["kilojoule"] / 4.184
        self.assertTrue(1200 < kcal < 3000, kcal)
        rows = scores.for_app(self.c)["cycle"]
        self.assertEqual(rows[0]["kilojoule"], with_body["row"]["kilojoule"])

    def test_energy_v1_is_separate_from_strain_model(self):
        self.assertNotIn("energy", scores.MODELS["v1"])
        self.assertEqual(scores.MODELS["v1"]["strain"]["max_hr"], 197.0)


if __name__ == "__main__":
    unittest.main()
