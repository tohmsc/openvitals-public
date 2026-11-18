#!/usr/bin/env python3
"""The numbers computed from the strap's heart rate (ble_hr). One implementation, used by
the dashboard's Today view, the Telegram messages and learn.py, so they always agree.
"""
import math, time

HIGH_HR = 120            # "heart rate up" until learn.py knows your own range
RR_MIN, RR_MAX = 280, 2000
RR_TOLERANCE = 0.30      # a beat interval must be within 30% of what the heart rate implies
RR_JUMP = 220            # successive intervals further apart than this are a missed/extra beat


def parse_rr(v, hr=None):
    """Beat intervals (ms) from a stored rr_ms string, without the ones that can't be real.

    The strap's beat detection gets unreliable when you move: it then reports intervals
    that don't fit the heart rate it reports in the same second (a missed beat doubles the
    interval, noise halves it). Those are dropped here, or HRV would be measuring noise.
    """
    out = []
    for x in str(v or "").replace("[", " ").replace("]", " ").replace(",", " ").split():
        try:
            x = float(x)
        except ValueError:
            continue
        if not RR_MIN < x < RR_MAX:
            continue
        if hr and abs(x - 60000 / hr) > RR_TOLERANCE * 60000 / hr:
            continue
        out.append(x)
    return out


NIGHT = (-2.0, 6.0)      # 22:00 to 06:00, in hours from midnight, until learn.py knows your hours
ASLEEP_MARGIN = 10       # bpm above the night's resting HR that still counts as calm enough for HRV
RHR_PLAUSIBLE = (30, 100)
HRV_PLAUSIBLE = (8, 250)


def day_stats(c, start, end, high_hr=HIGH_HR, rr_max_hr=None):
    """Strap stats for [start, end), or None if nothing was recorded.
    With rr_max_hr, HRV only uses beats from seconds at or below that heart rate."""
    rows = c.execute("SELECT ts,hr,rr_ms FROM ble_hr WHERE ts >= ? AND ts < ? AND hr > 0 ORDER BY ts",
                     (start, end)).fetchall()
    if not rows:
        return None
    active, high, diffs, prev_rr = 0.0, 0.0, [], None
    for i, r in enumerate(rows):
        if i:
            gap = r["ts"] - rows[i - 1]["ts"]
            if gap < 60:
                active += gap
                if r["hr"] >= high_hr:      # exercise or real exertion
                    high += gap
        if rr_max_hr is not None and r["hr"] > rr_max_hr:
            prev_rr = None
            continue
        for x in parse_rr(r["rr_ms"], r["hr"]):
            # successive beats: within 20% of each other (the usual ectopic-beat rule) and under RR_JUMP
            if prev_rr and r["ts"] - prev_rr[1] < 3.5 and abs(x - prev_rr[0]) < min(RR_JUMP, 0.2 * prev_rr[0]):
                diffs.append(x - prev_rr[0])
            prev_rr = (x, r["ts"])
    hrs = [r["hr"] for r in rows]
    # resting heart rate: the calmest 5 minutes (needs most of the window recorded)
    resting, lo, total = None, 0, 0
    for hi, r in enumerate(rows):
        total += r["hr"]
        while r["ts"] - rows[lo]["ts"] > 300:
            total -= rows[lo]["hr"]; lo += 1
        n = hi - lo + 1
        if n >= 200 and r["ts"] - rows[lo]["ts"] >= 240:
            avg = total / n
            resting = avg if resting is None else min(resting, avg)
    return {"first": rows[0]["ts"], "last": rows[-1]["ts"], "recorded_s": active, "high_s": high,
            "avg": sum(hrs) / len(hrs), "min": min(hrs), "max": max(hrs), "resting": resting,
            "rmssd": math.sqrt(sum(d * d for d in diffs) / len(diffs)) if len(diffs) >= 30 else None,
            "n_rr": len(diffs), "samples": len(rows)}


def night_stats(c, morning, window=NIGHT):
    """Last night, if at least 90 minutes were recorded. `morning` is local midnight of the day
    you woke up; `window` is (start, end) in hours from that midnight (learn.night_window).

    HRV is taken only from the calm part of the night (heart rate within ASLEEP_MARGIN of the
    night's resting rate): lying awake, getting up and the morning would otherwise pull it
    around, and it is the sleeping value that says something about recovery. A resting HR or
    HRV outside what a human heart does is treated as a bad reading ("suspect"), not as news.
    """
    a, b = morning + window[0] * 3600, morning + window[1] * 3600
    s = day_stats(c, a, b)
    if not s or s["recorded_s"] < 90 * 60 or s["resting"] is None:
        return None
    calm = day_stats(c, a, b, rr_max_hr=s["resting"] + ASLEEP_MARGIN)
    s["rmssd"], s["n_rr"] = calm["rmssd"], calm["n_rr"]
    s["suspect"] = not RHR_PLAUSIBLE[0] <= s["resting"] <= RHR_PLAUSIBLE[1]
    if s["rmssd"] is not None and not HRV_PLAUSIBLE[0] <= s["rmssd"] <= HRV_PLAUSIBLE[1]:
        s["rmssd"] = None                # keep the resting HR, drop the impossible HRV
    return s


def day_bounds(offset_days=0, now=None):
    """Local midnight to midnight for today (0) or yesterday (-1)."""
    lt = time.localtime(now or time.time())
    start = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday + offset_days, 0, 0, 0, 0, 0, -1))
    end = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday + offset_days + 1, 0, 0, 0, 0, 0, -1))
    return start, end


def battery(c):
    """Latest battery %, or None if unknown or more than a day old.

    decode.py keeps the newest reading in sync_state: device_soc is keyed by the firmware's
    tick counter, which restarts when the strap reboots, so its "last row" can be an old one.
    """
    import json, db
    try:
        now = json.loads(db.get_state(c, "battery_now") or "null")
    except ValueError:
        now = None
    if now:
        return now["soc"] if time.time() - now["ts"] < 86400 else None
    try:                                    # before the first decode pass wrote battery_now
        r = c.execute("SELECT soc_pct FROM device_soc ORDER BY seen DESC, rtc DESC LIMIT 1").fetchone()
    except Exception:
        return None
    return r["soc_pct"] if r else None
