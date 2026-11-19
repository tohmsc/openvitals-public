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


def beat_diffs(rows, rr_max_hr=None):
    """Differences between successive clean beat intervals (ms) in rows of (ts, hr, rr_ms),
    oldest first. With rr_max_hr, seconds above that heart rate are skipped."""
    diffs, prev_rr = [], None
    for r in rows:
        if rr_max_hr is not None and r["hr"] > rr_max_hr:
            prev_rr = None
            continue
        for x in parse_rr(r["rr_ms"], r["hr"]):
            # successive beats: within 20% of each other (the usual ectopic-beat rule) and under RR_JUMP
            if prev_rr and r["ts"] - prev_rr[1] < 3.5 and abs(x - prev_rr[0]) < min(RR_JUMP, 0.2 * prev_rr[0]):
                diffs.append(x - prev_rr[0])
            prev_rr = (x, r["ts"])
    return diffs


def rmssd(diffs, min_pairs=30):
    """Root mean square of successive differences, or None with fewer than min_pairs."""
    return math.sqrt(sum(d * d for d in diffs) / len(diffs)) if len(diffs) >= min_pairs else None


def day_stats(c, start, end, high_hr=HIGH_HR, rr_max_hr=None):
    """Strap stats for [start, end), or None if nothing was recorded.
    With rr_max_hr, HRV only uses beats from seconds at or below that heart rate."""
    rows = c.execute("SELECT ts,hr,rr_ms FROM ble_hr WHERE ts >= ? AND ts < ? AND hr > 0 ORDER BY ts",
                     (start, end)).fetchall()
    if not rows:
        return None
    active, high = 0.0, 0.0
    for i, r in enumerate(rows):
        if i:
            gap = r["ts"] - rows[i - 1]["ts"]
            if gap < 60:
                active += gap
                if r["hr"] >= high_hr:      # exercise or real exertion
                    high += gap
    diffs = beat_diffs(rows, rr_max_hr)
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
            "rmssd": rmssd(diffs),
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


# ---------------- breathing rate from beat intervals ----------------
# Heart rate rises a little on each breath in and falls on each breath out (respiratory sinus
# arrhythmia), so the beat-interval series wobbles at the breathing rate. Per window: rebuild
# beat times, resample the intervals evenly, find the strongest frequency in the breathing
# band; over the night: median of the windows that have a clear peak. How the numbers below
# were chosen and checked: fit/resp_rate/ (scores.RESP_RATE holds the frozen copy).
RESP = {
    "window_s": 120,         # one estimate per 2 minutes of beats ...
    "step_s": 60,            # ... every minute
    "fs": 2.0,               # resampling rate (Hz); breathing is far below 1 Hz
    "diff": True,            # spectrum of the sample-to-sample change (damps slow HR swings)
    "band": (0.14, 0.4),     # 8.4-24 breaths/min
    "df": 0.004,             # frequency step of the scan (Hz), refined by interpolation
    "min_cover": 0.8,        # share of the window that must be covered by clean beats
    "min_peak": 0.25,        # peak's share of the band's power: below it there is no clear rhythm
    "calm_margin": 20,       # only seconds within this many bpm of the night's resting HR
    "min_windows": 10,       # fewer clean windows than this: no night value
}


def beat_series(rows):
    """[(time_s, interval_ms)] from ble_hr rows (ts, hr, rr_ms), clean beats only.

    Each row carries the intervals that ended in that second, so beat times are rebuilt by
    adding intervals up, and re-anchored to the row's time when they drift (a beat the strap
    missed or one parse_rr dropped would otherwise squeeze the time axis)."""
    out, t, prev = [], None, None
    for ts, hr, rr in rows:
        xs = parse_rr(rr, hr)
        if not xs:
            continue
        span = sum(xs) / 1000.0
        if t is None or abs(t + span - (ts - 0.5)) > 1.0:
            t, prev = ts - 0.5 - span, None
        for x in xs:
            t += x / 1000.0
            # the usual ectopic-beat rule: within 20% of the beat before
            if prev is None or abs(x - prev) < min(RR_JUMP, 0.2 * prev):
                out.append((t, x))
            prev = x
    return out


def breath_spectrum(beats, a, p=RESP):
    """(frequencies, power) of the beat intervals in [a, a + window) over p["band"], or None if
    clean beats cover too little of the window."""
    w = p["window_s"]
    seg = [(t, x) for t, x in beats if a <= t < a + w]
    if len(seg) < 3 or sum(x for _, x in seg) / 1000.0 < p["min_cover"] * w:
        return None
    # resample at fs by linear interpolation (holding the ends), then remove the linear trend
    fs, n = p["fs"], int(w * p["fs"])
    ys, j = [], 0
    for k in range(n):
        t = a + k / fs
        while j < len(seg) - 2 and seg[j + 1][0] < t:
            j += 1
        (t0, y0), (t1, y1) = seg[j], seg[j + 1]
        if t <= t0:
            ys.append(y0)
        elif t >= t1:
            ys.append(y1)
        else:
            ys.append(y0 + (y1 - y0) * (t - t0) / (t1 - t0))
    if p.get("diff"):
        # change from sample to sample: damps the slow swings of heart rate (which would
        # otherwise outweigh breathing) and leaves the breathing rhythm's frequency as it is
        ys = [y1 - y0 for y0, y1 in zip(ys, ys[1:])]
        n = len(ys)
    km = (n - 1) / 2.0
    my = sum(ys) / n
    sxx = sum((k - km) ** 2 for k in range(n))
    slope = sum((k - km) * (y - my) for k, y in enumerate(ys)) / sxx
    hann = [0.5 - 0.5 * math.cos(2 * math.pi * k / (n - 1)) for k in range(n)]
    ys = [(y - my - slope * (k - km)) * h for k, (y, h) in enumerate(zip(ys, hann))]
    # power at each frequency of the band (a direct DFT: a few dozen frequencies only)
    lo, hi = p["band"]
    freqs = [lo + i * p["df"] for i in range(int((hi - lo) / p["df"]) + 1)]
    power = []
    for f in freqs:
        z, acc = complex(math.cos(2 * math.pi * f / fs), math.sin(2 * math.pi * f / fs)), 0j
        for y in ys:                            # Horner: |sum y_k z^k|, same power as the DFT
            acc = acc * z + y
        power.append(acc.real * acc.real + acc.imag * acc.imag)
    return freqs, power


def breath_peak(freqs, power, df):
    """(breaths/min, the peak's share of the band's power) from breath_spectrum's output, or None
    when the strongest point is the band's edge: that is slower (or faster) power spilling in,
    not a breathing rhythm."""
    total = sum(power)
    if total <= 0 or len(power) < 3:
        return None
    i = max(range(len(power)), key=power.__getitem__)
    if i == 0 or i == len(power) - 1:
        return None
    f = freqs[i]
    l, m, r = power[i - 1], power[i], power[i + 1]   # parabolic refinement between scan steps
    den = l - 2 * m + r
    if den < 0:
        f += 0.5 * (l - r) / den * df
    # the peak's share: power within one scan step either side, over the whole band
    share = sum(power[max(0, i - 1):i + 2]) / total
    return 60.0 * f, share


def breath_window(beats, a, p=RESP):
    """(breaths/min, peak share) for beats in [a, a + window), or None if too few clean beats."""
    sp = breath_spectrum(beats, a, p)
    return breath_peak(sp[0], sp[1], p["df"]) if sp else None


def calm_beats(c, a, b, p=RESP):
    """Clean beats in [a, b) from seconds within p["calm_margin"] of the period's resting heart
    rate (the calmest 5 minutes, as night_stats uses for HRV). [] if there are none."""
    s = day_stats(c, a, b)
    if not s or s["resting"] is None:
        return []
    cap = s["resting"] + p["calm_margin"]
    rows = c.execute("SELECT ts, hr, rr_ms FROM ble_hr WHERE ts >= ? AND ts < ? AND hr > 0 ORDER BY ts",
                     (a, b)).fetchall()
    beats, run = [], []
    for r in rows:                          # calm stretches only, each rebuilt on its own
        if r[1] <= cap:
            run.append((r[0], r[1], r[2]))
        elif run:
            beats += beat_series(run); run = []
    return beats + beat_series(run)


def breath_windows(beats, a, b, p=RESP):
    """[(window start, breath_window result or None)] for the windows in [a, b) that have beats."""
    out, t, i0, w = [], a, 0, p["window_s"]
    while t + w <= b:
        while i0 < len(beats) and beats[i0][0] < t:
            i0 += 1
        sub = beats[i0:i0 + int(w * 4)]               # at most 240 beats a minute
        if sub and sub[0][0] < t + w:
            out.append((t, breath_window(sub, t, p)))
        t += p["step_s"]
    return out


def resp_rate(c, a, b, p=None):
    """Breathing rate (breaths/min) over [a, b) from the strap's beat intervals, using only calm
    seconds (heart rate within p["calm_margin"] of the period's resting rate, as for HRV).

    Returns {"bpm", "windows" (clean windows used), "of" (windows that had beats)}; bpm is None
    when fewer than p["min_windows"] windows have enough clean beats and a clear breathing peak."""
    p = p or RESP
    wins = breath_windows(calm_beats(c, a, b, p), a, b, p)
    rates = sorted(e[0] for _, e in wins if e and e[1] >= p["min_peak"])
    k = len(rates)
    if k < p["min_windows"]:
        return {"bpm": None, "windows": k, "of": len(wins)}
    med = rates[k // 2] if k % 2 else (rates[k // 2 - 1] + rates[k // 2]) / 2
    return {"bpm": round(med, 1), "windows": k, "of": len(wins)}


def _overlap(a, b, lo, hi):
    return max(0.0, min(b, hi) - max(a, lo))


def worn_seconds(c, a, b):
    """Seconds in [a, b) the strap was on the wrist and stored data, from strap_min (decode.py).
    Minutes cut by a or b count in proportion. 0 when nothing has been decoded for that time."""
    try:
        rows = c.execute("SELECT minute, n FROM strap_min WHERE minute > ? AND minute < ?", (a - 60, b)).fetchall()
    except Exception:                       # no strap_min yet (decode has not run on this DB)
        return 0.0
    return sum(min(r["n"], 60) * _overlap(a, b, r["minute"], r["minute"] + 60) / 60 for r in rows)


def off_wrist_seconds(c, a, b):
    """Seconds in [a, b) the strap itself says it was off the wrist (offwrist, decode.py)."""
    try:
        rows = c.execute("SELECT start, end FROM offwrist WHERE end > ? AND start < ?", (a, b)).fetchall()
    except Exception:
        return 0.0
    return sum(_overlap(a, b, r["start"], r["end"]) for r in rows)


def wear_coverage(c, a, b):
    """How [a, b) is covered: worn (data stored), off (strap off the wrist), unknown (not read yet,
    or lost). Lets a coverage gate tell "you took it off" from "we have no data"."""
    worn, off = worn_seconds(c, a, b), off_wrist_seconds(c, a, b)
    return {"worn_s": worn, "off_s": off, "unknown_s": max(0.0, (b - a) - worn - off)}


def steps(c, a, b):
    """Steps the strap counted in the minutes starting in [a, b), or None if nothing was decoded."""
    try:
        r = c.execute("SELECT SUM(steps) s, COUNT(*) n FROM strap_min WHERE minute >= ? AND minute < ?",
                      (a, b)).fetchone()
    except Exception:
        return None
    return r["s"] if r["n"] else None


def day_bounds(offset_days=0, now=None):
    """Local midnight to midnight for today (0) or yesterday (-1)."""
    lt = time.localtime(now or time.time())
    start = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday + offset_days, 0, 0, 0, 0, 0, -1))
    end = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday + offset_days + 1, 0, 0, 0, 0, 0, -1))
    return start, end


def battery(c, key="battery_now"):
    """Latest strap battery %, or None if unknown or more than a day old.

    The daemon asks the strap (GetBatteryLevel) on every connect and after every drain and
    keeps the answer in sync_state. The firmware's "SOC report" log lines are not used:
    a history drain replays old console logs, so the newest line to arrive can be months old.
    """
    import json, db
    try:
        now = json.loads(db.get_state(c, key) or "null")
    except ValueError:
        return None
    if not now or now.get("src") != "strap":   # written by an older decode.py from log lines
        return None
    return now["soc"] if time.time() - now["ts"] < 86400 else None


def pack_battery(c):
    """Latest battery pack % (Battery Level characteristic; likely the pack), or None."""
    return battery(c, "pack_battery_now")


# ---------------- coverage: how much of a span the strap's heart rate covers ----------------
GAP_S = 60               # seconds without heart rate before it counts as a gap
HOST_WORDS = {"asleep": "the Mac was asleep", "searching": "the Mac couldn't reach the strap",
              "linked": "the strap was connected", None: "the daemon wasn't running"}


def _overlap(a, b, x, y):
    return max(0.0, min(b, y) - max(a, x))


def coverage(c, a, b, now=None, min_gap=GAP_S):
    """How much of [a, b) has heart rate in ble_hr, and why the rest doesn't.

    Returns {"span_s", "recorded_s", "share", "pending_s", "nothing_s", "read_to", "gaps"}:
      recorded_s  seconds with at least one heart-rate row
      pending_s   gap time the strap's stored history hasn't been read for yet (sync_state
                  history_ts). If you wore the strap then, it is still on it and fills in at
                  the next sync: this is the "worn but missing" estimate (an upper bound,
                  since the strap may have been off the wrist).
      nothing_s   gap time already read from the strap with nothing stored for it (off the
                  wrist, charging, flat battery, or another app such as WHOOP's read and
                  cleared it first), so nothing more will come
      gaps        [{"start", "end", "pending_s", "host": {state: seconds}, "why"}], longest
                  first; host comes from host_log (what the laptop was doing)
    The future is never counted: b is clipped to now.
    """
    now = time.time() if now is None else now
    b = min(b, now)
    span = max(0.0, b - a)
    out = {"span_s": span, "recorded_s": 0, "share": 0.0, "pending_s": 0.0, "nothing_s": 0.0,
           "read_to": None, "gaps": []}
    if span <= 0:
        return out
    secs = [r[0] for r in c.execute("SELECT DISTINCT CAST(ts AS INTEGER) FROM ble_hr "
                                    "WHERE ts >= ? AND ts < ? AND hr > 0 ORDER BY 1", (a, b))]
    out["recorded_s"] = len(secs)
    out["share"] = min(1.0, len(secs) / span)
    r = c.execute("SELECT value FROM sync_state WHERE key='history_ts'").fetchone()
    read_to = float(r[0]) if r and r[0] else None
    out["read_to"] = read_to
    edges = [a - 1] + secs + [b]
    holes = [(x + 1, y) for x, y in zip(edges, edges[1:]) if y - x > min_gap]
    host, since = [], None
    if holes:
        try:
            host = c.execute("SELECT start, end, state FROM host_log WHERE end > ? AND start < ? ORDER BY start",
                             (a, b)).fetchall()
            since = c.execute("SELECT MIN(start) FROM host_log").fetchone()[0]
        except Exception:                      # a database the daemon never ran on
            host = []
    for x, y in holes:
        seen = {}
        for h in host:
            o = _overlap(x, y, h[0], h[1])
            if o:
                seen[h[2]] = seen.get(h[2], 0.0) + o
        # time host_log doesn't cover, from when it started: the daemon wasn't running
        unwatched = (y - max(x, since)) - sum(seen.values()) if since is not None and y > since else 0
        if unwatched > 1:
            seen[None] = unwatched
        pending = 0.0 if read_to is None else max(0.0, y - max(x, read_to))
        out["pending_s"] += pending
        out["nothing_s"] += (y - x) - pending if read_to is not None else 0.0
        out["gaps"].append({"start": x, "end": y, "pending_s": pending,
                            "host": {k or "none": round(v) for k, v in seen.items()},
                            "why": _why(y - x, pending, seen, read_to)})
    out["gaps"].sort(key=lambda g: g["start"] - g["end"])
    return out


def _why(length, pending, seen, read_to):
    """One short phrase for a gap, from what the strap and the laptop say about it."""
    main = max(seen, key=seen.get) if seen else None
    mostly = HOST_WORDS[main] if seen and seen[main] >= length / 2 else None
    if read_to is None:                        # history is never read here (--no-drain)
        return mostly or "no heart rate"
    if pending >= length / 2:
        why = "still on the strap, comes in at the next sync"
        return f"{why} ({mostly})" if mostly and main != "linked" else why
    if main == "linked" and seen[main] >= length / 2:
        return "the strap was connected but sent no heart rate (off the wrist?)"
    if main is None and mostly:                # read later, by this daemon or something else
        return "the daemon wasn't running, and the strap had nothing stored for it later"
    # read past, nothing came: off the wrist, charging, or another app (WHOOP's) read it first
    why = "the strap had nothing stored for it (off the wrist, charging, or read by another app)"
    return f"{why}; {mostly}" if mostly else why


def coverage_days(c, days=7, now=None):
    """Per calendar day, newest first: share recorded, the 'worn but missing' estimate and the
    longest gap, for the Device page. Stops at the first day before any heart rate."""
    now = time.time() if now is None else now
    first = c.execute("SELECT MIN(ts) m FROM ble_hr WHERE ts > ?", (now - (days + 1) * 86400,)).fetchone()[0]
    out = []
    for k in range(days):
        a, b = day_bounds(-k, now)
        if first is None or b <= first:
            break
        cov = coverage(c, a, b, now)
        g = cov["gaps"][0] if cov["gaps"] else None
        out.append({"date": time.strftime("%Y-%m-%d", time.localtime(a)), "span_s": round(cov["span_s"]),
                    "recorded_s": cov["recorded_s"], "share": round(cov["share"], 3),
                    "pending_s": round(cov["pending_s"]), "nothing_s": round(cov["nothing_s"]),
                    "longest": {"start": g["start"], "end": g["end"], "why": g["why"]} if g else None})
    return out


def _hm(ts):
    return time.strftime("%H:%M", time.localtime(ts))


def gap_text(g):
    """'14:00-19:00 (5h 0m): why' for a gap from coverage()."""
    h, m = divmod(int((g["end"] - g["start"]) // 60), 60)
    return f"{_hm(g['start'])}-{_hm(g['end'])} ({f'{h}h {m}m' if h else f'{m} min'}): {g['why']}"
