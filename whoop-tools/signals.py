#!/usr/bin/env python3
"""Nightly skin temperature from the strap, as a change against your own usual (what WHOOP shows too).

Input: strap_min (decode.py: per-minute raw skin temperature) and sleep_night (sleep.py: when
you fell asleep and woke up). Output: one row per night in skin_night.

What is known about the raw value (record version 24, offset 69; see whoop.parse_historical):
  - it is a temperature: after putting the strap on it climbs ~30-40 counts over 20-30 minutes
    (warming up on the wrist) and it is highest while asleep, like wrist skin temperature;
  - on the one night we have both raw records and WHOOP's own reading (the end of the night of
    11-12 June 2025), the raw mean was 342.8 and WHOOP said 33.9 C. So ~0.1 C per count fits,
    but one partial night cannot fix the scale or the offset: it is NOT calibrated.
So the number this keeps is the change against your own usual night, in raw counts, with an
approximate C (counts / 10) that is labelled as approximate wherever it is shown.

Guards: a night needs MIN_MINUTES of worn minutes between falling asleep and waking up;
the usual needs BASELINE_MIN earlier nights (from the last BASELINE_NIGHTS), else "learning".

    python signals.py      # recompute recent nights and print them
"""
import statistics, time
import db

PER_C = 10            # raw counts per degree C: likely (see above), not calibrated
MIN_MINUTES = 180     # worn minutes asleep for a night's temperature to count
MIN_IN_MINUTE = 30    # records in a minute for that minute to count as worn
BASELINE_NIGHTS = 30  # your usual: the median of up to this many earlier nights...
BASELINE_MIN = 3      # ...once there are at least this many

SCHEMA = """CREATE TABLE IF NOT EXISTS skin_night (
  date TEXT PRIMARY KEY, temp_raw REAL, minutes INTEGER, usual_raw REAL, nights INTEGER,
  dev_raw REAL, dev_c REAL, status TEXT, computed_at REAL)"""


def night_temp(c, onset, wake):
    """(mean raw skin temperature, worn minutes) between onset and wake; (None, minutes) if too few."""
    rows = c.execute("SELECT temp_sum, temp_n FROM strap_min WHERE minute >= ? AND minute < ? AND temp_n >= ?",
                     (onset, wake, MIN_IN_MINUTE)).fetchall()
    if len(rows) < MIN_MINUTES:
        return None, len(rows)
    return statistics.fmean(r["temp_sum"] / r["temp_n"] for r in rows), len(rows)


def update(c=None, days=14):
    """Recompute skin_night for the finished nights of the last `days` days. Returns the rows."""
    own = c is None
    c = c or db.connect()
    c.execute(SCHEMA)
    try:
        nights = c.execute("SELECT date, onset, wake FROM sleep_night WHERE status='done' AND onset IS NOT NULL "
                           "AND wake IS NOT NULL AND date >= date('now', ?) ORDER BY date",
                           (f"-{int(days)} days",)).fetchall()
    except Exception:                    # sleep.py has not run on this database yet
        nights = []
    out = []
    for n in nights:
        temp, minutes = night_temp(c, n["onset"], n["wake"])
        before = [r["temp_raw"] for r in c.execute(
            "SELECT temp_raw FROM skin_night WHERE date < ? AND temp_raw IS NOT NULL ORDER BY date DESC LIMIT ?",
            (n["date"], BASELINE_NIGHTS))]
        usual = statistics.median(before) if len(before) >= BASELINE_MIN else None
        if temp is None:
            status, dev = "too little", None
        elif usual is None:
            status, dev = "learning", None
        else:
            status, dev = "ok", temp - usual
        row = {"date": n["date"], "temp_raw": temp, "minutes": minutes, "usual_raw": usual, "nights": len(before),
               "dev_raw": dev, "dev_c": None if dev is None else dev / PER_C, "status": status,
               "computed_at": time.time()}
        c.execute(f"INSERT OR REPLACE INTO skin_night({','.join(row)}) VALUES({','.join('?' * len(row))})",
                  list(row.values()))
        out.append(row)
    c.commit()
    if own:
        c.close()
    return out


def recent(c, limit=30):
    """Newest first. Empty if skin_night does not exist yet."""
    try:
        return [dict(r) for r in c.execute("SELECT * FROM skin_night ORDER BY date DESC LIMIT ?", (limit,))]
    except Exception:
        return []


if __name__ == "__main__":
    for r in update(days=60):
        approx = "" if r["temp_raw"] is None else f"  (~{r['temp_raw'] / PER_C:.1f} C, not calibrated)"
        dev = "" if r["dev_raw"] is None else f"  vs usual {r['dev_raw']:+.0f} counts (~{r['dev_c']:+.1f} C)"
        print(f"{r['date']}  {r['status']:10s} {r['minutes']:4d} min{approx}{dev}")
