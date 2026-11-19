#!/usr/bin/env python3
"""Stress monitor: how activated your body is right now, next to your own calm, on a 0-3 scale.

What it follows
  WHOOP's public description of its Stress Monitor (2023): a live 0-3 score from heart rate
  and HRV, compared with a personal baseline from the last 14 days, with motion taken into
  account so exercise isn't read as stress. 0-1 low, 1-2 medium ("alert, focused"), 2-3 high.
  WHOOP has not published its weighting, so the arithmetic below is ours.
  The physiology: acute mental stress raises heart rate and lowers vagally mediated HRV
  (RMSSD) (Kim et al. 2018, Psychiatry Investigation meta-analysis; Castaldo et al. 2015).
  RMSSD needs a few minutes of clean beats: 5 minutes is the standard short-term window
  (Task Force 1996); 1-5 minute RMSSD tracks it well (Shaffer & Ginsberg 2017).
  Posture, talking, digestion, heat and caffeine also move HR and HRV; a wrist strap can't tell
  those apart from stress, and caffeine's effect on resting HRV is small and inconsistent in the
  literature (Koenig et al. 2013 review). So this reads "activation", not the cause.

How each 5-minute window is scored
  1. Not scored when the strap has under 3 minutes of heart rate in it (off the wrist, or not
     synced yet), during detected sleep (sleep.py), while the wrist moves (motion_min, from the
     accelerometer), when the heart rate is in an exercise zone (40% of heart-rate reserve or
     more), in the 5 minutes after moving (heart rate is still settling), when the strap's
     movement for that time hasn't arrived yet, or with fewer than 30 clean beat pairs.
  2. Otherwise: zHR = how far the window's median heart rate is above your calm baseline, and
     zHRV = how far its ln(RMSSD) is below it, each in units of your own calm-to-calm variation.
     Activation = the mean of the two. Score = 0.5 + activation / 2, kept within 0-3: your
     typical calm minute is 0.5 (Low), 1 variation above it is 1.0 (Medium), 3 above is 2.0 (High).
  3. Baseline: the windows that passed step 1 while awake on the previous 14 days (median and
     scaled MAD), once there are 3 hours of them across 2 days. Until then a starting point:
     from your latest night (resting HR + 8 bpm, HRV x 0.85: awake and still reads a little higher
     HR and lower HRV than asleep; on the first evening checked, one person's still, awake windows
     sat at +8 bpm and x1.1, so this leans cautious) or, with no night yet, general adult values
     (HR 72, RMSSD 35 ms; short-term resting norms in Nunan et al. 2010 centre near 70 bpm and 42 ms).

Not a diagnosis and not medical advice.

    python stress.py               # store today's windows and print them
    python stress.py 2026-11-19    # one day, printed, nothing stored
"""
import json, math, statistics, sys, time
import db, learn, metrics

WINDOW = 300            # seconds per score
MIN_HR_S = 180          # seconds of heart rate a window needs
MIN_MOTION_MIN = 3      # minutes of movement data a window needs
MOVE = 0.15             # wrist movement per minute (g per one-second step) that counts as moving
MOVE_PEAK = 0.30        # one minute over this is enough
MOVE_MEAN = 0.08        # or the window's average over this
ACTIVE_HRR = 0.40       # heart rate at this share of heart-rate reserve or more: exercise
MIN_PAIRS = 30          # clean beat pairs for a window's RMSSD
BASE_DAYS = 14
BASE_MIN = 36           # calm windows (3 hours) before your own baseline is used...
BASE_MIN_DAYS = 2       # ...spread over at least this many days
HR_SD = (3.0, 6.0)      # bpm: floor, default
LN_SD = (0.15, 0.35)    # ln(RMSSD): floor, default
DEFAULT = {"hr": 72.0, "rmssd": 35.0}
FROM_NIGHT = {"hr": 8.0, "rmssd": 0.85}
FRESH = 30 * 60         # "now" looks back this far
LEVELS = ((2.0, "High"), (1.0, "Medium"), (0.0, "Low"))

SCHEMA = """CREATE TABLE IF NOT EXISTS stress (
  ts REAL PRIMARY KEY, date TEXT, state TEXT, score REAL, hr REAL, rmssd REAL, n_rr INTEGER,
  move REAL, computed_at REAL)"""
COLS = "ts,date,state,score,hr,rmssd,n_rr,move,computed_at"

# Why a window has no score, in words (the app and the worker have the same list).
WHY = {
    "no_data": "No heart rate from the strap (off the wrist, or not synced yet).",
    "asleep": "Asleep.",
    "no_motion": "The strap's movement data for this time hasn't come in yet.",
    "moving": "You're moving, so a raised heart rate says nothing about stress.",
    "active": "Heart rate is in an exercise zone.",
    "settling": "Just stopped moving; heart rate is still settling.",
    "few_beats": "Not enough clean beats to read HRV.",
}


def level(score):
    if score is None:
        return None
    return next(name for lo, name in LEVELS if score >= lo)


def _median(xs):
    return statistics.median(xs) if xs else None


def _mad_sd(xs, floor, default):
    if len(xs) < 5:
        return default
    m = statistics.median(xs)
    return max(floor, 1.4826 * statistics.median([abs(x - m) for x in xs]))


def _midnight(date):
    return time.mktime(time.strptime(date, "%Y-%m-%d"))


def _shift(date, days):
    return time.strftime("%Y-%m-%d", time.localtime(_midnight(date) + days * 86400 + 43200))


def _has(c, t):
    return c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (t,)).fetchone() is not None


def _sleep_spans(c, a, b):
    if not _has(c, "sleep_night"):
        return []
    return [(r["onset"], r["wake"]) for r in c.execute(
        "SELECT onset, wake FROM sleep_night WHERE onset < ? AND wake > ?", (b, a))]


def zones(c, date):
    """(resting, max) heart rate for the exercise-zone check."""
    u = learn.usual(c, date)
    rest = u.get("rhr") or (u.get("then") or {}).get("rhr") or 60.0
    top = learn.max_hr(c)
    if not top:
        import scores                       # the calibrated max the strain estimate uses
        top = (scores.get_model(c, scores.current(c)) or scores.MODELS["v1"])["strain"].get("max_hr") or 180.0
    return rest, top


def windows(c, date, now=None):
    """Every 5-minute window of local day `date` up to `now`, with its state and raw numbers
    (no score: that needs a baseline). Oldest first."""
    now = now or time.time()
    a = _midnight(date)
    b = min(_midnight(_shift(date, 1)), now - now % WINDOW)
    if b <= a:
        return []
    n = int((b - a) // WINDOW)
    rows = [[] for _ in range(n)]
    for r in c.execute("SELECT ts, hr, rr_ms FROM ble_hr WHERE ts >= ? AND ts < ? AND hr > 0 ORDER BY ts", (a, b)):
        rows[int((r["ts"] - a) // WINDOW)].append(r)
    move = [[] for _ in range(n)]
    if _has(c, "motion_min"):
        for r in c.execute("SELECT minute, move_sum, n FROM motion_min WHERE minute >= ? AND minute < ?", (a, b)):
            if r["n"] >= 20:
                move[int((r["minute"] - a) // WINDOW)].append(r["move_sum"] / r["n"])
    spans = _sleep_spans(c, a, b)
    rest, top = zones(c, date)
    active_hr = rest + ACTIVE_HRR * (top - rest)
    out, prev_busy = [], False
    for i in range(n):
        t = a + i * WINDOW
        rs, mv = rows[i], move[i]
        w = {"ts": t, "date": date, "hr": None, "rmssd": None, "n_rr": 0,
             "move": round(sum(mv) / len(mv), 4) if mv else None}
        if rs:
            w["hr"] = float(statistics.median(r["hr"] for r in rs))
        moving = len(mv) >= 1 and (max(mv) > MOVE_PEAK or sum(mv) / len(mv) > MOVE_MEAN or
                                   sum(1 for x in mv if x > MOVE) >= 2)
        busy = False
        if len(rs) < MIN_HR_S:
            state = "no_data"
        elif any(s <= t + WINDOW / 2 < e for s, e in spans):
            state = "asleep"
        elif moving:
            state, busy = "moving", True
        elif w["hr"] >= active_hr:
            state, busy = "active", True
        elif len(mv) < MIN_MOTION_MIN:
            state = "no_motion"
        elif prev_busy:
            state = "settling"
        else:
            diffs = metrics.beat_diffs(rs)
            w["n_rr"] = len(diffs)
            w["rmssd"] = metrics.rmssd(diffs, MIN_PAIRS)
            plausible = w["rmssd"] is not None and metrics.HRV_PLAUSIBLE[0] <= w["rmssd"] <= metrics.HRV_PLAUSIBLE[1]
            state = "ok" if plausible else "few_beats"
            if not plausible:
                w["rmssd"] = None
        w["state"] = state
        prev_busy = busy
        out.append(w)
    return out


def _stored(c, date, now=None):
    """Windows stored for a day at least two days back, or None. update() rewrites yesterday on
    every run, so by then the stored rows hold everything the strap handed over for that day."""
    if not _has(c, "stress") or date > _shift(learn._date(now or time.time()), -2):
        return None
    rs = [dict(r) for r in c.execute(f"SELECT {COLS} FROM stress WHERE date=? ORDER BY ts", (date,))]
    return rs or None


def _night(c, date):
    """Latest night before or on `date` with resting HR and HRV (sleep.py, else learn.py's rows)."""
    for table in ("sleep_night", "daily_night"):
        if _has(c, table):
            r = c.execute(f"SELECT rhr, hrv FROM {table} WHERE date <= ? AND rhr IS NOT NULL AND hrv IS NOT NULL "
                          "ORDER BY date DESC LIMIT 1", (date,)).fetchone()
            if r and metrics.RHR_PLAUSIBLE[0] <= r["rhr"] <= metrics.RHR_PLAUSIBLE[1]:
                return r["rhr"], r["hrv"]
    return None


def baseline(c, date, now=None):
    """Your calm: from the previous 14 days' calm, still, awake windows, or a starting point."""
    calm, days = [], set()
    for k in range(1, BASE_DAYS + 1):
        d = _shift(date, -k)
        a = _midnight(d)
        if not c.execute("SELECT 1 FROM ble_hr WHERE ts >= ? AND ts < ? LIMIT 1", (a, a + 86400)).fetchone():
            continue
        ws = _stored(c, d, now) or windows(c, d, now=_midnight(_shift(d, 1)))
        ok = [w for w in ws if w["state"] == "ok"]
        calm += ok
        if ok:
            days.add(d)
    hrs = [w["hr"] for w in calm]
    lns = [math.log(w["rmssd"]) for w in calm]
    have = {"windows": len(calm), "days": len(days)}
    if len(calm) >= BASE_MIN and len(days) >= BASE_MIN_DAYS:
        b = {"source": "own", "hr": _median(hrs), "rmssd": math.exp(_median(lns)),
             "hr_sd": _mad_sd(hrs, *HR_SD), "ln_sd": _mad_sd(lns, *LN_SD)}
    else:
        night = _night(c, date)
        if night:
            b = {"source": "night", "hr": night[0] + FROM_NIGHT["hr"], "rmssd": night[1] * FROM_NIGHT["rmssd"]}
        else:
            b = {"source": "default", "hr": DEFAULT["hr"], "rmssd": DEFAULT["rmssd"]}
        b.update(hr_sd=HR_SD[1], ln_sd=LN_SD[1])
    b.update(have, date=date, need={"windows": BASE_MIN, "days": BASE_MIN_DAYS})
    b["text"] = baseline_text(b)
    return b


def baseline_text(b):
    if b["source"] == "own":
        return (f"Compared with your own calm, still, awake times over the last {BASE_DAYS} days "
                f"({b['windows'] * WINDOW // 60} minutes on {b['days']} days: heart rate around {b['hr']:.0f}, "
                f"HRV around {b['rmssd']:.0f} ms).")
    so_far = f"{b['windows'] * WINDOW // 60} of {BASE_MIN * WINDOW // 60} minutes so far"
    start = ("a starting point guessed from your latest night" if b["source"] == "night"
             else "general adult values")
    return (f"Still learning your calm: until the strap has seen {BASE_MIN * WINDOW // 3600} hours of you still "
            f"and awake on {BASE_MIN_DAYS} days ({so_far}), this compares with {start} "
            f"(heart rate {b['hr']:.0f}, HRV {b['rmssd']:.0f} ms), so treat it as rough.")


def score(w, b):
    """0-3 for a window in state ok, else None."""
    if w["state"] != "ok" or not w["rmssd"]:
        return None
    zhr = (w["hr"] - b["hr"]) / b["hr_sd"]
    zhrv = (math.log(b["rmssd"]) - math.log(w["rmssd"])) / b["ln_sd"]
    return round(min(3.0, max(0.0, 0.5 + (zhr + zhrv) / 4)), 2)


def now_value(series, t=None):
    """The 'right now' reading, from the last 30 minutes: the newest window with a score, unless
    a newer one says why there can't be one (moving, exercise, settling, too few beats). Windows
    still waiting for the strap's movement data don't count against an earlier score."""
    t = t or time.time()
    recent = [w for w in series if w["ts"] + WINDOW >= t - FRESH and w["state"] != "no_data"]
    told = [w for w in recent if w["state"] != "no_motion"]
    if told:
        w = told[-1]
        return {"ts": w["ts"], "state": w["state"], "score": w["score"], "level": level(w["score"]),
                "why": None if w["state"] == "ok" else WHY[w["state"]],
                "pending": recent[-1]["state"] == "no_motion"}
    if recent:
        return {"ts": None, "state": "no_motion", "score": None, "level": None, "why": WHY["no_motion"],
                "pending": True}
    return {"ts": None, "state": "no_data", "score": None, "level": None, "pending": False,
            "why": "Nothing from the strap in the last 30 minutes."}


def day(c, date=None, now=None, base=None):
    """Today's (or `date`'s) series with scores, the 'now' reading and the baseline."""
    now = now or time.time()
    date = date or learn._date(now)
    b = base or cached_baseline(c, date, now)
    series = []
    for w in windows(c, date, now):
        w["score"] = score(w, b)
        series.append(w)
    return {"date": date, "series": series, "now": now_value(series, now), "baseline": b}


def cached_baseline(c, date, now=None):
    """The day's baseline, worked out once per day (it only uses earlier days)."""
    try:
        got = json.loads(db.get_state(c, "stress_baseline") or "null")
    except ValueError:
        got = None
    if got and got.get("date") == date:
        return got
    b = baseline(c, date, now)
    try:
        db.set_state(c, "stress_baseline", json.dumps(b))
    except Exception:                       # a read-only database: just don't cache
        pass
    return b


def for_app(c, now=None):
    """What /api/summary carries: today's series (compact), now, and the baseline."""
    d = day(c, now=now)
    return {"date": d["date"], "now": d["now"], "baseline": d["baseline"],
            "series": [{"ts": w["ts"], "state": w["state"], "score": w["score"], "hr": w["hr"],
                        "rmssd": round(w["rmssd"], 1) if w["rmssd"] else None} for w in d["series"]]}


def update(c=None, now=None):
    """Store today's and yesterday's windows (the daemon runs this every few minutes)."""
    c = c or db.connect()
    c.execute(SCHEMA)
    now = now or time.time()
    today = learn._date(now)
    out = {}
    cols = ','.join('?' * len(COLS.split(',')))
    # older days the baseline reads, stored once (unscored: only their calm windows are used)
    for k in range(2, BASE_DAYS + 2):
        d = _shift(today, -k)
        a = _midnight(d)
        if c.execute("SELECT 1 FROM stress WHERE date=? LIMIT 1", (d,)).fetchone() or not c.execute(
                "SELECT 1 FROM ble_hr WHERE ts >= ? AND ts < ? LIMIT 1", (a, a + 86400)).fetchone():
            continue
        c.executemany(f"INSERT OR REPLACE INTO stress({COLS}) VALUES({cols})",
                      [(w["ts"], d, w["state"], None, w["hr"], w["rmssd"], w["n_rr"], w["move"], time.time())
                       for w in windows(c, d, now=_midnight(_shift(d, 1)))])
    for date in (_shift(today, -1), today):
        b = baseline(c, date, now) if date != today else cached_baseline(c, date, now)
        d = day(c, date, now, base=b)
        have = {r["ts"]: (r["state"], r["score"], r["hr"], r["rmssd"]) for r in c.execute(
            "SELECT ts, state, score, hr, rmssd FROM stress WHERE date=?", (date,))}
        stamp = time.time()
        # only rows that changed get a new computed_at, so cloud_sync pushes just those
        c.executemany(f"INSERT OR REPLACE INTO stress({COLS}) VALUES({cols})",
                      [(w["ts"], date, w["state"], w["score"], w["hr"], w["rmssd"], w["n_rr"], w["move"], stamp)
                       for w in d["series"]
                       if have.get(w["ts"]) != (w["state"], w["score"], w["hr"], w["rmssd"])])
        out[date] = d
    c.commit()
    db.set_state(c, "stress_now", json.dumps(out[today]["now"]))
    return out


def _clock(ts):
    return time.strftime("%H:%M", time.localtime(ts))


if __name__ == "__main__":
    c = db.connect()
    if len(sys.argv) > 1:
        d = day(c, sys.argv[1], now=_midnight(_shift(sys.argv[1], 1)), base=baseline(c, sys.argv[1]))
    else:
        d = update(c)[learn._date(time.time())]
    print(d["baseline"]["text"])
    for w in d["series"]:
        if w["state"] != "no_data":
            print(_clock(w["ts"]), f"{w['state']:9s}", f"hr {w['hr']:.0f}" if w["hr"] else "",
                  f"rmssd {w['rmssd']:.0f}" if w["rmssd"] else "", f"stress {w['score']:.2f} {level(w['score'])}"
                  if w["score"] is not None else "")
    n = d["now"]
    print("now:", n["level"] or n["why"], f"({n['score']})" if n["score"] is not None else "")
