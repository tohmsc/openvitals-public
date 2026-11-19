#!/usr/bin/env python3
"""When you fell asleep and woke up, from the strap alone: wrist movement (the accelerometer in
the stored history, per minute in motion_min, filled by decode.py) and heart rate (ble_hr).

The method is wrist actigraphy, which is how sleep labs score wrist trackers, plus a heart-rate check:
  1. Each minute gets a movement score: the movement in the 7 minutes around it, weighted
     toward the minute itself (Cole-Kripke weights).
  2. A minute is asleep when that score is low and the heart rate is not far (30 bpm) above the
     night's low. Awake runs of 1-2 minutes inside sleep (turning over) count as sleep.
  3. Asleep runs of 10+ minutes are sleep blocks. Blocks less than 60 minutes apart belong to
     the same sleep, and the night is the group with the most sleep. It starts at its first
     block of 20+ minutes and ends at the end of its last block.
  4. Inside it, 5+ minutes in a row awake is a wake-up.

How close, checked against a sleep lab (2026-11-19, fit/sleep_psg/): the public PhysioNet
dataset of Walch et al. 2019 (31 adults, one polysomnography night each, Apple Watch movement
and heart rate turned into exactly this module's per-minute inputs). Held-out numbers (each
person scored with the HR_ABOVE x JOIN setting chosen on the other 30), mean absolute error:
    falling asleep  6.5 min (median -1)    waking up  33 min (median -0.5; 6 of 31 off by 40+,
                                           two by 4-5 h: gaps / a restless end in the watch data)
    time asleep     43 min (median -4)     per 30 s:  86% right, 88% of sleep, 67% of wake found
    wake-ups (5+ min) about 3 too many a night
The settings before this check (heart rate 15 bpm over the low, blocks joined within 30 min)
scored 28 / 40 / 73 min and 78% on the same nights: REM sleep raises heart rate past 15 bpm,
which split nights in two and started some 2-3 hours late. Re-run with the watch's 1 Hz signal
smoothed instead (the strap's own filtering is unknown) the held-out errors are 18 / 25 / 45 min
and 88%. The movement threshold STILL was not tuned: its scale depends on the device, and 0.02
sits between the best values for the two versions of the watch signal (0.015 and 0.025).
Caveats: a lab night starts at lights-out, so lying still in bed before sleeping is barely
tested; lying completely still while awake reads as asleep (wake is the weak side, as for all
wrist actigraphy); not yet checked on this strap's own nights.

    python sleep.py            # recompute recent nights and print them
    python sleep.py 2026-11-19 # one night (the date you woke up)
"""
import json, statistics, sys, time
import db, metrics

STILL = 0.02          # weighted movement per minute (g per one-second step) below this is still
HR_ABOVE = 30         # still, but heart rate this far over the night's low: awake (15 cut REM sleep out)
FILL = 2              # awake runs this short inside sleep are turning over, not waking
MIN_BLOCK = 10        # minutes asleep in a row to count as a sleep block
FIRST_BLOCK = 20      # the night starts with a block at least this long
JOIN = 60             # blocks closer than this many minutes are the same sleep
WAKEUP = 5            # minutes awake in a row that count as waking up
DONE_AFTER = 30       # minutes of movement data after the last block before the night counts as over
UP_AFTER = 20         # ...or minutes of live heart rate after it, clearly above the night's low
UP_HR = 10            # bpm over the night's low that live heart rate must sit at to say "up"
GAP_FILL = 10         # missing minutes (strap not read) up to this long take the state around them
EVENING, NOON = -6, 14  # search from 18:00 the evening before to 14:00 on the day you woke
WEIGHTS = (106, 54, 58, 76, 230, 74, 67)  # Cole-Kripke, minutes m-4 .. m+2

SCHEMA = """CREATE TABLE IF NOT EXISTS sleep_night (
  date TEXT PRIMARY KEY, onset REAL, wake REAL, asleep_s REAL, awake_s REAL, wakeups INTEGER,
  rhr REAL, hrv REAL, usual_s REAL, status TEXT, headline TEXT, detail TEXT, segments TEXT,
  computed_at REAL)"""
COLS = "date,onset,wake,asleep_s,awake_s,wakeups,rhr,hrv,usual_s,status,headline,detail,segments,computed_at"


def _runs(states):
    """[(state, start index, length)] for consecutive equal states."""
    out = []
    for i, s in enumerate(states):
        if out and out[-1][0] == s:
            out[-1][2] += 1
        else:
            out.append([s, i, 1])
    return out


def minutes(c, a, b):
    """Per-minute (movement, heart rate) for [a, b), as two lists; None where nothing was read."""
    n = int((b - a) // 60)
    move, hr = [None] * n, [[] for _ in range(n)]
    for r in c.execute("SELECT minute, move_sum, n FROM motion_min WHERE minute >= ? AND minute < ?", (a, b)):
        i = int((r["minute"] - a) // 60)
        if 0 <= i < n and r["n"] >= 20:              # a third of the minute read, at least
            move[i] = r["move_sum"] / r["n"]
    for r in c.execute("SELECT ts, hr FROM ble_hr WHERE ts >= ? AND ts < ? AND hr > 0", (a, b)):
        hr[int((r["ts"] - a) // 60)].append(r["hr"])
    return move, [statistics.median(h) if len(h) >= 10 else None for h in hr]


def night_low(move, hr):
    """The night's low heart rate: 5th percentile of still minutes (None with too little data)."""
    known = [h for m, h in zip(move, hr) if h is not None and m is not None and m < STILL]
    return sorted(known)[len(known) // 20] if len(known) >= 30 else None


def classify(move, hr):
    """'s' asleep, 'a' awake, None no data, per minute."""
    n, low = len(move), night_low(move, hr)
    st = []
    for i in range(n):
        if move[i] is None:
            st.append(None)
            continue
        num = den = 0.0
        for k, w in enumerate(WEIGHTS):
            j = i + k - 4
            if 0 <= j < n and move[j] is not None:
                num += w * move[j]; den += w
        score = num / den
        h = [x for x in hr[max(0, i - 2):i + 3] if x is not None]
        racing = low is not None and h and statistics.median(h) > low + HR_ABOVE
        st.append("s" if score < STILL and not racing else "a")
    # short gaps in the data take the state around them; short awake runs inside sleep are sleep
    runs = _runs(st)
    for k, (s, i, ln) in enumerate(runs):
        if 0 < k < len(runs) - 1 and runs[k - 1][0] == runs[k + 1][0] and runs[k - 1][0] is not None:
            if (s is None and ln <= GAP_FILL) or (s == "a" and ln <= FILL and runs[k - 1][0] == "s"):
                st[i:i + ln] = [runs[k - 1][0]] * ln
    return st


def find(st):
    """(first, last) minute index of the night's sleep, or None."""
    blocks = [(i, i + ln) for s, i, ln in _runs(st) if s == "s" and ln >= MIN_BLOCK]
    groups = []
    for b in blocks:
        if groups and b[0] - groups[-1][-1][1] <= JOIN:
            groups[-1].append(b)
        else:
            groups.append([b])
    best, best_n = None, 0
    for g in groups:
        g = g[next((k for k, b in enumerate(g) if b[1] - b[0] >= FIRST_BLOCK), len(g)):]
        n = sum(1 for b in g for _ in range(*b))
        if g and n > best_n:
            best, best_n = (g[0][0], g[-1][1]), n
    return best


def usual_s(c, date):
    """Your usual time asleep: your own nights here once there are 3, else your WHOOP years."""
    own = [r["asleep_s"] for r in c.execute(
        "SELECT asleep_s FROM sleep_night WHERE status='done' AND date < ? ORDER BY date DESC LIMIT 30", (date,))]
    if len(own) >= 3:
        return statistics.median(own)
    whoop = [r["s"] for r in c.execute(
        "SELECT (total_in_bed_ms - COALESCE(total_awake_ms, 0)) / 1000.0 s FROM api_sleep "
        "WHERE COALESCE(nap,0)=0 AND total_in_bed_ms > 0 ORDER BY start DESC LIMIT 120")]
    return statistics.median(whoop) if len(whoop) >= 10 else None


def dur(s):
    h, m = divmod(int(round(s / 60)), 60)
    return f"{h} h {m:02d} min" if h else f"{m} min"


def clock(ts):
    return time.strftime("%H:%M", time.localtime(ts))


def words(night):
    """(headline, detail): the 'here is how you slept' text. Shared by the app and Telegram."""
    a, u = night["asleep_s"], night.get("usual_s")
    if night["status"] != "done":
        return (f"{dur(a)} asleep so far",
                f"Fell asleep at {clock(night['onset'])}. The rest of the night is still coming in from the strap.")
    head = f"You slept {dur(a)}"
    if u:
        d = a - u
        head += (", about your usual." if abs(d) < 20 * 60 else
                 f", {dur(abs(d))} {'more' if d > 0 else 'less'} than usual.")
    else:
        head += "."
    w = night["wakeups"]
    woke = "without waking up" if not w else "woke up once" if w == 1 else f"woke up {w} times"
    return head, f"Fell asleep at {clock(night['onset'])}, up at {clock(night['wake'])}, {woke}."


def night(c, morning, now=None):
    """Last night's sleep for the day starting at local midnight `morning`, or None."""
    now = now or time.time()
    a, b = morning + EVENING * 3600, min(morning + NOON * 3600, now - now % 60)
    if b - a < 3600:
        return None
    move, hr = minutes(c, a, b)
    st = classify(move, hr)
    found = find(st)
    if not found:
        return None
    i, j = found
    onset, wake = a + i * 60, a + j * 60
    inside = st[i:j]
    awake_runs = [ln for s, _, ln in _runs(inside) if s == "a"]
    # Over once movement after the last block says so, or (the strap's stored history runs
    # behind real time) live heart rate after it has been up for a while.
    data_after = sum(1 for s in st[j:] if s is not None)
    low, live = night_low(move, hr), [h for h in hr[j:] if h is not None]
    up = low is not None and len(live) >= UP_AFTER and statistics.median(live) > low + UP_HR
    done = data_after >= DONE_AFTER or up or now >= morning + NOON * 3600
    stats = metrics.day_stats(c, onset, wake)
    rhr = stats and stats["resting"]
    hrv = None
    if rhr:
        calm = metrics.day_stats(c, onset, wake, rr_max_hr=rhr + metrics.ASLEEP_MARGIN)
        hrv = calm and calm["rmssd"]
    # what the app draws: an hour either side, as runs of asleep / awake / no data
    lo, hi = max(0, i - 60), min(len(st), j + 60)
    segs = [[a + (lo + k) * 60, a + (lo + k + ln) * 60, s or "-"] for s, k, ln in _runs(st[lo:hi])]
    n = {
        "date": time.strftime("%Y-%m-%d", time.localtime(morning)),
        "onset": onset, "wake": wake,
        "asleep_s": inside.count("s") * 60.0, "awake_s": inside.count("a") * 60.0,
        "wakeups": sum(1 for ln in awake_runs if ln >= WAKEUP),
        "rhr": rhr, "hrv": hrv, "status": "done" if done else "ongoing", "segments": json.dumps(segs),
    }
    n["usual_s"] = usual_s(c, n["date"])
    n["headline"], n["detail"] = words(n)
    return n


def update(c=None, now=None):
    """Recompute the nights that can still change (yesterday, today, and tonight once it starts)."""
    c = c or db.connect()
    c.execute(SCHEMA)
    now = now or time.time()
    today = metrics.day_bounds(0, now)[0]
    out = []
    for morning in (today - 86400, today, today + 86400):
        if morning + EVENING * 3600 > now:
            continue
        n = night(c, morning, now)
        if not n:
            continue
        old = c.execute("SELECT status FROM sleep_night WHERE date=?", (n["date"],)).fetchone()
        if old and old["status"] == "done" and n["status"] != "done":
            continue
        n["computed_at"] = time.time()
        c.execute(f"INSERT OR REPLACE INTO sleep_night({COLS}) VALUES({','.join('?' * len(COLS.split(',')))})",
                  [n[k] for k in COLS.split(",")])
        out.append(n)
    c.commit()
    return out


def recent(c, limit=30):
    c.execute(SCHEMA)
    return [dict(r) for r in c.execute(f"SELECT {COLS} FROM sleep_night ORDER BY date DESC LIMIT ?", (limit,))]


if __name__ == "__main__":
    c = db.connect()
    if len(sys.argv) > 1:
        m = time.mktime(time.strptime(sys.argv[1], "%Y-%m-%d"))
        nights = [night(c, m)]
    else:
        nights = update(c)
    for n in nights:
        if not n:
            print("no sleep found")
            continue
        print(f"{n['date']}  {clock(n['onset'])} -> {clock(n['wake'])}  asleep {dur(n['asleep_s'])}, "
              f"awake {dur(n['awake_s'])}, {n['wakeups']} wake-ups, rhr {n['rhr'] and round(n['rhr'])}, "
              f"hrv {n['hrv'] and round(n['hrv'])}  [{n['status']}]")
        print("   ", n["headline"], n["detail"])
