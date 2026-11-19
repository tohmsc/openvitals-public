#!/usr/bin/env python3
"""Health alerts: sustained changes against your own usual, only from clean nights.

Each rule needs RUN nights (or mornings) in a row past its line. One bad night is noise and
never fires anything; a night whose reading is suspect, too thin or missing breaks the run
instead of counting toward it. Your usual comes from your own strap nights before the run
(learn.py), so the nights being judged don't drag the usual toward themselves.

  rhr_up        resting HR clearly above your usual (learn.trend: 4 bpm or 1.5x your normal
                night-to-night variation over it, whichever is more)
  hrv_low       overnight HRV clearly below your usual (by 20% or 1.5x your normal variation,
                whichever is more), only nights with enough clean beats (learn.MIN_RR)
  short_sleep   asleep at least an hour and 15% less than your usual (sleep.py), only nights
                that look like a real night (2-14 h, heart rate recorded for 85% of it)
  resp_up       breathing rate during sleep 1 breath/min or 1.5x your variation above your
                usual, only if a breathing-rate value is stored for the night
  low_recovery  recovery estimate under 25% (scores.py, only estimates that passed its checks)

The app shows the alerts that are on (Today page); Telegram sends each once when it starts
(telegram_report.nudges) and not again until it has ended and started over.

This is pattern-matching on your own numbers. It does not detect or diagnose anything and is
not medical advice.

    python alerts.py          # what is on, and why
"""
import math, statistics, time
import db, learn, metrics

RUN = 3                     # nights in a row before an alert
RHR_HRV_DROP = 0.20         # HRV this far below usual (as a share), at least
SHORT_MIN_S = 3600          # sleep at least this much under usual...
SHORT_SHARE = 0.15          # ...and at least this share of it
SLEEP_H = (2.0, 14.0)       # outside this a "night" is a detection problem
SLEEP_COVERAGE = 0.85
RESP_UP = 1.0               # breaths/min over usual, at least
RESP_MIN_NIGHTS = 5
RESP_COLUMNS = ("resp_rate", "resp", "breath_rate", "breathing_rate")
LOW_RECOVERY = 25.0
NOTE = ("This is pattern-matching on your own numbers, not a diagnosis. If you feel unwell, "
        "or this goes on, talk to a doctor.")


def _shift(date, days):
    return time.strftime("%Y-%m-%d", time.localtime(time.mktime(time.strptime(date, "%Y-%m-%d")) + days * 86400 + 43200))


def _has(c, table):
    return c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None


def _cols(c, table):
    return {r["name"] for r in c.execute(f"PRAGMA table_info({table})")} if _has(c, table) else set()


def _anchor(dates, today):
    """The night a run is counted back from: this morning's if it is in, else yesterday's.
    So an alert doesn't switch off (and on again, re-sending) just because this morning's
    night hasn't been read yet. None when the latest night is older than that."""
    for d in (today, _shift(today, -1)):
        if d in dates:
            return d
    return None


def run_back(nights, anchor, past):
    """Dates in a row ending at `anchor` whose value is clean (not None) and past the line."""
    out, d = [], anchor
    while d in nights and nights[d] is not None and past(nights[d]):
        out.append(d)
        d = _shift(d, -1)
    return list(reversed(out))


def _alert(key, title, dates, text, why, threshold):
    return {"key": key, "title": title, "nights": len(dates), "since": dates[0], "until": dates[-1],
            "text": text, "why": why, "threshold": threshold, "note": NOTE}


def _fmt_list(xs, d=0):
    return ", ".join(f"{x:.{d}f}" for x in xs)


# ---------------- the rules ----------------
def _own_usual(c, anchor):
    """Your usual from nights before the last RUN (so the run doesn't count toward its own usual)."""
    u = learn.usual(c, _shift(anchor, -(RUN - 1)))
    return u if u["source"] == "own" else None


def rhr_up(c, today):
    rows = {r["date"]: r["rhr"] for r in c.execute(
        "SELECT date, rhr FROM daily_night WHERE date <= ? AND rhr IS NOT NULL ORDER BY date DESC LIMIT 30", (today,))}
    anchor = _anchor(rows, today)
    u = anchor and _own_usual(c, anchor)
    if not u:
        return None
    n = learn.trend(c, anchor, u)                     # the existing rule: 3+ mornings in a row
    if not n:
        return None
    dates = [_shift(anchor, -k) for k in range(n - 1, -1, -1)]
    vals = [rows[d] for d in dates]
    line = learn.trend_line(u)
    return _alert("rhr_up", "Resting heart rate up", dates,
                  f"Your resting heart rate has been above your usual for {n} nights in a row.",
                  f"Last {n} nights: {_fmt_list(vals)} bpm. Your usual: {u['rhr']:.0f} bpm.",
                  f"Alert line: {line:.0f} bpm (usual + 4 bpm, or 1.5x your normal night-to-night "
                  f"variation, whichever is more), {RUN}+ nights in a row.")


def hrv_low(c, today):
    nights = {}
    for r in c.execute("SELECT date, rhr, hrv, n_rr FROM daily_night WHERE date <= ? AND rhr IS NOT NULL "
                       "ORDER BY date DESC LIMIT 30", (today,)):
        clean = (r["hrv"] and r["n_rr"] is not None and r["n_rr"] >= learn.MIN_RR
                 and metrics.HRV_PLAUSIBLE[0] <= r["hrv"] <= metrics.HRV_PLAUSIBLE[1]
                 and metrics.RHR_PLAUSIBLE[0] <= r["rhr"] <= metrics.RHR_PLAUSIBLE[1])
        nights[r["date"]] = r["hrv"] if clean else None
    anchor = _anchor(nights, today)
    u = anchor and _own_usual(c, anchor)
    if not u or not u.get("hrv"):
        return None
    drop = max(RHR_HRV_DROP, 1 - math.exp(-1.5 * u["hrv_spread"]))
    line = u["hrv"] * (1 - drop)
    dates = run_back(nights, anchor, lambda v: v < line)
    if len(dates) < RUN:
        return None
    return _alert("hrv_low", "HRV down", dates,
                  f"Your overnight HRV has been below your usual for {len(dates)} nights in a row.",
                  f"Last {len(dates)} nights: {_fmt_list([nights[d] for d in dates])} ms. Your usual: {u['hrv']:.0f} ms.",
                  f"Alert line: {line:.0f} ms ({drop:.0%} under usual, from your normal night-to-night "
                  f"variation, at least 20%), {RUN}+ nights in a row, only nights with enough clean beats.")


def _sleep_clean(c, r):
    h = (r["asleep_s"] or 0) / 3600
    if r["status"] != "done" or not SLEEP_H[0] <= h <= SLEEP_H[1] or not r["wake"] or r["wake"] <= r["onset"]:
        return False
    n = c.execute("SELECT COUNT(*) n FROM ble_hr WHERE ts >= ? AND ts < ? AND hr > 0",
                  (r["onset"], r["wake"])).fetchone()["n"]
    return n / (r["wake"] - r["onset"]) >= SLEEP_COVERAGE


def short_sleep(c, today):
    if not _has(c, "sleep_night"):
        return None
    rows = c.execute("SELECT date, onset, wake, asleep_s, usual_s, status FROM sleep_night WHERE date <= ? "
                     "ORDER BY date DESC LIMIT 14", (today,)).fetchall()
    nights = {r["date"]: (r["asleep_s"], r["usual_s"]) if _sleep_clean(c, r) and r["usual_s"] else None
              for r in rows}
    anchor = _anchor(nights, today)
    if not anchor:
        return None
    line = lambda usual: usual - max(SHORT_MIN_S, SHORT_SHARE * usual)
    dates = run_back(nights, anchor, lambda v: v[0] < line(v[1]))
    if len(dates) < RUN:
        return None
    hours = [nights[d][0] / 3600 for d in dates]
    usual = nights[dates[-1]][1]
    return _alert("short_sleep", "Short sleep", dates,
                  f"You've slept much less than usual for {len(dates)} nights in a row.",
                  f"Last {len(dates)} nights: {_fmt_list(hours, 1)} h asleep. Your usual: {usual / 3600:.1f} h.",
                  f"Alert line: {line(usual) / 3600:.1f} h (at least an hour and 15% under your usual), "
                  f"{RUN}+ nights in a row, only nights that look complete.")


def resp_up(c, today):
    """Only if some night table stores a breathing rate (added separately); otherwise silent."""
    for table in ("sleep_night", "daily_night"):
        col = next((k for k in RESP_COLUMNS if k in _cols(c, table)), None)
        if col:
            break
    else:
        return None
    rows = c.execute(f"SELECT date, {col} v FROM {table} WHERE date <= ? ORDER BY date DESC LIMIT 40",
                     (today,)).fetchall()
    nights = {r["date"]: r["v"] if r["v"] and 4 <= r["v"] <= 40 else None for r in rows}   # breaths/min a person has
    anchor = _anchor(nights, today)
    if not anchor:
        return None
    start = _shift(anchor, -(RUN - 1))
    before = [v for d, v in nights.items() if d < start and v is not None]
    if len(before) < RESP_MIN_NIGHTS:
        return None
    usual = statistics.median(before)
    spread = max(0.3, 1.4826 * statistics.median([abs(v - usual) for v in before]))
    line = usual + max(RESP_UP, 1.5 * spread)
    dates = run_back(nights, anchor, lambda v: v > line)
    if len(dates) < RUN:
        return None
    return _alert("resp_up", "Breathing rate up", dates,
                  f"Your breathing rate during sleep has been above your usual for {len(dates)} nights in a row.",
                  f"Last {len(dates)} nights: {_fmt_list([nights[d] for d in dates], 1)} breaths/min. "
                  f"Your usual: {usual:.1f}.",
                  f"Alert line: {line:.1f} breaths/min (usual + 1, or 1.5x your normal variation), "
                  f"{RUN}+ nights in a row.")


def low_recovery(c, today):
    if not _has(c, "estimates"):
        return None
    import scores
    nights = {r["date"]: r["value"] if r["ok"] and r["value"] is not None else None for r in c.execute(
        "SELECT date, value, ok FROM estimates WHERE kind='recovery' AND version=? AND date <= ? "
        "ORDER BY date DESC LIMIT 14", (scores.current(c), today))}
    anchor = _anchor(nights, today)
    if not anchor:
        return None
    dates = run_back(nights, anchor, lambda v: v < LOW_RECOVERY)
    if len(dates) < RUN:
        return None
    return _alert("low_recovery", "Very low recovery", dates,
                  f"Your recovery estimate has been very low {len(dates)} mornings in a row.",
                  f"Last {len(dates)} mornings: {_fmt_list([nights[d] for d in dates])}%.",
                  f"Alert line: under {LOW_RECOVERY:.0f}%, {RUN}+ mornings in a row, only estimates that "
                  "passed their checks.")


RULES = {"rhr_up": rhr_up, "hrv_low": hrv_low, "short_sleep": short_sleep, "resp_up": resp_up,
         "low_recovery": low_recovery}


def evaluate(c, today=None):
    """{rule key: alert dict, or None when it is off}. A rule that errors is off (and says so)."""
    today = today or learn._date(time.time())
    out = {}
    for key, rule in RULES.items():
        try:
            out[key] = rule(c, today)
        except Exception as ex:               # one broken rule must not take the others down
            print(f"alert {key} error:", ex)
            out[key] = None
    return out


def active(c, today=None):
    return [a for a in evaluate(c, today).values() if a]


def message(a):
    """The Telegram text for an alert starting."""
    return f"⚠️ {a['title']}. {a['text']}\n{a['why']}\n{a['threshold']}\n{a['note']}"


if __name__ == "__main__":
    c = db.connect()
    on = active(c)
    if not on:
        print("No alerts. (Each needs 3 clean nights in a row past its line.)")
    for a in on:
        print(message(a), "\n")
