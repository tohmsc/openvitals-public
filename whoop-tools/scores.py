#!/usr/bin/env python3
"""Strain, Sleep and Recovery estimated from the strap, the way WHOOP scores them, calibrated
on the days WHOOP itself scored for you.

WHOOP stopped scoring when the strap stopped going through its app (archive ends June 2025).
This module computes the same three scores from what the strap records now (ble_hr: heart
rate and beat intervals every second; sleep_night: when you slept, from sleep.py), using
WHOOP's published method where there is one and a fit to your own WHOOP history where not.

VERSIONS
Every model is a frozen set of numbers under a version name (MODELS["v1"], ...). Estimates
are stored per version in the `estimates` table and never overwritten by another version, so
a v2 can be added next to v1 and compared before an install uses it (current()). `python scores.py check`
re-runs each version's validation against the WHOOP archive, so the accuracy below is
reproducible, not just claimed.

ONLY WHEN IT CAN BE TRUSTED
Each estimate has ok=1 only when its inputs pass the checks in GATES (enough of the day
recorded, a clean night, a baseline of your own nights). Rows with ok=0 keep the reason in
`why`; the app shows only ok=1 rows of the version this install uses (current()).

v1, how each score is made (sources: WHOOP's docs, WHOOP patent US 12,318,226 B2, your archive)

  Strain   Each second's heart rate as a share of your heart rate reserve (resting to max),
           sorted into WHOOP's zones (0: under 50%, 1: 50-60% ... 5: 90-100%; WHOOP switched
           your zones to heart rate reserve in Dec 2024, which the archive shows). Minutes in
           each zone are weighted and summed, then mapped to 0-21 with the patent's arctan
           curve. Weights and curve fitted on 127 of your WHOOP workouts (Dec 2024 on, zone
           times from WHOOP). Held-out error: 0.51 strain on workouts, 1.26 on whole days.
           Max heart rate: 197, what WHOOP was using for you by 2025 (raised when the strap
           sees a higher sustained rate, as WHOOP does).
  Sleep    Need = your baseline + strain term + part of last night's shortfall - naps.
           The strain term is the patent's 1.7 / (1 + e^((17 - strain) / 3.5)) hours (it
           reproduces WHOOP's API example to the millisecond). Baseline 7.88 h and 43% debt
           carry-over fitted on your nights before May 2025: held-out error 2.0 points on
           sleep performance when it was hours-vs-need. Since May 2025 WHOOP blends in
           efficiency and consistency; that blend is fitted on your May-June 2025 nights
           (held-out error 4.9 points). Consistency is the share of the day's minutes in the
           same state (asleep/awake) on each of the last 4 days, corrected to WHOOP's scale
           (8 points typical error on the archive). Stages (deep, REM) are not estimated.
  Recovery Your night's resting heart rate and HRV against your own last 30 strap nights (in
           units of your normal night-to-night variation), plus sleep performance. Fitted on
           321 WHOOP recoveries: held-out error 7.1 points, right zone 86% of the time (8.3
           and 76% without sleep, which is used when the sleep estimate is missing).

generic-v1, for anyone without a WHOOP history (nothing in it is fitted to you)

  Strain   v1's zone weights and curve (they describe how hard each zone is, not one person),
           with your own resting heart rate (your nights) and max heart rate (208 - 0.7 x age
           if your age is known, else 180, raised to the highest rate the strap has seen you
           hold for 10 seconds).
  Sleep    Need = your own median sleep once there are 14 nights (8 h before that; kept
           within 7-9 h) + the patent's strain term + part of last night's shortfall.
           Performance = hours slept against need (WHOOP's published definition).
  Recovery Same inputs as v1, default weights with HRV carrying most of the score.

WHICH MODEL THIS INSTALL USES (current())
  1. the one chosen in sync_state "scores_model" (calibrate.py sets it), else
  2. v1, if this database holds the WHOOP archive v1 was fitted on (MODELS["v1"]["fingerprint"]),
  3. else generic-v1.
Models calibrate.py fits on someone's archive live in the score_models table of their own
database (never overwritten); MODELS holds the built-in ones.

Breathing rate (resp_rate in the sleep row) has its own frozen block, RESP_RATE, versioned
apart from MODELS so adding it did not change any v1 number. See RESP_RATE for its method and
validation; it is filled only when enough clean windows exist, else left empty.

  Energy   kcal per day in the strain row (as kilojoule), from energy.py: resting rate from
           your body data plus Keytel's heart-rate equation above 35% of reserve. Frozen in
           ENERGY_V1 (separate from MODELS); held back without body data or under the
           strain coverage gate. Held-out error on WHOOP's numbers: 16.5% on days (no better
           than a typical-day guess on the rebuilt archive days), 18.5% on workouts.

This is pattern-matching on heart rate and movement. It is not WHOOP's code, not a diagnosis
and not medical advice.

Run:  python scores.py              # update recent days, print them
      python scores.py check        # re-validate the current version on the WHOOP archive
      python scores.py which        # which model this install uses, and why
      python scores.py 2026-11-19   # one day
"""
import calendar, hashlib, json, math, sys, time
import db, energy, learn, metrics

OWNER = "v1"            # fitted on the owner's WHOOP archive; the default only on that archive
GENERIC = "generic-v1"  # for everyone else, until calibrate.py fits a model to them

# Fingerprint of the WHOOP archive v1 was fitted on: a hash of each api_* table's row count and
# first/last day before `until` (see fingerprint()). No readings, only enough to recognise it.
V1_FINGERPRINT = {"until": "2025-06-22", "sha": "65579381f5a65a57"}

MODELS = {
    "v1": {
        "fitted": "2026-11-19",
        "strain": {
            "max_hr": 197.0,
            # per-minute load in zones 0..5 (zone 1 = 1)
            "weights": [0.27996, 1.0, 3.82264, 5.87113, 10.08847, 15.32562],
            # strain = 21 * (atan(slope * (ln load - offset)) / pi + 0.5)
            "slope": 0.61956, "offset": 4.89692,
            # minutes under zone 1: the share that counts as zone 0, by the day's average HR
            # (fitted on 199 WHOOP days; under zone 0 is close to resting and adds nothing)
            "zone0_share": [0.6074, 0.0327],
            "validation": {"workouts": 127, "workout_mae": 0.51, "days": 199, "day_mae": 1.26},
        },
        "sleep": {
            "baseline_h": 7.882, "debt_carry": 0.433,
            # since May 2025: perf = a*hours_pct + b*efficiency + c*consistency + d
            "blend": [0.678, 0.676, 0.35, -62.425],
            # ...and without a consistency yet (fewer than 4 nights): a*hours_pct + b*eff + d
            "blend_no_consistency": [0.592, 1.177, -76.878],
            "consistency_scale": [1.004, -8.859],
            "validation": {"hours_mae": 2.0, "blend_mae": 4.9, "no_consistency_mae": 5.7,
                           "consistency_mae": 8.2},
        },
        "recovery": {
            # recovery = a + b*z_rhr + c*z_hrv + d*(sleep_perf - 80) / 10
            "with_sleep": [66.855, 0.341, 10.184, 4.703],
            "without_sleep": [65.304, -0.531, 10.311],
            "validation": {"days": 321, "mae": 7.1, "zone_match": 0.86,
                           "mae_without_sleep": 8.3, "zone_match_without_sleep": 0.76},
        },
        "fingerprint": V1_FINGERPRINT,
    },
}

# ---- generic-v1: published sources and defaults, for people with no WHOOP history ----
_NOT_FITTED = "not fitted to you: published figures and defaults (run calibrate.py if you have a WHOOP archive)"
_V1 = MODELS[OWNER]
MODELS[GENERIC] = {
    "fitted": None,
    "strain": {
        # None = yours, see personal_max_hr(): Tanaka, Monahan & Seals 2001 (J Am Coll Cardiol
        # 37:153), max HR = 208 - 0.7 x age, the same for men and women; raised to the highest
        # heart rate the strap has seen you hold for 10 s, as WHOOP does.
        "max_hr": None,
        # Tanaka's formula at 40 years, used while the profile has no age (api_profile "age" or
        # "birthday"; `calibrate.py --age N` sets it). A guess: an age makes it better.
        "max_hr_no_age": 180.0,
        # Resting HR comes from your own nights (learn.usual). Before 3 nights: 60 bpm, the
        # bottom of the usual adult range (AHA: 60-100 bpm).
        "rest_default": 60.0,
        # Zone weights, curve and zone-0 share are v1's. They say how much harder a minute in
        # each zone is, and how WHOOP's arctan curve (patent US 12,318,226 B2) maps load to
        # 0-21. Zones are a share of each person's own heart rate reserve, so they carry over,
        # but they were fitted on one person's 127 workouts: a default, not a fit to you.
        "weights": list(_V1["strain"]["weights"]),
        "slope": _V1["strain"]["slope"], "offset": _V1["strain"]["offset"],
        "zone0_share": list(_V1["strain"]["zone0_share"]),
        "validation": {"fitted_to_you": False, "note": _NOT_FITTED},
    },
    "sleep": {
        # None = yours, see personal_sleep_baseline(): your median sleep over your last 30 strap
        # nights once there are 14 (WHOOP starts need from your own baseline); 8.0 h before
        # that, the middle of the 7-9 h adults need (Watson et al. 2015, AASM/SRS consensus,
        # Sleep 38:843). Kept within 7-9 h so a run of short nights can't lower the need.
        "baseline_h": None, "baseline_default_h": 8.0, "baseline_min_nights": 14,
        "baseline_range_h": [7.0, 9.0],
        # Share of last night's shortfall added to tonight's need. WHOOP says need includes
        # recent sleep debt but publishes no figure; 0.433 is v1's fit, used as a default.
        "debt_carry": _V1["sleep"]["debt_carry"],
        # Performance = hours slept / need: WHOOP's published definition until May 2025. Its
        # newer blend with efficiency and consistency has no published weights: not guessed.
        "blend": None, "blend_no_consistency": None,
        # Consistency as WHOOP describes it (share of minutes in the same state, asleep or
        # awake, on each of the last 4 days), unscaled.
        "consistency_scale": [1.0, 0.0],
        "validation": {"fitted_to_you": False, "note": _NOT_FITTED},
    },
    "recovery": {
        # recovery = a + b*z_rhr + c*z_hrv + d*(sleep_perf - 80) / 10, z in units of your own
        # night-to-night spread. A default, not a fit: HRV carries most of it (WHOOP calls HRV
        # the biggest input; v1's fit gives ~10 points per spread of HRV and ~0 for resting HR
        # once HRV is in). Weights HRV 10, sleep 4, resting HR -2 (about 60/25/15 of the moving
        # part). Resting HR keeps a small negative weight because a raised resting HR is a known
        # sign of poor recovery (Buchheit 2014, Front Physiol 5:73). a = 66: a usual night lands
        # at the top of yellow, where v1's fit puts it (65-67).
        "with_sleep": [66.0, -2.0, 10.0, 4.0],
        "without_sleep": [66.0, -3.0, 10.0],
        "validation": {"fitted_to_you": False, "note": _NOT_FITTED},
    },
}

# Breathing rate during sleep, from the beat intervals (respiratory sinus arrhythmia: heart rate
# rises on each breath in and falls on each breath out). metrics.resp_rate, r1: seconds within
# 20 bpm of the night's resting HR; 2-minute windows every minute, intervals resampled at 2 Hz
# and differenced; the strongest frequency in 0.14-0.4 Hz (8.4-24/min), kept when it holds
# >= 25% of the band's power and beats cover >= 80% of the window; the night is the median of
# the kept windows, given only with >= 10 of them.
# Validated on PhysioNet ECG beats degraded to look like the strap (10 ms jitter, 3% missed
# beats, 1/1024 s quantising, per-second rows, parse_rr), against a respiration belt/flow
# reference: Fantasia (40 healthy adults, 2 h at rest, awake) and MIT-BIH SLPDB (18 nights,
# sleep apnoea patients). Parameters chosen on half the subjects; held-out 29 recordings:
# per window error 0.53 breaths/min (accepted windows); per recording 1.13 mean, 0.49 median,
# 68% within 1 and 88% within 2; a value for 25 of 29. Sleep nights alone (8 with a value):
# 1.21 mean, 0.19 median. The two big misses: breathing below the band (7.7/min) and an
# apnoea night with little clean reference. fit/resp_rate/ re-runs it.
RESP_RATE_CURRENT = "r1"
RESP_RATE = {
    "r1": {
        "fitted": "2026-11-19",
        "params": {"window_s": 120, "step_s": 60, "fs": 2.0, "diff": True, "band": (0.14, 0.4),
                   "df": 0.004, "min_cover": 0.8, "min_peak": 0.25, "calm_margin": 20, "min_windows": 10},
        "validation": {"recordings": 29, "with_value": 25, "window_mae": 0.53, "night_mae": 1.13,
                       "night_median_err": 0.49, "within1": 0.68, "within2": 0.88,
                       "sleep_night_mae": 1.21, "sleep_night_median_err": 0.19},
    },
}

# Energy per day (kcal; stored as kilojoule in the strain row, WHOOP's unit). Frozen apart from
# MODELS so the strain numbers above never move with it. energy.py has the method; the fit and
# these errors come from whoop-tools/fit/energy/fit.py (first 70% of the archive by date, errors
# on the last 30%). The archive has no per-second heart rate, so the check rebuilds each day's
# and workout's heart rate from WHOOP's averages and zone minutes: the errors below are for that
# rebuild, not yet for real strap days.
ENERGY_V1 = {
    "fitted": "2026-11-19",
    "threshold_hrr": 0.35,     # active from 35% of heart rate reserve
    "active_scale": 0.612,     # share of Keytel's energy above the resting rate that counts
    "rest_factor": 1.0,        # Mifflin-St Jeor basal rate as is
    # Threshold: the best fit on the first 70% whose estimate for the one archive day the strap
    # also recorded second by second (44% of it) stays under WHOOP's number for that day; lower
    # thresholds fit the rebuilt days a little better but read too much into real heart rate.
    # Scale refitted on all days and workouts. Held-out (last 30%, from 2025-03-08):
    "validation": {
        "days": 102, "day_mae_kcal": 365, "day_mae_pct": 16.5, "day_bias_kcal": -132,
        "workouts": 67, "workout_mae_kcal": 59, "workout_mae_pct": 18.5,
        # the same check for the plain formulas (Keytel above 30% of reserve, no scale) and
        # for no heart rate at all (training kcal per minute x length)
        "plain_day_mae_pct": 29.9, "plain_workout_mae_pct": 67.9,
        "no_hr_day_mae_pct": 16.1, "no_hr_workout_mae_pct": 58.7,
        "real_day": {"recorded": 0.44, "model_at_least_vs_whoop": -0.04},
    },
}

GATES = {
    "strain_coverage": 0.85,   # share of the day with heart rate recorded
    "night_coverage": 0.85,    # share of the sleep with heart rate recorded
    "min_rr": learn.MIN_RR,    # clean beat pairs for the night's HRV to count
    "min_nights": learn.MIN_NIGHTS,
    "sleep_h": (2.0, 14.0),    # a "night" outside this is a detection problem, not a night
}

DAY_START_H = 4              # without a detected sleep, a day runs 04:00 to 04:00

SCHEMA = """CREATE TABLE IF NOT EXISTS estimates (
  date TEXT, kind TEXT, version TEXT, value REAL, ok INTEGER, why TEXT,
  row TEXT, inputs TEXT, computed_at REAL, PRIMARY KEY (date, kind, version))"""

# Models calibrate.py fitted on this database's WHOOP archive. A row is never replaced.
MODELS_SCHEMA = """CREATE TABLE IF NOT EXISTS score_models (
  id TEXT PRIMARY KEY, params TEXT, validation TEXT, fitted_at REAL, fingerprint TEXT)"""

# The WHOOP archive tables (whoop_api.py or import_data.py) and the column that dates a row.
ARCHIVE = (("api_cycle", "start"), ("api_sleep", "start"), ("api_recovery", "created_at"),
           ("api_workout", "start"))


# ---------------- helpers ----------------
def _midnight(date):
    return time.mktime(time.strptime(date, "%Y-%m-%d"))


def _shift(date, days):
    t = _midnight(date) + days * 86400 + 43200        # noon: safe across DST changes
    return time.strftime("%Y-%m-%d", time.localtime(t))


def _iso(ts):
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(ts)) if ts else None


def _has(c, table):
    return c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None


def _sleep_row(c, date):
    """sleep.py's night that ended on the morning of `date`, if finished."""
    if not _has(c, "sleep_night"):
        return None
    r = c.execute("SELECT * FROM sleep_night WHERE date=?", (date,)).fetchone()
    return dict(r) if r and r["status"] == "done" and r["onset"] and r["wake"] else None


def strain_map(load, m):
    if load <= 0:
        return 0.0
    return 21 * (math.atan(m["slope"] * (math.log(load) - m["offset"])) / math.pi + 0.5)


def need_from_strain(strain):
    """WHOOP patent: extra sleep need (hours) from the day's strain."""
    return 1.7 / (1 + math.exp((17 - strain) / 3.5))


# ---------------- which model this install uses ----------------
def fingerprint(c, until=None):
    """A short hash of this database's WHOOP archive: per api_* table, the row count and first
    and last day of rows dated before `until` (default: the archive's last day, so rows WHOOP
    may still add on that day don't change it, nor does anything added later). None if empty."""
    if until is None:
        ends = [c.execute(f"SELECT MAX(substr({col},1,10)) d FROM {t}").fetchone()["d"]
                for t, col in ARCHIVE if _has(c, t)]
        ends = [d for d in ends if d]
        if not ends:
            return None
        until = max(ends)
    parts = []
    for t, col in ARCHIVE:
        if not _has(c, t):
            parts.append([t, 0, None, None])
            continue
        r = c.execute(f"SELECT COUNT(*) n, MIN(substr({col},1,10)) a, MAX(substr({col},1,10)) b FROM {t} "
                      f"WHERE substr({col},1,10) < ?", (until,)).fetchone()
        parts.append([t, r["n"], r["a"], r["b"]])
    if not sum(p[1] for p in parts):
        return None
    return {"until": until, "sha": hashlib.sha256(json.dumps(parts).encode()).hexdigest()[:16]}


def fitted_here(c, fp):
    """True when `fp` (a model's fingerprint) is this database's archive."""
    if not fp or not fp.get("until"):
        return False
    mine = fingerprint(c, fp["until"])
    return bool(mine) and mine["sha"] == fp["sha"]


def get_model(c, mid):
    """A model by id: built in (MODELS) or fitted on this database (score_models). None if unknown."""
    if mid in MODELS:
        return MODELS[mid]
    if not mid or c is None or not _has(c, "score_models"):
        return None
    r = c.execute("SELECT * FROM score_models WHERE id=?", (mid,)).fetchone()
    if not r:
        return None
    m = json.loads(r["params"])
    m.update(validation=json.loads(r["validation"] or "{}"), fingerprint=json.loads(r["fingerprint"] or "null"),
             fitted=time.strftime("%Y-%m-%d", time.localtime(r["fitted_at"])))
    return m


def save_model(c, mid, params, validation, fp):
    """Store a fitted model. Versions are never overwritten: an existing id is an error."""
    c.execute(MODELS_SCHEMA)
    if mid in MODELS or c.execute("SELECT 1 FROM score_models WHERE id=?", (mid,)).fetchone():
        raise ValueError(f"model {mid} already exists; versions are never overwritten")
    c.execute("INSERT INTO score_models(id,params,validation,fitted_at,fingerprint) VALUES(?,?,?,?,?)",
              (mid, json.dumps(params), json.dumps(validation), time.time(), json.dumps(fp)))
    c.commit()


def which(c):
    """(model id, why) for this install. See the module docstring."""
    chosen = db.get_state(c, "scores_model")
    if chosen:
        if get_model(c, chosen):
            return chosen, f"chosen for this install (sync_state scores_model = {chosen})"
        bad = f"the chosen model {chosen} doesn't exist here; "
    else:
        bad = ""
    if fitted_here(c, MODELS[OWNER].get("fingerprint")):
        return OWNER, bad + "this database holds the WHOOP archive v1 was fitted on"
    return GENERIC, bad + "no model fitted to this person's WHOOP archive (calibrate.py makes one)"


def current(c):
    """The model id this install shows."""
    return which(c)[0]


# ---------------- personal numbers for models that leave them open (generic-v1) ----------------
def profile_age(c, today=None):
    """Age in years from the local profile (api_profile "age", or a birthday YYYY-MM-DD), else None."""
    if not _has(c, "api_profile"):
        return None
    p = {r["key"]: r["value"] for r in c.execute(
        "SELECT key, value FROM api_profile WHERE key IN ('age','birthday','birth_date','date_of_birth','birth_year')")}
    try:
        if p.get("birth_year") and not p.get("age"):        # energy.py stores age this way
            a = int((today or time.strftime("%Y-%m-%d"))[:4]) - int(float(p["birth_year"]))
            return float(a) if 10 <= a <= 100 else None
        if p.get("age"):
            a = float(p["age"])
            return a if 10 <= a <= 100 else None
        b = p.get("birthday") or p.get("birth_date") or p.get("date_of_birth")
        if b:
            y, mo, d = (int(x) for x in b[:10].split("-"))
            ty, tm, td = (int(x) for x in (today or time.strftime("%Y-%m-%d")).split("-"))
            a = ty - y - ((tm, td) < (mo, d))
            return float(a) if 10 <= a <= 100 else None
    except (ValueError, TypeError):
        pass
    return None


SUSTAINED_S = 10        # "sustained": this many seconds in a row, as in heart_zones()
HR_CEILING = 230        # beats above this are a bad optical reading, not a heart


def seen_max_hr(c):
    """Highest heart rate the strap has recorded for 10 s in a row (the lowest beat of the best
    run). Scans only rows added since the last call (kept in sync_state)."""
    st = json.loads(db.get_state(c, "scores_max_seen") or "{}")
    since, best = st.get("ts", 0.0), st.get("hr")
    run, prev, last = [], None, since
    for r in c.execute("SELECT ts, hr FROM ble_hr WHERE ts > ? AND hr > 0 ORDER BY ts", (since - SUSTAINED_S,)):
        ts, hr = r["ts"], r["hr"]
        if hr > HR_CEILING or (prev is not None and ts - prev > 2):
            run = []
        if hr <= HR_CEILING:
            run = (run + [hr])[-SUSTAINED_S:]
            if len(run) == SUSTAINED_S:
                lo = min(run)
                best = lo if best is None else max(best, lo)
        prev, last = ts, max(last, ts)
    if last > since:
        db.set_state(c, "scores_max_seen", json.dumps({"ts": last, "hr": best}))
    return best


def personal_max_hr(c, m):
    """(max HR, where it came from) for a model whose max_hr is left open."""
    age = profile_age(c)
    if age:
        base, src = 208 - 0.7 * age, f"208 - 0.7 x age {age:.0f} (Tanaka)"
    else:
        base, src = m["max_hr_no_age"], "180, Tanaka at 40 (no age in the profile)"
    seen = seen_max_hr(c)
    if seen and seen > base:
        return float(seen), "the highest rate the strap has seen you hold for 10 s"
    return round(base, 1), src


def personal_sleep_baseline(c, date, m):
    """(hours, where it came from): median of your last 30 finished nights before `date`, once
    there are `baseline_min_nights`; the default before that; kept within baseline_range_h."""
    hours = []
    if _has(c, "sleep_night"):
        lo, hi = GATES["sleep_h"]
        hours = [r["asleep_s"] / 3600 for r in c.execute(
            "SELECT asleep_s FROM sleep_night WHERE date < ? AND status='done' AND asleep_s > 0 "
            "ORDER BY date DESC LIMIT 30", (date,)) if lo <= r["asleep_s"] / 3600 <= hi]
    need = m["baseline_min_nights"]
    if len(hours) < need:
        return m["baseline_default_h"], f"default {m['baseline_default_h']:g} h until {need} nights ({len(hours)} so far)"
    a, b = m["baseline_range_h"]
    med = learn._median(hours)
    v = min(b, max(a, med))
    return round(v, 2), f"your median of {len(hours)} nights" + ("" if v == med else f" ({med:.1f} h, kept within {a:g}-{b:g} h)")


def personalise(c, date, m):
    """The model with its open numbers (None) filled in from this person's data for `date`."""
    st, sl = m["strain"], m["sleep"]
    if st.get("max_hr") is None:
        top, src = personal_max_hr(c, st)
        st = dict(st, max_hr=top, max_hr_from=src)
    if sl.get("baseline_h") is None:
        base, src = personal_sleep_baseline(c, date, sl)
        sl = dict(sl, baseline_h=base, baseline_from=src)
    return dict(m, strain=st, sleep=sl)


# ---------------- strain ----------------
def heart_zones(c, a, b, rest, top):
    """Minutes in WHOOP zones 0..5 for [a, b), plus minutes under zone 0, recorded seconds,
    average and highest heart rate. Seconds further than a minute apart are a gap."""
    rows = c.execute("SELECT ts, hr FROM ble_hr WHERE ts >= ? AND ts < ? AND hr > 0 ORDER BY ts",
                     (a, b)).fetchall()
    mins, rec, tot, hi, prev = [0.0] * 6, 0.0, 0.0, 0, None
    run = []                                   # for "sustained": 10 s in a row at or over max
    for r in rows:
        dt = min(r["ts"] - prev, 1.0) if prev is not None and r["ts"] - prev < 60 else 1.0
        prev = r["ts"]
        v = (r["hr"] - rest) / (top - rest)
        z = 0 if v < 0.5 else min(5, int((v - 0.5) * 10) + 1)
        mins[z] += dt / 60
        rec += dt
        tot += r["hr"] * dt
        run = run + [r["hr"]] if r["hr"] >= top else []
        if len(run) >= 10:
            hi = max(hi, min(run))
    return {"mins": mins, "recorded_s": rec, "avg": tot / rec if rec else None,
            "max": max((r["hr"] for r in rows), default=None), "sustained_over_max": hi or None}


def day_window(c, date, now=None):
    """(start, end, ongoing) of the physiological day `date`: from falling asleep the night
    before to falling asleep the next night (WHOOP's cycle), else 04:00 to 04:00."""
    now = now or time.time()
    s0, s1 = _sleep_row(c, date), _sleep_row(c, _shift(date, 1))
    a = s0["onset"] if s0 else _midnight(date) + DAY_START_H * 3600
    b = s1["onset"] if s1 else _midnight(_shift(date, 1)) + DAY_START_H * 3600
    return a, min(b, now), b > now


def strain(c, date, rest, m, now=None):
    a, b, ongoing = day_window(c, date, now)
    if b <= a:
        return None
    top = m["max_hr"]
    z = heart_zones(c, a, b, rest, top)
    if z["sustained_over_max"]:                # WHOOP raises max HR when it sees a higher one
        top = z["sustained_over_max"]
        z = heart_zones(c, a, b, rest, top)
    w, (p0, p1) = m["weights"], m["zone0_share"]
    span_min = (b - a) / 60
    below = span_min - sum(z["mins"][1:])     # minutes not in zones 1-5, recorded or not
    share = min(1.0, max(0.0, p0 + p1 * ((z["avg"] or 70) - 70)))
    load = sum(wi * mi for wi, mi in zip(w[1:], z["mins"][1:])) + w[0] * below * share
    cover = z["recorded_s"] / (b - a)
    value = round(strain_map(load, m), 1)
    ok, why = True, None
    if ongoing:                    # the day model was checked on whole days only
        ok, why = False, "the day isn't over yet (the Today page shows it as it happens)"
    elif cover < GATES["strain_coverage"]:
        ok, why = False, f"heart rate recorded for {cover:.0%} of the day (needs {GATES['strain_coverage']:.0%})"
    row = {"start": _iso(a), "end": None if ongoing else _iso(b), "strain": value,
           "avg_hr": round(z["avg"]) if z["avg"] else None, "max_hr": z["max"], "kilojoule": None,
           "zones_min": [round(x, 1) for x in z["mins"]]}
    return {"value": value, "ok": ok, "why": why, "row": row,
            "inputs": {"rest": rest, "max_hr": top, "coverage": round(cover, 3), "load": round(load, 1),
                       "ongoing": ongoing, **({"max_hr_from": m["max_hr_from"]} if "max_hr_from" in m else {})}}


def add_energy(c, date, st, now=None, em=ENERGY_V1):
    """Put the day's energy (energy.py) into the strain estimate's row as kilojoule, or None with
    the reason in inputs["energy"]["why"]. The strain value and its ok/why are not touched."""
    a, b, ongoing = day_window(c, date, now)
    kj, inputs = energy.for_day(c, date, a, b, st["inputs"]["rest"], st["inputs"]["max_hr"], ongoing,
                                em, GATES["strain_coverage"])
    st["row"]["kilojoule"] = kj
    st["row"]["energy_why"] = inputs.get("why") if kj is None else None     # for the app's "held back"
    st["inputs"]["energy"] = dict(inputs, version="energy_v1")
    return st


# ---------------- sleep ----------------
def consistency(c, date, m):
    """WHOOP-style consistency: share of the day's minutes in the same state (asleep/awake) on
    each of the last 4 days, ending this morning. None until 4 nights are known."""
    nights = [_sleep_row(c, _shift(date, -i)) for i in range(4)]
    if not all(nights):
        return None
    end = nights[0]["wake"]
    spans = [(n["onset"], n["wake"]) for n in nights]
    same = total = 0
    for k in range(0, 1440, 5):
        states = [any(s <= end - (d + 1) * 86400 + k * 60 < e for s, e in spans) for d in range(4)]
        same += all(x == states[0] for x in states)
        total += 1
    a, b = m["consistency_scale"]
    return max(0.0, min(100.0, a * 100 * same / total + b))


def sleep(c, date, prev_strain, prev_sleep, m):
    n = _sleep_row(c, date)
    if not n:
        return None
    in_bed = n["wake"] - n["onset"]
    asleep_h = (n["asleep_s"] or 0) / 3600
    debt = prev_sleep["inputs"]["shortfall_h"] * m["debt_carry"] if prev_sleep else 0.0
    naps = 0.0                                 # sleep.py finds the main night only
    s_need = need_from_strain(prev_strain if prev_strain is not None else 10.0)
    need = m["baseline_h"] + s_need + debt - naps
    hours_pct = min(100.0, 100 * asleep_h / need)
    eff = 100 * (n["asleep_s"] or 0) / in_bed if in_bed > 0 else None
    cons = consistency(c, date, m)
    if m.get("blend") is None:                 # hours against need only (generic-v1)
        perf = hours_pct
        basis = "hours vs need"
    elif cons is not None:
        a, b, cc, d = m["blend"]
        perf = a * hours_pct + b * eff + cc * cons + d
        basis = "hours+efficiency+consistency"
    else:
        a, b, d = m["blend_no_consistency"]
        perf = a * hours_pct + b * eff + d
        basis = "hours+efficiency"
    perf = round(max(0.0, min(100.0, perf)))
    cover = c.execute("SELECT COUNT(*) n FROM ble_hr WHERE ts >= ? AND ts < ? AND hr > 0",
                      (n["onset"], n["wake"])).fetchone()["n"] / max(in_bed, 1)
    rv = RESP_RATE_CURRENT
    resp = metrics.resp_rate(c, n["onset"], n["wake"], RESP_RATE[rv]["params"])
    lo, hi = GATES["sleep_h"]
    ok, why = True, None
    if not lo <= asleep_h <= hi:
        ok, why = False, f"{asleep_h:.1f} h asleep is outside {lo:.0f}-{hi:.0f} h: likely a detection problem"
    elif cover < GATES["night_coverage"]:
        ok, why = False, f"heart rate recorded for {cover:.0%} of the night"
    row = {"start": _iso(n["onset"]), "end": _iso(n["wake"]), "sleep_perf": perf,
           "sleep_efficiency": round(eff, 1) if eff is not None else None,
           "sleep_consistency": round(cons) if cons is not None else None, "resp_rate": resp["bpm"],
           "total_in_bed_ms": int(in_bed * 1000), "total_awake_ms": int((n["awake_s"] or 0) * 1000),
           "total_light_ms": None, "total_sws_ms": None, "total_rem_ms": None,
           "disturbances": n["wakeups"], "need_ms": int(need * 3600e3)}
    return {"value": perf, "ok": ok, "why": why, "row": row,
            "inputs": {"asleep_h": round(asleep_h, 2), "need_h": round(need, 2), "need_strain_h": round(s_need, 2),
                       "debt_h": round(debt, 2), "shortfall_h": round(max(0.0, need - asleep_h), 2),
                       "prev_strain": prev_strain, "basis": basis, "coverage": round(cover, 3),
                       "resp_version": rv, "resp_windows": resp["windows"], "resp_windows_tried": resp["of"],
                       **({"baseline_h": m["baseline_h"], "baseline_from": m["baseline_from"]}
                          if "baseline_from" in m else {})}}


# ---------------- recovery ----------------
def recovery(c, date, sleep_est, m):
    morning = _midnight(date)
    sn = _sleep_row(c, date)
    window = ((sn["onset"] - morning) / 3600, (sn["wake"] - morning) / 3600) if sn else learn.night_window(c)
    night = metrics.night_stats(c, morning, window)
    if not night:
        return None
    u = learn.usual(c, date)
    ok, why = True, None
    if night.get("suspect"):
        ok, why = False, "the night's resting heart rate is outside what a heart does: a bad reading"
    elif u["source"] != "own":
        ok, why = False, f"learning your usual: {u['nights']} of {GATES['min_nights']} strap nights"
    elif not night.get("rmssd") or night.get("n_rr", 0) < GATES["min_rr"]:
        ok, why = False, f"only {night.get('n_rr', 0)} clean beat pairs overnight (needs {GATES['min_rr']})"
    value, basis = None, None
    if u.get("rhr") and night.get("rmssd") and u.get("hrv"):
        zr = (night["resting"] - u["rhr"]) / u.get("rhr_spread", learn.RHR_SPREAD[1])
        zh = math.log(night["rmssd"] / u["hrv"]) / u.get("hrv_spread", learn.HRV_SPREAD[1])
        if sleep_est and sleep_est["ok"]:
            a, b, w, s = m["with_sleep"]
            value, basis = a + b * zr + w * zh + s * (sleep_est["value"] - 80) / 10, "hrv+rhr+sleep"
        else:
            a, b, w = m["without_sleep"]
            value, basis = a + b * zr + w * zh, "hrv+rhr"
        value = round(min(99.0, max(1.0, value)))
    elif ok:
        ok, why = False, "no baseline to compare with"
    row = {"created_at": _iso(sn["wake"] if sn else morning + window[1] * 3600), "recovery_score": value,
           "hrv_rmssd_ms": round(night["rmssd"], 1) if night.get("rmssd") else None,
           "resting_hr": round(night["resting"], 1), "spo2": None, "skin_temp_c": None}
    return {"value": value, "ok": ok and value is not None, "why": why, "row": row,
            "inputs": {"basis": basis, "n_rr": night.get("n_rr"), "usual_rhr": u.get("rhr"),
                       "usual_hrv": u.get("hrv"), "usual_source": u["source"]}}


# ---------------- store ----------------
def _get(c, date, kind, version):
    r = c.execute("SELECT * FROM estimates WHERE date=? AND kind=? AND version=?", (date, kind, version)).fetchone()
    if not r:
        return None
    return {"value": r["value"], "ok": bool(r["ok"]), "why": r["why"], "row": json.loads(r["row"]),
            "inputs": json.loads(r["inputs"])}


def _put(c, date, kind, version, est):
    if not est:
        return
    c.execute("INSERT INTO estimates(date,kind,version,value,ok,why,row,inputs,computed_at) VALUES(?,?,?,?,?,?,?,?,?) "
              "ON CONFLICT(date,kind,version) DO UPDATE SET value=excluded.value, ok=excluded.ok, why=excluded.why, "
              "row=excluded.row, inputs=excluded.inputs, computed_at=excluded.computed_at",
              (date, kind, version, est["value"], int(est["ok"]), est["why"], json.dumps(est["row"]),
               json.dumps(est["inputs"]), time.time()))


def first_day(c):
    r = c.execute("SELECT MIN(ts) t FROM ble_hr WHERE hr > 0").fetchone()
    return learn._date(r["t"]) if r and r["t"] else None


def update(c=None, days=7, version=None, now=None, only=None):
    """(Re)compute the last `days` days (oldest first: sleep needs the day before's strain and
    shortfall) with `version` (default: this install's current()). Returns {date: {kind: estimate}}."""
    c = c or db.connect()
    c.execute(SCHEMA)
    now = now or time.time()
    version = version or current(c)
    base = get_model(c, version)
    if base is None:
        raise ValueError(f"unknown scores model {version}")
    today = learn._date(now)
    start = first_day(c)
    if not start:
        return {}
    dates = [_shift(today, -i) for i in range(days - 1, -1, -1)]
    dates = [d for d in dates if d >= start and (not only or d == only)]
    out = {}
    for d in dates:
        u = learn.usual(c, d)
        rest = u.get("rhr") or (u.get("then") or {}).get("rhr") or base["strain"].get("rest_default", 50.0)
        m = personalise(c, d, base)
        prev_strain = _get(c, _shift(d, -1), "strain", version)
        prev_sleep = _get(c, _shift(d, -1), "sleep", version)
        sl = sleep(c, d, prev_strain["value"] if prev_strain and prev_strain["ok"] else None,
                   prev_sleep if prev_sleep and prev_sleep["ok"] else None, m["sleep"])
        rc = recovery(c, d, sl, m["recovery"])
        st = strain(c, d, rest, m["strain"], now)
        if st:
            add_energy(c, d, st, now)
        for kind, est in (("sleep", sl), ("recovery", rc), ("strain", st)):
            _put(c, d, kind, version, est)
        out[d] = {"sleep": sl, "recovery": rc, "strain": st}
    c.commit()
    return out


def for_app(c, version=None, limit=400):
    """Trusted estimates shaped like the WHOOP tables the app already draws, newest first,
    each marked with `est` (the version) and `basis`."""
    out = {"recovery": [], "sleep": [], "cycle": [], "held": []}
    if not _has(c, "estimates"):
        return out
    version = version or current(c)
    kind_to = {"recovery": "recovery", "sleep": "sleep", "strain": "cycle"}
    for r in c.execute("SELECT date, kind, row, inputs FROM estimates WHERE version=? AND ok=1 "
                       "ORDER BY date DESC LIMIT ?", (version, limit)):
        row = json.loads(r["row"])
        row.update(est=version, est_date=r["date"], basis=json.loads(r["inputs"]).get("basis"))
        out[kind_to[r["kind"]]].append(row)
    # the last two weeks' held-back estimates, so the app can say why a day has no score
    out["held"] = [{"date": r["date"], "kind": r["kind"], "why": r["why"]} for r in c.execute(
        "SELECT date, kind, why FROM estimates WHERE version=? AND ok=0 AND date >= ? ORDER BY date DESC",
        (version, learn._date(time.time() - 14 * 86400)))]
    return out


# ---------------- validation on the WHOOP archive ----------------
def _utc(s):
    return calendar.timegm(time.strptime(s[:19], "%Y-%m-%dT%H:%M:%S"))


def check(c, version=None):
    """Re-run the version's numbers against the WHOOP archive and report the errors (all days,
    in-sample: the held-out figures in MODELS come from fitting on the first 70% only)."""
    m = get_model(c, version or current(c))
    ZN = ["zone_zero_milli", "zone_one_milli", "zone_two_milli", "zone_three_milli", "zone_four_milli", "zone_five_milli"]
    res = {}
    # strain on workouts (zone minutes from WHOOP, Dec 2024 on, HR-reserve zones)
    errs = []
    for w in c.execute("SELECT strain, zone_raw FROM api_workout WHERE zone_raw LIKE '%zone_zero%' AND strain > 0 "
                       "AND start >= '2024-12'"):
        z = json.loads(w["zone_raw"])
        mins = [z[k] / 6e4 for k in ZN]
        if sum(mins[1:]) < 5 and w["strain"] > 8:      # logged by hand, no heart rate behind it
            continue
        errs.append(abs(strain_map(sum(a * b for a, b in zip(m["strain"]["weights"], mins)), m["strain"]) - w["strain"]))
    res["strain_workouts"] = (len(errs), sum(errs) / len(errs) if errs else None)
    # sleep need and performance before May 2025 (hours vs need)
    cyc = [(_utc(r["end"]), r["strain"]) for r in c.execute("SELECT end, strain FROM api_cycle WHERE end IS NOT NULL AND strain > 0")]
    errs, debt = [], 0.0
    for s in c.execute("SELECT start, sleep_perf, total_in_bed_ms, total_awake_ms FROM api_sleep WHERE nap=0 AND "
                       "sleep_perf IS NOT NULL AND total_in_bed_ms > 0 AND start < '2025-05-01' ORDER BY start"):
        t = _utc(s["start"])
        prev = [st for e, st in cyc if abs(e - t) < 1800]
        if not prev:
            debt = 0.0
            continue
        asleep = (s["total_in_bed_ms"] - s["total_awake_ms"]) / 3.6e6
        need = (m["sleep"]["baseline_h"] or m["sleep"]["baseline_default_h"]) + need_from_strain(prev[0]) + debt
        errs.append(abs(min(100, 100 * asleep / need) - s["sleep_perf"]))
        debt = m["sleep"]["debt_carry"] * max(0.0, need - asleep)
    res["sleep_hours_pre_may25"] = (len(errs), sum(errs) / len(errs) if errs else None)
    return res


def _print_day(d, e):
    parts = []
    for k in ("recovery", "sleep", "strain"):
        x = e.get(k)
        if not x:
            parts.append(f"{k} —")
        else:
            v = x["value"]
            parts.append(f"{k} {v if v is not None else '—'}{'' if x['ok'] else ' (held back: ' + str(x['why']) + ')'}")
    print(d, " | ".join(parts))


if __name__ == "__main__":
    c = db.connect()
    args = sys.argv[1:]
    if args[:1] == ["which"]:
        mid, why = which(c)
        m = get_model(c, mid)
        print(f"{mid}: {why}")
        for k in ("strain", "sleep", "recovery"):
            print(f"  {k}: {json.dumps(m[k].get('validation') or (m.get('validation') or {}).get(k))}")
    elif args[:1] == ["check"]:
        for k, (n, mae) in check(c).items():
            print(f"{k:24s} n={n:4d}  mean error {mae:.2f}" if mae is not None else f"{k}: no data")
    else:
        only = args[0] if args else None
        for d, e in update(c, days=30 if only else 7, only=only).items():
            _print_day(d, e)
