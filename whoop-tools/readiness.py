#!/usr/bin/env python3
"""Readiness: one plain read of today ("Primed", "Balanced", "Strained", "Run down") and the
signals behind it, from numbers the system already has.

  hrv        last night's HRV against your usual (learn.usual: median of your last 30 nights),
             compared on the log scale in units of your own night-to-night variation, so a
             drop means the same thing for a low-HRV and a high-HRV person (Plews 2013,
             Buchheit 2014). Only nights with enough clean beats (learn.MIN_RR).
  rhr        last night's resting HR against your usual, in units of your variation (Lamberts 2004).
  resp       breathing rate during sleep (scores.py sleep estimate) against the median of your
             earlier nights; only a rise counts, and it needs a bigger move (1.5x / 2x your
             variation) because one night's estimate is noisy.
  load       acute:chronic workload: mean Strain of the last 7 days over the last 28 (Gabbett
             2016). 0.8-1.3 is the usual "sweet spot"; above 1.5 is a spike. Only complete days
             whose Strain estimate passed its checks; needs 14 of them.
  (no monotony) NOOP also flags Foster's training monotony (mean / SD of a week's load, 2+).
             On Strain it fires nearly every week: Strain never drops to 0 on rest days, so a
             normal week (8-13) scores ~6. It would only be noise here, so it is left out.
  alerts     an alert that is on (alerts.py: 3+ nights in a row past your line) counts as a
             red flag for its signal, since it is stronger evidence than one night.

Each signal gets a flag: good, neutral, watch or bad. The headline:
  Run down   two red flags, or a recovery signal red while load is spiking
  Strained   one red flag
  Primed     two or more good signals and nothing to watch
  Balanced   anything else
  Learning   fewer than 7 nights of your own to compare with and no load history yet

The thresholds are the published ones NOOP (github.com/ryanbr/noop) also uses; the baselines
are ours. Strain is a 0-21 log-like scale, so the load ratio on it is softer than on raw
training load. This is pattern-matching on your own numbers, not medical advice.

    python readiness.py [YYYY-MM-DD]
"""
import json, math, statistics, sys, time
import db, learn

VERSION = "r1"
MIN_BASELINE = 7          # own nights before hrv / rhr / resp are compared at all
ACUTE, CHRONIC, MIN_CHRONIC = 7, 28, 14
RESP_WATCH, RESP_BAD = 1.5, 2.0
RESP_SPREAD_FLOOR = 0.3   # breaths/min


def _shift(date, days):
    return time.strftime("%Y-%m-%d", time.localtime(time.mktime(time.strptime(date, "%Y-%m-%d")) + days * 86400 + 43200))


def _flag(z):
    """z oriented so positive is better."""
    return "good" if z >= 0.5 else "neutral" if z > -0.5 else "watch" if z > -1.0 else "bad"


TEXT = {
    "hrv": {"good": "above your usual: well recovered", "neutral": "in your normal range",
            "watch": "a little below your usual", "bad": "well below your usual: your body is still working"},
    "rhr": {"good": "at or below your usual", "neutral": "in your normal range",
            "watch": "a little high", "bad": "clearly above your usual: hard training, illness or a late night can do this"},
}


def _signal(key, label, value, usual, unit, flag, detail, **extra):
    return dict({"key": key, "label": label, "value": value, "usual": usual, "unit": unit,
                 "flag": flag, "detail": detail}, **extra)


def _estimates(c, kind, start, end):
    if not c.execute("SELECT 1 FROM sqlite_master WHERE name='estimates'").fetchone():
        return {}
    out = {}
    for r in c.execute("SELECT date, value, row FROM estimates WHERE kind=? AND ok=1 AND date>=? AND date<=? "
                       "ORDER BY date, computed_at", (kind, start, end)):
        out[r["date"]] = r
    return out


def night_signals(c, today, u):
    sig = []
    night = c.execute("SELECT rhr, hrv, n_rr FROM daily_night WHERE date=?", (today,)).fetchone()
    own = u.get("source") == "own" and u.get("nights", 0) >= MIN_BASELINE
    if night and own:
        if night["hrv"] and u.get("hrv") and (night["n_rr"] is None or night["n_rr"] >= learn.MIN_RR):
            z = math.log(night["hrv"] / u["hrv"]) / u["hrv_spread"]
            f = _flag(z)
            sig.append(_signal("hrv", "HRV", round(night["hrv"]), round(u["hrv"]), "ms", f, TEXT["hrv"][f], z=round(z, 2)))
        if night["rhr"] and u.get("rhr"):
            z = (u["rhr"] - night["rhr"]) / u["rhr_spread"]
            f = _flag(z)
            sig.append(_signal("rhr", "Resting HR", round(night["rhr"]), round(u["rhr"]), "bpm", f, TEXT["rhr"][f], z=round(z, 2)))
    return sig, night


def resp_signal(c, today):
    rows = _estimates(c, "sleep", _shift(today, -learn.WINDOW_DAYS), today)
    vals = {}
    for d, r in rows.items():
        try:
            v = json.loads(r["row"] or "{}").get("resp_rate")
        except ValueError:
            v = None
        if v and 4 <= v <= 40:
            vals[d] = v
    if today not in vals:
        return None
    before = [v for d, v in vals.items() if d < today]
    if len(before) < MIN_BASELINE:
        return None
    usual = statistics.median(before)
    spread = max(RESP_SPREAD_FLOOR, 1.4826 * statistics.median([abs(v - usual) for v in before]))
    z = (vals[today] - usual) / spread
    if z >= RESP_BAD:
        f, t = "bad", "up on your usual: sometimes an early sign of getting ill"
    elif z >= RESP_WATCH:
        f, t = "watch", "a little above your usual"
    else:
        f, t = "neutral", "in your normal range"
    return _signal("resp", "Breathing rate", round(vals[today], 1), round(usual, 1), "/min", f, t, z=round(z, 2))


def load_signals(c, today):
    """Strain of complete days before today (today's isn't over)."""
    days = _estimates(c, "strain", _shift(today, -CHRONIC), _shift(today, -1))
    strain = {d: r["value"] for d, r in days.items() if r["value"] is not None}
    out, acwr = [], None
    if len(strain) >= MIN_CHRONIC:
        acute = [v for d, v in strain.items() if d >= _shift(today, -ACUTE)]
        chronic = list(strain.values())
        if acute and statistics.mean(chronic) > 0:
            a, ch = statistics.mean(acute), statistics.mean(chronic)
            acwr = a / ch
            if acwr < 0.8:
                f, t = "watch", "lighter than your recent weeks: room to build"
            elif acwr < 1.3:
                f, t = "good", "in the sweet spot for your recent weeks"
            elif acwr < 1.5:
                f, t = "watch", "building fast: watch for fatigue"
            else:
                f, t = "bad", "a spike on your recent weeks: higher injury risk"
            out.append(_signal("load", "Training load", round(acwr, 2), 1.0, "acute:chronic", f, t,
                               acute=round(a, 1), chronic=round(ch, 1)))
    return out, acwr


def synthesize(signals, have_baseline):
    if not signals or not have_baseline:
        return "learning", "Learning", "A few more nights of your own and readiness will have something to compare with."
    flags = [s["flag"] for s in signals]
    bad = flags.count("bad")
    recovery_down = any(s["key"] in ("hrv", "rhr", "resp") and s["flag"] == "bad" for s in signals)
    load_high = any(s["key"] == "load" and s["flag"] == "bad" for s in signals)
    if bad >= 2 or (recovery_down and load_high):
        return "rundown", "Run down", "Several signals are down at once. Make today a recovery day and protect tonight's sleep."
    if bad:
        return "strained", "Strained", "One signal is flagging. Training is fine if you keep it controlled."
    if flags.count("good") >= 2 and "watch" not in flags:
        return "primed", "Primed", "Your signals line up and your load is supported. A harder session is well backed today."
    return "balanced", "Balanced", "Nothing is flagging. Train by feel."


def evaluate(c, today=None):
    today = today or time.strftime("%Y-%m-%d")
    u = learn.usual(c, today)
    signals, night = night_signals(c, today, u)
    r = resp_signal(c, today)
    if r:
        signals.append(r)
    load, acwr = load_signals(c, today)
    signals += load
    try:
        import alerts
        on = alerts.active(c, today)
    except Exception:          # alerts are a bonus here, never a reason for no read
        on = []
    by_alert = {"hrv_low": "hrv", "rhr_up": "rhr", "resp_up": "resp"}
    for a in on:
        key = by_alert.get(a.get("key"))
        for s in signals:
            if s["key"] == key and s["flag"] != "bad":
                s["flag"], s["detail"] = "bad", a.get("text") or s["detail"]
    have = (u.get("source") == "own" and u.get("nights", 0) >= MIN_BASELINE) or acwr is not None
    level, headline, summary = synthesize(signals, have)
    return {"date": today, "version": VERSION, "level": level, "headline": headline, "summary": summary,
            "signals": signals, "acwr": round(acwr, 2) if acwr else None,
            "baseline_nights": u.get("nights", 0), "needs_nights": MIN_BASELINE,
            "alerts": [a.get("key") for a in on]}


if __name__ == "__main__":
    r = evaluate(db.connect(), sys.argv[1] if len(sys.argv) > 1 else None)
    print(f"{r['date']}  {r['headline']}: {r['summary']}")
    for s in r["signals"]:
        print(f"  {s['flag']:>7}  {s['label']}: {s['value']} {s['unit']} (usual {s['usual']}) - {s['detail']}")
    if r["level"] == "learning":
        print(f"  own nights so far: {r['baseline_nights']} of {r['needs_nights']}")
