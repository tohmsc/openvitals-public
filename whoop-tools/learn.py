#!/usr/bin/env python3
"""What the system has learned about you: from your WHOOP history, from your strap nights,
and from what you tell it.

From your WHOOP archive (the days WHOOP itself scored), fitted once and refreshed when the
archive changes:

  the recovery model  how far WHOOP's recovery score moved, for you, when your HRV and resting
                      heart rate moved away from your usual. The same relationship is applied
                      to the strap's nights to give a recovery estimate in percent. The fit is
                      checked on days it was not fitted on, and that accuracy is reported.
  your hours          when you usually fall asleep and wake, so "last night" covers your night
                      and not a fixed 22:00-06:00
  your range          WHOOP's max heart rate for you, so "heart rate up" means the same effort
                      it would for you, not for an average person
  your variation      how much your resting HR and HRV normally move from night to night

From your strap nights (one row per morning in daily_night):

  your usual          the median of your last 30 nights. The archive is not used as your usual
                      once it is older than 60 days: bodies change, and the strap's numbers are
                      measured differently. It is still shown for comparison ("then and now").

From you (/good, /ok, /rough on Telegram, or the buttons on the dashboard):

  your zones          where green starts and red ends. Told "take it lighter" on a day you felt
                      good, the red zone shrinks a little; told "push" on a day you felt rough,
                      the green zone does.

This is pattern-matching on resting heart rate and HRV. It does not detect or diagnose
anything, and it is not medical advice.

Run:  python learn.py                 # what it has learned
      python learn.py feel good       # record how you feel today (good / ok / rough)
"""
import calendar, json, math, sys, time
import db, metrics

MIN_NIGHTS = 3            # own nights needed before they define "your usual"
WINDOW_DAYS = 30
ARCHIVE_MAX_AGE_DAYS = 60 # WHOOP scores older than this are "then", not your usual now
RHR_SPREAD = (2.0, 3.0, 6.0)        # bpm: floor, default, ceiling
HRV_SPREAD = (0.10, 0.15, 0.35)     # log ratio: floor, default, ceiling
GREEN_MIN, RED_MAX = 67.0, 34.0     # WHOOP's own zone boundaries on recovery %
STEP = 2.0                          # recovery points one piece of feedback moves a boundary
MIN_FIT_DAYS = 60                   # archive days needed to fit a personal model
MIN_RR = 200                        # clean beat pairs a night needs before its HRV is believed
# Used when there is no archive to fit on: HRV moves recovery most, resting HR less.
DEFAULT_MODEL = {"both": [70.0, -3.0, 9.0], "rhr": [70.0, -8.0], "days": 0, "source": "default"}
FEEL = {"rough": 1, "ok": 2, "good": 3}
FEEL_WORD = {v: k for k, v in FEEL.items()}


def _median(xs):
    xs = sorted(xs)
    n = len(xs)
    return None if not n else xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2


def _spread(xs, floor, default, ceiling):
    """Robust night-to-night variation (scaled median absolute deviation), kept in sane bounds."""
    if len(xs) < 5:
        return default
    m = _median(xs)
    return min(ceiling, max(floor, 1.4826 * _median([abs(x - m) for x in xs])))


def _date(ts):
    return time.strftime("%Y-%m-%d", time.localtime(ts))


def _utc(s):
    return calendar.timegm(time.strptime(s[:19], "%Y-%m-%dT%H:%M:%S"))


def _cached(c, key, stamp, build):
    """build() once per `stamp` (e.g. the number of archive rows), remembered in sync_state."""
    try:
        got = json.loads(db.get_state(c, key) or "null")
    except ValueError:
        got = None
    if got and got.get("stamp") == stamp:
        return got["value"]
    value = build()
    db.set_state(c, key, json.dumps({"stamp": stamp, "value": value}))
    return value


# ---------------- from the WHOOP archive ----------------
def _archive(c):
    return c.execute("SELECT created_at, recovery_score r, resting_hr rhr, hrv_rmssd_ms hrv FROM api_recovery "
                     "WHERE recovery_score IS NOT NULL AND resting_hr > 0 AND hrv_rmssd_ms > 0 "
                     "ORDER BY created_at").fetchall()


def _least_squares(X, y):
    k = len(X[0])
    A = [[sum(x[i] * x[j] for x in X) for j in range(k)] for i in range(k)]
    b = [sum(x[i] * v for x, v in zip(X, y)) for i in range(k)]
    for i in range(k):
        p = A[i][i]
        if abs(p) < 1e-9:
            return None
        A[i] = [v / p for v in A[i]]; b[i] /= p
        for j in range(k):
            if j != i:
                f = A[j][i]
                A[j] = [vj - f * vi for vj, vi in zip(A[j], A[i])]; b[j] -= f * b[i]
    return b


def zone(recovery, green_min=GREEN_MIN, red_max=RED_MAX):
    return "green" if recovery >= green_min else "red" if recovery < red_max else "yellow"


def _fit(rec):
    """Fit recovery % on how far each archive night was from the 30 days before it.

    Deviations are in units of your own night-to-night variation (z), exactly as they are
    computed for strap nights, so the fitted weights carry over even though the strap's HRV
    is not on WHOOP's scale. Accuracy is measured on the last 30% of days, which the weights
    used for that measurement never saw.
    """
    rows = []
    for i, x in enumerate(rec):
        t = _utc(x["created_at"])
        prev = [p for p in rec[:i] if t - _utc(p["created_at"]) <= WINDOW_DAYS * 86400]
        if len(prev) < 10:
            continue
        rh, hv = [p["rhr"] for p in prev], [math.log(p["hrv"]) for p in prev]
        zr = (x["rhr"] - _median(rh)) / _spread(rh, *RHR_SPREAD)
        zh = (math.log(x["hrv"]) - _median(hv)) / _spread(hv, *HRV_SPREAD)
        rows.append((x["r"], zr, zh))
    if len(rows) < MIN_FIT_DAYS:
        return None
    y = [r[0] for r in rows]
    both = _least_squares([(1, r[1], r[2]) for r in rows], y)
    rhr = _least_squares([(1, r[1]) for r in rows], y)
    if not both or not rhr:
        return None
    cut = int(len(rows) * 0.7)
    train, test = rows[:cut], rows[cut:]
    held = _least_squares([(1, r[1], r[2]) for r in train], [r[0] for r in train]) or both
    held_r = _least_squares([(1, r[1]) for r in train], [r[0] for r in train]) or rhr
    clip = lambda v: min(99.0, max(1.0, v))
    pred = [clip(held[0] + held[1] * r[1] + held[2] * r[2]) for r in test]
    pred_r = [clip(held_r[0] + held_r[1] * r[1]) for r in test]
    truth = [r[0] for r in test]
    n = len(test)
    return {"both": [round(v, 3) for v in both], "rhr": [round(v, 3) for v in rhr], "days": len(rows),
            "source": "archive", "tested_on": n,
            "error": round(sum(abs(p - t) for p, t in zip(pred, truth)) / n, 1),
            "zone_match": round(sum(zone(p) == zone(t) for p, t in zip(pred, truth)) / n, 2),
            "error_rhr_only": round(sum(abs(p - t) for p, t in zip(pred_r, truth)) / n, 1),
            "zone_match_rhr_only": round(sum(zone(p) == zone(t) for p, t in zip(pred_r, truth)) / n, 2)}


def model(c):
    """The recovery model: fitted on your archive if it is big enough, a general default if not."""
    rec = _archive(c)
    return _cached(c, "learn_model", len(rec), lambda: _fit(rec)) or DEFAULT_MODEL


def then(c):
    """Your WHOOP-era numbers (last 60 scored days): for comparison and as starting spreads."""
    rec = _archive(c)[-60:]
    if len(rec) < 10:
        return None
    rh, hv = [r["rhr"] for r in rec], [math.log(r["hrv"]) for r in rec]
    return {"rhr": _median(rh), "hrv": math.exp(_median(hv)), "year": rec[-1]["created_at"][:4],
            "end": rec[-1]["created_at"][:10], "rhr_spread": _spread(rh, *RHR_SPREAD),
            "hrv_spread": _spread(hv, *HRV_SPREAD)}


def night_window(c):
    """(start, end) of your night in hours from midnight, from when you fell asleep and woke on
    your WHOOP nights: a little before your early bedtimes to your usual late wake-ups."""
    def build():
        bed, wake = [], []
        for s in c.execute("SELECT start, end, raw FROM api_sleep WHERE COALESCE(nap,0)=0 AND start IS NOT NULL "
                           "AND end IS NOT NULL ORDER BY start DESC LIMIT 120"):
            try:
                off = json.loads(s["raw"] or "{}").get("timezone_offset") or "+00:00"
                sec = (1 if off[0] == "+" else -1) * (int(off[1:3]) * 3600 + int(off[4:6]) * 60)
            except (ValueError, IndexError):
                continue
            b = ((_utc(s["start"]) + sec) % 86400) / 3600
            bed.append(b - 24 if b > 12 else b)
            wake.append(((_utc(s["end"]) + sec) % 86400) / 3600)
        if len(bed) < 30:
            return list(metrics.NIGHT)
        bed.sort(); wake.sort()
        start = bed[len(bed) // 10] - 0.5                      # half an hour before your early nights
        end = wake[len(wake) * 3 // 4]                         # covers three wake-ups in four
        return [round(max(-4.0, min(0.0, start)), 2), round(max(5.0, min(10.0, end)), 2)]
    n = c.execute("SELECT COUNT(*) n FROM api_sleep").fetchone()["n"]
    return tuple(_cached(c, "learn_night", n, build))


def max_hr(c):
    """Your max heart rate: WHOOP's figure for you, else the top of your recorded workouts."""
    r = c.execute("SELECT value FROM api_profile WHERE key='max_heart_rate'").fetchone()
    try:
        if r and float(r["value"]) > 120:
            return float(r["value"])
    except ValueError:
        pass
    tops = [x["max_hr"] for x in c.execute("SELECT max_hr FROM api_workout WHERE max_hr > 0 ORDER BY max_hr DESC")]
    return tops[len(tops) // 20] if len(tops) >= 20 else None   # 95th percentile: one spike isn't your max


# ---------------- your usual ----------------
def usual(c, today):
    """Your usual night, from nights before `today` (YYYY-MM-DD).

    source: "own" (strap nights), "archive" (WHOOP scores under 60 days old, until enough own
    nights exist) or None. "then" always carries your WHOOP-era numbers when there are any.
    """
    t = then(c)
    spreads = {"rhr_spread": t["rhr_spread"] if t else RHR_SPREAD[1],
               "hrv_spread": t["hrv_spread"] if t else HRV_SPREAD[1]}      # yours, until the strap knows better
    day0 = time.mktime(time.strptime(today, "%Y-%m-%d"))
    own = c.execute("SELECT rhr, hrv FROM daily_night WHERE date < ? AND date >= ? AND rhr IS NOT NULL "
                    "ORDER BY date DESC", (today, _date(day0 - WINDOW_DAYS * 86400))).fetchall()
    if len(own) >= MIN_NIGHTS:
        rhr = [r["rhr"] for r in own]
        hrv = [r["hrv"] for r in own if r["hrv"]]
        u = {"rhr": _median(rhr), "hrv": _median(hrv), "nights": len(own), "source": "own", "then": t}
        if len(rhr) >= 5:
            spreads["rhr_spread"] = _spread(rhr, *RHR_SPREAD)
        if len(hrv) >= 5:
            spreads["hrv_spread"] = _spread([math.log(x) for x in hrv], *HRV_SPREAD)
        return dict(u, **spreads)
    recent = t and day0 - _utc(t["end"] + "T00:00:00") <= ARCHIVE_MAX_AGE_DAYS * 86400
    if recent:
        return dict({"rhr": t["rhr"], "hrv": t["hrv"], "nights": len(own), "source": "archive", "then": t}, **spreads)
    return dict({"rhr": None, "hrv": None, "nights": len(own), "source": None, "then": t}, **spreads)


def high_hr(c, today=None):
    """The heart rate that counts as "up" for you: halfway from your resting rate to your max
    (50% of heart rate reserve, the usual line between easy and moderate effort)."""
    today = today or _date(time.time())
    top = max_hr(c)
    if not top:
        return metrics.HIGH_HR
    own = [r["rhr"] for r in c.execute("SELECT rhr FROM daily_night WHERE date < ? AND rhr IS NOT NULL "
                                       "ORDER BY date DESC LIMIT 30", (today,))]
    t = then(c)
    rest = _median(own) if len(own) >= MIN_NIGHTS else t["rhr"] if t else None
    if not rest:
        return metrics.HIGH_HR
    return int(round(min(150, max(105, rest + 0.5 * (top - rest)))))


# ---------------- the recovery estimate and the call ----------------
def recovery(night, u, m=None):
    """Estimated recovery % for last night, or None when there is nothing to compare with.

    Uses HRV and resting HR when the night has enough clean beats for its HRV to be believed,
    resting HR alone otherwise (a weaker read, and reported as such).
    Returns (percent, "hrv+rhr" | "rhr").
    """
    if not night or night.get("suspect") or not u.get("rhr"):
        return None
    m = m or DEFAULT_MODEL
    zr = (night["resting"] - u["rhr"]) / u.get("rhr_spread", RHR_SPREAD[1])
    if night.get("rmssd") and u.get("hrv") and night.get("n_rr", MIN_RR) >= MIN_RR:
        zh = math.log(night["rmssd"] / u["hrv"]) / u.get("hrv_spread", HRV_SPREAD[1])
        a, b, w = m["both"]
        est, basis = a + b * zr + w * zh, "hrv+rhr"
    else:
        a, b = m["rhr"]
        est, basis = a + b * zr, "rhr"
    return min(99.0, max(1.0, est)), basis


def thresholds(c, before=None):
    """Where green starts and red ends (recovery %), after replaying your feedback in date order.

    Replayed from scratch each time (not stored), so changing a day's answer changes the result
    and the same history always gives the same zones.
    """
    g, r, used = GREEN_MIN, RED_MAX, 0
    q = "SELECT date, score, feel FROM daily_night WHERE feel IS NOT NULL AND score IS NOT NULL"
    for row in c.execute(q + (" AND date < ?" if before else "") + " ORDER BY date", (before,) if before else ()).fetchall():
        call, feel = zone(row["score"], g, r), row["feel"]
        if feel == 3:                       # felt good: it was too cautious
            if call == "red": r -= STEP
            elif call == "yellow": g -= STEP
        elif feel == 1:                     # felt rough: it was not cautious enough
            if call == "green": g += STEP
            elif call == "yellow": r += STEP
        elif feel == 2:                     # felt ok: the extremes were overstated
            if call == "red": r -= STEP / 2
            elif call == "green": g += STEP / 2
        g = min(85.0, max(50.0, g))
        r = min(g - 15.0, max(15.0, r))
        used += 1
    return {"green_min": round(g, 1), "red_max": round(r, 1), "feedback": used}


def call_for(night, u, th=None, m=None):
    """green / yellow / red for last night, or none (no data) / new (nothing to compare with yet)
    / suspect (the reading itself looks wrong)."""
    if not night:
        return "none"
    if night.get("suspect"):
        return "suspect"
    est = recovery(night, u, m)
    if est is None:
        return "new"
    th = th or {"green_min": GREEN_MIN, "red_max": RED_MAX}
    return zone(est[0], th["green_min"], th["red_max"])


def trend_line(u):
    """Resting HR (bpm) that counts as clearly above your usual: 4 bpm or 1.5x your normal
    night-to-night variation over it, whichever is more."""
    return u["rhr"] + max(4.0, 1.5 * u["rhr_spread"])


def trend(c, today, u):
    """Mornings in a row (ending today) with resting HR clearly above your usual, if 3 or more.
    A single high night is noise; several in a row is the pattern worth acting on."""
    if not u.get("rhr"):
        return 0
    line = trend_line(u)
    run, expect = 0, time.mktime(time.strptime(today, "%Y-%m-%d"))
    for r in c.execute("SELECT date, rhr FROM daily_night WHERE date <= ? AND rhr IS NOT NULL ORDER BY date DESC LIMIT 14", (today,)):
        if r["date"] != _date(expect + 43200) or r["rhr"] < line:
            break
        run += 1
        expect -= 86400
    return run if run >= 3 else 0


# ---------------- remembering each day ----------------
def remember(c, today, night, yesterday, u, th, m=None):
    """Store this morning's numbers, estimate and call. Keeps feedback already given for the day."""
    if not night or night.get("suspect"):
        return None
    est = recovery(night, u, m)
    call = call_for(night, u, th, m)
    c.execute("INSERT INTO daily_night(date,rhr,hrv,n_rr,load_s,peak,recorded_s,score,call) VALUES(?,?,?,?,?,?,?,?,?) "
              "ON CONFLICT(date) DO UPDATE SET rhr=excluded.rhr, hrv=excluded.hrv, n_rr=excluded.n_rr, "
              "load_s=excluded.load_s, peak=excluded.peak, recorded_s=excluded.recorded_s, score=excluded.score, "
              "call=excluded.call",
              (today, night["resting"], night["rmssd"], night.get("n_rr"),
               yesterday["high_s"] if yesterday else None, yesterday["max"] if yesterday else None,
               yesterday["recorded_s"] if yesterday else None, est[0] if est else None, call))
    c.commit()
    return est


def record_feel(c, word, now=None):
    """Store how you feel today. Returns (date, call that was made or None, zones before, after)."""
    today = _date(now or time.time())
    before = thresholds(c)
    c.execute("INSERT INTO daily_night(date, feel, feel_ts) VALUES(?,?,?) "
              "ON CONFLICT(date) DO UPDATE SET feel=excluded.feel, feel_ts=excluded.feel_ts",
              (today, FEEL[word], now or time.time()))
    c.commit()
    row = c.execute("SELECT call FROM daily_night WHERE date=?", (today,)).fetchone()
    return today, row["call"], before, thresholds(c)


# ---------------- what it knows ----------------
def summary(c, today=None):
    """Everything learned so far, as a dict (the dashboard and `python learn.py` both use it)."""
    today = today or _date(time.time())
    u, th, m = usual(c, today), thresholds(c), model(c)
    rows = c.execute("SELECT date, rhr, hrv, load_s, call, feel, score FROM daily_night "
                     "WHERE rhr IS NOT NULL ORDER BY date").fetchall()
    rated = [r for r in rows if r["feel"] and r["call"] in ("green", "yellow", "red")]
    agree = sum(1 for r in rated if (r["call"], r["feel"]) in (("green", 3), ("yellow", 2), ("red", 1)))
    after_big, after_other = [], []          # does a hard day show up in the night after it?
    if u["rhr"]:
        for r in rows:
            if r["load_s"] is not None:
                (after_big if r["load_s"] >= 30 * 60 else after_other).append(r["rhr"] - u["rhr"])
    effect = None
    if len(after_big) >= 3 and len(after_other) >= 3:
        effect = sum(after_big) / len(after_big) - sum(after_other) / len(after_other)
    return {"usual": u, "thresholds": th, "model": m, "high_hr": high_hr(c, today), "max_hr": max_hr(c),
            "night_window": list(night_window(c)), "nights": len(rows), "rated": len(rated), "agree": agree,
            "big_day_effect": effect, "big_days": len(after_big), "trend": trend(c, today, u)}


def _clock(h):
    h %= 24
    return f"{int(h):02d}:{int(round(h % 1 * 60)) % 60:02d}"


def learned_text(c, today=None):
    s = summary(c, today)
    u, th, m, t = s["usual"], s["thresholds"], s["model"], s["usual"]["then"]
    lines = []
    if u["source"] == "own":
        line = f"Your usual night now: resting HR {u['rhr']:.0f}"
        if u["hrv"]:
            line += f", HRV {u['hrv']:.0f} ms"
        line += f" (your last {u['nights']} strap nights)."
        if t and abs(u["rhr"] - t["rhr"]) >= 3:
            d = u["rhr"] - t["rhr"]
            line += (f" Your resting HR is {abs(d):.0f} bpm {'higher' if d > 0 else 'lower'} than in {t['year']} "
                     f"({t['rhr']:.0f} then)" + (", which is what less training does." if d > 0 else "."))
        lines.append(line)
    elif u["source"] == "archive":
        lines.append(f"Your usual, from recent WHOOP scores until the strap has {MIN_NIGHTS} nights: "
                     f"resting HR {u['rhr']:.0f}, HRV {u['hrv']:.0f} ms.")
    else:
        line = f"Still learning your usual now: {u['nights']} of {MIN_NIGHTS} strap nights."
        if t:
            line += (f" In {t['year']} WHOOP had you at resting HR {t['rhr']:.0f} and HRV {t['hrv']:.0f} ms; "
                     "I show that for comparison but don't judge you against it.")
        lines.append(line)
    if m["source"] == "archive":
        lines.append(f"Recovery estimate: modelled on {m['days']} days WHOOP scored for you. Checked on {m['tested_on']} "
                     f"days it hadn't seen, it landed in WHOOP's zone {m['zone_match'] * 100:.0f}% of the time, "
                     f"typically {m['error']:.0f} points off. For you HRV moves recovery far more than resting HR, "
                     f"so on a night without a clean HRV reading it is weaker ({m['zone_match_rhr_only'] * 100:.0f}%).")
    else:
        lines.append("Recovery estimate: a general rule (no WHOOP history to model it on).")
    a, b = s["night_window"]
    lines.append(f"Your night: I read {_clock(a)} to {_clock(b)}"
                 + (", from when you actually slept on WHOOP." if (a, b) != metrics.NIGHT else "."))
    lines.append(f"Heart rate counts as up for you from {s['high_hr']} bpm"
                 + (f" (halfway to your max of {s['max_hr']:.0f})." if s["max_hr"] else " (a general default)."))
    if s["big_day_effect"] is not None:
        e = s["big_day_effect"]
        lines.append(f"After your big days your resting HR runs {abs(e):.0f} bpm {'higher' if e > 0 else 'lower'} "
                     f"than after other days ({s['big_days']} big days so far)." if abs(e) >= 1 else
                     f"Big days haven't moved your resting HR the night after ({s['big_days']} so far).")
    if th["feedback"]:
        drift = (th["green_min"] - GREEN_MIN) + (th["red_max"] - RED_MAX)
        lean = "less cautious than WHOOP's zones" if drift < -0.5 else "more cautious than WHOOP's zones" if drift > 0.5 else "on WHOOP's zones"
        lines.append(f"You've told me how you felt on {th['feedback']} day{'s' if th['feedback'] != 1 else ''}; "
                     f"my call matched on {s['agree']}. I'm now {lean} (green from {th['green_min']:.0f}%, red under {th['red_max']:.0f}%).")
    else:
        lines.append("Tell me how you feel (good, ok or rough) and I'll move the zones to fit you.")
    return "\n".join("• " + l for l in lines)


if __name__ == "__main__":
    c = db.connect()
    args = sys.argv[1:]
    if args[:1] == ["feel"] and len(args) > 1 and args[1] in FEEL:
        date, call, _, th = record_feel(c, args[1])
        print(f"{date}: felt {args[1]}" + (f" (the call was {call})" if call else ""), "| zones now", th)
    else:
        print(learned_text(c))
