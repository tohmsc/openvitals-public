#!/usr/bin/env python3
"""Energy burned per day (kcal, WHOOP's "Calories") from the strap's heart rate and your body data.

HOW
  Resting  Mifflin-St Jeor basal rate from sex, weight, height and age, spread over the day.
  Active   For each second with heart rate at or above a threshold share of your heart rate
           reserve (resting to max), the Keytel et al. 2005 equation (sex, weight, age, heart
           rate) gives the energy of exercise; the part above the resting rate counts, times
           a scale. Seconds without a heart rate count as resting.
  The threshold, the scale and a resting factor are in scores.ENERGY_V1 with how well they
  matched WHOOP's own numbers on your archive (fit: whoop-tools/fit/energy/fit.py).

YOUR BODY DATA
  Kept only in your local database (table api_profile; cloud_sync.py does not send it):
      python energy.py set sex male          # male or female (the equations need one)
      python energy.py set weight_kg 70
      python energy.py set height_cm 175
      python energy.py set age 30            # stored as birth_year
      python energy.py show
  Without all four, no energy number is made ("held back"), rather than a guess.

Run:  python energy.py 2026-11-19            # one day's energy, from the strap's heart rate
"""
import sys, time
import db

KJ_PER_KCAL = 4.184

# body data keys -> what to enter
FIELDS = {
    "sex": "male or female",
    "weight_kg": "body weight in kg, e.g. 70",
    "height_cm": "height in cm, e.g. 175",
    "birth_year": "year of birth, e.g. 1995 (or use: set age 30)",
}
RANGES = {"weight_kg": (25.0, 300.0), "height_cm": (100.0, 250.0), "birth_year": (1900, 2100)}


# ---------------- body data ----------------
def _clean(key, value):
    """The stored form of a profile value, or ValueError with a plain message."""
    v = str(value).strip().lower()
    if key == "sex":
        v = {"m": "male", "f": "female"}.get(v, v)
        if v not in ("male", "female"):
            raise ValueError("sex must be male or female")
        return v
    if key not in RANGES:
        raise ValueError(f"unknown key {key!r}; use one of: {', '.join(FIELDS)} (or age)")
    x = float(v)
    lo, hi = RANGES[key]
    if not lo <= x <= hi:
        raise ValueError(f"{key} must be between {lo:g} and {hi:g}")
    return str(int(x)) if key == "birth_year" else f"{x:g}"


def set_profile(c, key, value, now=None):
    """Store one body value. `age` is stored as birth_year (this year minus age)."""
    if key == "age":
        a = float(value)
        if not 10 <= a <= 110:
            raise ValueError("age must be between 10 and 110")
        key, value = "birth_year", time.localtime(now or time.time()).tm_year - int(a)
    v = _clean(key, value)
    c.execute("INSERT INTO api_profile(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
              (key, v))
    c.commit()
    return key, v


def profile(c):
    """(body, missing): body has sex, weight_kg, height_cm, birth_year when all are set and valid."""
    rows = {r[0]: r[1] for r in c.execute(
        f"SELECT key, value FROM api_profile WHERE key IN ({','.join('?' * len(FIELDS))})", tuple(FIELDS))}
    body, missing = {}, []
    for k in FIELDS:
        try:
            v = _clean(k, rows[k])
            body[k] = v if k == "sex" else float(v)
        except (KeyError, ValueError, TypeError):
            missing.append(k)
    return (None if missing else body), missing


def age_on(body, date):
    """Age in years on `date` (YYYY-MM-DD), from the birth year (within a year)."""
    return int(date[:4]) - body["birth_year"]


# ---------------- formulas ----------------
def bmr_kcal_day(sex, weight_kg, height_cm, age):
    """Mifflin-St Jeor (1990) basal metabolic rate, kcal per day."""
    return 10 * weight_kg + 6.25 * height_cm - 5 * age + (5 if sex == "male" else -161)


def keytel_kcal_min(sex, hr, weight_kg, age):
    """Keytel et al. 2005 energy expenditure while exercising (no VO2max), kcal per minute."""
    if sex == "male":
        kj = -55.0969 + 0.6309 * hr + 0.1988 * weight_kg + 0.2017 * age
    else:
        kj = -20.4022 + 0.4472 * hr - 0.1263 * weight_kg + 0.074 * age
    return kj / KJ_PER_KCAL


def rate_kcal_min(hr, rest, top, body, age, m):
    """Energy rate (kcal/min) at heart rate `hr` (None: not recorded, counts as resting)."""
    base = bmr_kcal_day(body["sex"], body["weight_kg"], body["height_cm"], age) / 1440
    rest_rate = m["rest_factor"] * base
    if hr is None or (hr - rest) / (top - rest) < m["threshold_hrr"]:
        return rest_rate, 0.0
    extra = max(0.0, keytel_kcal_min(body["sex"], hr, body["weight_kg"], age) - base)
    return rest_rate, m["active_scale"] * extra


def energy(c, a, b, rest, top, body, age, m):
    """Energy for [a, b) from ble_hr: total, resting and active kcal, and recorded seconds.
    Seconds further than a minute apart are a gap (counted at the resting rate)."""
    rest_rate, _ = rate_kcal_min(None, rest, top, body, age, m)
    active, rec, prev = 0.0, 0.0, None
    cache = {}
    for ts, hr in c.execute("SELECT ts, hr FROM ble_hr WHERE ts >= ? AND ts < ? AND hr > 0 ORDER BY ts", (a, b)):
        dt = min(ts - prev, 1.0) if prev is not None and ts - prev < 60 else 1.0
        prev = ts
        rec += dt
        if hr not in cache:
            cache[hr] = rate_kcal_min(hr, rest, top, body, age, m)[1]
        active += cache[hr] * dt / 60
    resting = rest_rate * (b - a) / 60
    return {"kcal": resting + active, "resting_kcal": resting, "active_kcal": active, "recorded_s": rec}


def for_day(c, date, a, b, rest, top, ongoing, m, gate):
    """Energy for the day window [a, b): (kilojoule or None, inputs with the reason when held back)."""
    body, missing = profile(c)
    if not body:
        return None, {"why": "needs your body data: " + ", ".join(missing) + " (python energy.py set ...)"}
    age = age_on(body, date)
    e = energy(c, a, b, rest, top, body, age, m)
    cover = e["recorded_s"] / (b - a) if b > a else 0.0
    inputs = {"kcal": round(e["kcal"]), "active_kcal": round(e["active_kcal"]), "coverage": round(cover, 3)}
    if ongoing:
        return None, dict(inputs, why="the day isn't over yet")
    if cover < gate:
        return None, dict(inputs, why=f"heart rate recorded for {cover:.0%} of the day (needs {gate:.0%})")
    return round(e["kcal"] * KJ_PER_KCAL, 1), inputs


# ---------------- command line ----------------
def _main(args):
    c = db.connect()
    if args[:1] == ["set"] and len(args) == 3:
        try:
            k, v = set_profile(c, args[1], args[2])
        except ValueError as ex:
            print("Not saved:", ex)
            return 1
        print(f"Saved {k} = {v} (only in your local database).")
        return 0
    if args[:1] in (["show"], ["set"]):
        body, missing = profile(c)
        rows = {r[0]: r[1] for r in c.execute(
            f"SELECT key, value FROM api_profile WHERE key IN ({','.join('?' * len(FIELDS))})", tuple(FIELDS))}
        for k, h in FIELDS.items():
            print(f"  {k:11s} {rows.get(k, '(not set)'):10s} {h}")
        if missing:
            print("Energy is held back until these are set:", ", ".join(missing))
            print("  python energy.py set <key> <value>")
        return 0
    import scores
    date = args[0] if args else time.strftime("%Y-%m-%d")
    body, missing = profile(c)
    if not body:
        print("Held back: needs your body data:", ", ".join(missing))
        print("  python energy.py set <key> <value>   (python energy.py show for the list)")
        return 1
    a, b, ongoing = scores.day_window(c, date)
    rest = 50.0
    mid = scores.current(c)
    est = scores._get(c, date, "strain", mid) if scores._has(c, "estimates") else None
    if est:
        rest, top = est["inputs"]["rest"], est["inputs"]["max_hr"]
    else:
        top = (scores.get_model(c, mid) or scores.MODELS["v1"])["strain"].get("max_hr") or 180.0
    kj, inp = for_day(c, date, a, b, rest, top, ongoing, scores.ENERGY_V1, scores.GATES["strain_coverage"])
    so_far = " so far" if ongoing else ""
    print(f"{date}: {inp['kcal']} kcal{so_far} ({inp['active_kcal']} active), heart rate for {inp['coverage']:.0%} of the day")
    if kj is None:
        print("Held back:", inp["why"])
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
