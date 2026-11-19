#!/usr/bin/env python3
"""Decode captured raw strap frames (ble_raw) into structured tables:
  - realtime_hr : verified HR + RR from REALTIME_DATA (0x28)
  - device_soc  : battery state-of-charge over time (from firmware SOC reports)
  - device_logs : reassembled firmware console text (0x32), one row per log line
The event-framed records (0x30 #3/#63) go to strap_samples as they are: one pair every
~9 minutes, 10 and 14 int16s, a status record (not raw optical data); labels not verified.
The stored 1 Hz history records (0x2F, version 24) are folded into ble_hr, so the seconds
the laptop was not connected get backfilled once the strap comes back in range, and into
motion_min, strap_min (seconds worn, skin temperature, steps, optical levels) and offwrist.
Version 25 records (a 40 s burst of one 24 Hz optical channel every ~20 minutes) stay raw.
"""
import db, whoop, struct, re, time, os, fcntl, json, math

EXTRA = """
-- rr_1024 is a historical name: the values are raw strap RR, which is whole milliseconds.
CREATE TABLE IF NOT EXISTS realtime_hr (rtc INTEGER PRIMARY KEY, hr INTEGER, rr_1024 TEXT);
-- rtc is the firmware's tick counter (restarts when the strap reboots); `seen` is when we
-- decoded the report, so "latest" and chart order survive a reboot.
CREATE TABLE IF NOT EXISTS device_soc (rtc INTEGER PRIMARY KEY, soc_pct REAL, subsystem TEXT, seen REAL);
CREATE TABLE IF NOT EXISTS device_logs (rtc INTEGER, subsystem TEXT, message TEXT);
CREATE INDEX IF NOT EXISTS idx_logs_rtc ON device_logs(rtc);
-- how much the wrist moved, per minute, from the accelerometer in the stored history
-- (record version 24): sum of |change in the gravity vector| between consecutive seconds,
-- and how many such steps. sleep.py reads move_sum / n. Kept after the raw frames go to R2.
CREATE TABLE IF NOT EXISTS motion_min (minute INTEGER PRIMARY KEY, move_sum REAL NOT NULL, n INTEGER NOT NULL);
-- the rest of each stored-history record (version 24), per minute; layout in whoop.parse_historical.
--   n          records stored that minute. The strap only stores records while it is on the
--              wrist, so this is seconds worn (it ticks a little fast: up to ~62 a minute).
--   temp_sum   sum of the raw skin temperature (temp_n readings); mean = temp_sum / temp_n.
--              Raw counts; ~0.1 C per count is likely but not calibrated (see signals.py).
--   opt_a_sum, opt_b_sum   sums of the two optical levels (n readings). Which LED each is,
--              is not known: kept so the data is not lost, not shown anywhere.
--   steps      steps counted by the strap that minute (from its running step counter)
CREATE TABLE IF NOT EXISTS strap_min (minute INTEGER PRIMARY KEY, n INTEGER NOT NULL,
  temp_sum REAL NOT NULL DEFAULT 0, temp_n INTEGER NOT NULL DEFAULT 0,
  opt_a_sum REAL NOT NULL DEFAULT 0, opt_b_sum REAL NOT NULL DEFAULT 0, steps INTEGER NOT NULL DEFAULT 0);
-- stretches where the strap stopped storing records although its record counter carried on
-- without a gap: it was off the wrist (its firmware log says OFF-WRIST / ON-WRIST at exactly
-- these times). Time that is neither here nor in strap_min is simply not known (not read yet).
CREATE TABLE IF NOT EXISTS offwrist (start REAL PRIMARY KEY, end REAL NOT NULL);
-- raw per-sample sensor records from the historical event stream.
-- event_cmd 63 = R10/R11 raw optical (PPG); 3 = paired sensor record.
-- i16_json is the payload parsed as little-endian int16s (field semantics are
-- candidate/unverified for this firmware's event format); hex is the lossless payload.
CREATE TABLE IF NOT EXISTS strap_samples (
  ts INTEGER, sub INTEGER, event_cmd INTEGER, n_i16 INTEGER, i16_json TEXT, hex TEXT);
CREATE INDEX IF NOT EXISTS idx_samples ON strap_samples(ts, event_cmd);
-- heart rate filled in behind cloud_sync's watermark (stored history read late): pushed once, then removed
CREATE TABLE IF NOT EXISTS cloud_hr_pending (ts REAL PRIMARY KEY);
"""

LINE_RE = re.compile(r"(\d+):\s*([A-Za-z0-9_]+):\s*(.*)")
SOC_RE  = re.compile(r"SOC report:\s*(\d{1,3}\.\d\d)$")   # whole value only: a cut line ("SOC report: 9") is not 9%

MARK = b"\x34\x00\x01"
MIN_TS = 1_600_000_000   # anything earlier is a strap clock that was never set
HR_RECORD_VERSION = 24   # the only stored-record layout whose heart rate field is verified
LOCK = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".decode.lock")

def _put(c, key, value):
    c.execute("INSERT INTO sync_state(key,value) VALUES(?,?) "
              "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))

def add_motion(c, accel):
    """Fold {unix second: (x, y, z) in g} into motion_min. Only steps between records at most
    2 s apart count, so a gap (strap off, not yet drained) never reads as one big movement."""
    per = {}
    ts = sorted(accel)
    for a, b in zip(ts, ts[1:]):
        if b - a <= 2:
            m = per.setdefault(int(b // 60) * 60, [0.0, 0])
            m[0] += math.dist(accel[a], accel[b]); m[1] += 1
    c.executemany("INSERT INTO motion_min(minute,move_sum,n) VALUES(?,?,?) ON CONFLICT(minute) DO UPDATE "
                  "SET move_sum = move_sum + excluded.move_sum, n = n + excluded.n",
                  [(m, v[0], v[1]) for m, v in per.items()])


OFF_GAP = 10          # seconds between two consecutive records that mean "not stored": off the wrist
TEMP_OK = (100, 500)  # raw skin temperature counts outside this are not a reading
MAX_STEP = 20         # the counter adds up to ~13 at once (it credits a walk once it is sure); more is a jump


def _merge(ranges):
    out = []
    for lo, hi in sorted(ranges):
        if out and lo <= out[-1][1] + 1:
            out[-1][1] = max(out[-1][1], hi)
        else:
            out.append([lo, hi])
    return out[-500:]      # a hole per lost stretch; only very old ones would ever fall off


def add_strap(c, recs):
    """Fold parsed version-24 records into strap_min and offwrist.

    The strap sometimes sends a record twice (in one drain or the next), so each record
    sequence number is used once: the ranges already folded in live in sync_state.
    Steps come from the difference of the running step counter between consecutive records;
    an off-wrist stretch is a time gap between records whose sequence numbers are consecutive.
    """
    ranges = json.loads(db.get_state(c, "strap_seq_seen", "[]"))
    def seen(s):
        return any(lo <= s <= hi for lo, hi in ranges)
    new = {}
    for p in recs:
        if p["seq"] not in new and not seen(p["seq"]):
            new[p["seq"]] = p
    if not new:
        return
    last = json.loads(db.get_state(c, "strap_last", "null"))      # [seq, ts, steps] of the newest record
    prev = {s: (p["ts"], p["steps_total"]) for s, p in new.items()}
    if last:
        prev.setdefault(last[0], (last[1], last[2]))
    per, gaps = {}, []
    for s in sorted(new):
        p = new[s]
        row = per.setdefault(int(p["ts"] // 60) * 60, [0, 0.0, 0, 0.0, 0.0, 0])
        row[0] += 1
        if TEMP_OK[0] <= p["skin_temp_raw"] <= TEMP_OK[1]:
            row[1] += p["skin_temp_raw"]; row[2] += 1
        row[3] += p["optical"][0]; row[4] += p["optical"][1]
        if s - 1 in prev:
            pts, psteps = prev[s - 1]
            dt = p["ts"] - pts
            if dt > OFF_GAP:
                gaps.append((pts + 1, p["ts"]))
            elif 0 <= dt <= 3 and 0 < p["steps_total"] - psteps <= MAX_STEP:
                row[5] += p["steps_total"] - psteps
    c.executemany(
        "INSERT INTO strap_min(minute,n,temp_sum,temp_n,opt_a_sum,opt_b_sum,steps) VALUES(?,?,?,?,?,?,?) "
        "ON CONFLICT(minute) DO UPDATE SET n=n+excluded.n, temp_sum=temp_sum+excluded.temp_sum, "
        "temp_n=temp_n+excluded.temp_n, opt_a_sum=opt_a_sum+excluded.opt_a_sum, "
        "opt_b_sum=opt_b_sum+excluded.opt_b_sum, steps=steps+excluded.steps",
        [(m, *v) for m, v in per.items()])
    c.executemany("INSERT OR IGNORE INTO offwrist(start,end) VALUES(?,?)", gaps)
    runs = []
    for s in sorted(new):
        if runs and s == runs[-1][1] + 1:
            runs[-1][1] = s
        else:
            runs.append([s, s])
    _put(c, "strap_seq_seen", json.dumps(_merge(ranges + runs)))
    top = max(new)
    if not last or top > last[0]:
        _put(c, "strap_last", json.dumps([top, new[top]["ts"], new[top]["steps_total"]]))


def _strap_rec(inner):
    """The parsed record if it is a version-24 record with a set clock, else None."""
    if not (inner and inner[0] == whoop.T_HISTORICAL and len(inner) > 1 and inner[1] == HR_RECORD_VERSION):
        return None
    p = whoop.parse_historical(inner)
    ts = p.get("ts")
    if "steps_total" not in p or not ts or not (MIN_TS < ts < time.time() + 86400):
        return None
    return p


def backfill_strap(c, upto):
    """Once: the per-minute strap signals from frames decoded before strap_min existed."""
    recs = []
    for r in c.execute("SELECT hex FROM ble_raw WHERE ptype = ? AND rowid <= ? ORDER BY rowid",
                       (whoop.T_HISTORICAL, upto)):
        p = _strap_rec(bytes.fromhex(r["hex"]))
        if p:
            recs.append(p)
    add_strap(c, recs)
    _put(c, "strap_backfilled", "1")


def backfill_motion(c, upto):
    """Once: movement from stored-history frames decoded before motion_min existed."""
    accel = {}
    for r in c.execute("SELECT hex FROM ble_raw WHERE ptype = ? AND rowid <= ?", (whoop.T_HISTORICAL, upto)):
        inner = bytes.fromhex(r["hex"])
        if len(inner) > 1 and inner[1] == HR_RECORD_VERSION:
            p = whoop.parse_historical(inner)
            ts, g = p.get("ts"), p.get("accel_g")
            if ts and MIN_TS < ts < time.time() + 86400 and g and all(abs(x) < 16 for x in g):
                accel.setdefault(ts, g)
    add_motion(c, accel)
    _put(c, "motion_backfilled", "1")


def run(full=False, quiet=False, force=False):
    """Decode new frames. One run at a time: two processes reading the same watermark (the
    daemon's timer and a Resync from the dashboard) would each decode the same frames."""
    with open(LOCK, "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        c = db.connect()
        try:
            return _run(c, full, quiet, force)
        finally:
            c.close()


def _section(c, name, fn):
    """Run one part of a decode pass in its own savepoint. A part that fails (a schema the
    code didn't expect, a frame nobody has seen before) is rolled back and reported, and the
    rest still runs: on the night of 18-19 Nov one bad firmware log line held back eight
    hours of heart rate, because the whole pass failed and the watermark never moved."""
    c.execute(f"SAVEPOINT {name}")
    try:
        out = fn()
    except Exception as ex:
        c.execute(f"ROLLBACK TO {name}")
        c.execute(f"RELEASE {name}")
        print(f"decode: {name} skipped ({ex})")
        return None
    c.execute(f"RELEASE {name}")
    return out


def _resume_point(c, last):
    """The rowid to continue after. Normally the stored watermark; but if ble_raw was emptied
    or rebuilt (a restored copy, a manual cleanup) SQLite numbers new frames from the top of
    what is left, which can be below the watermark: those frames would be skipped for good.
    The last decoded frame's arrival time tells them apart: rows at or below the watermark
    that arrived after it are new."""
    seen = db.get_state(c, "decode_ts")
    if seen is None or not last:
        return last
    r = c.execute("SELECT MAX(rowid) m FROM ble_raw WHERE rowid <= ? AND ts <= ?", (last, float(seen))).fetchone()
    resume = r["m"] or 0
    if resume != last and c.execute("SELECT 1 FROM ble_raw WHERE rowid > ? AND rowid <= ? LIMIT 1",
                                    (resume, last)).fetchone():
        print(f"decode: raw frames were renumbered (watermark {last}); decoding from row {resume + 1}")
        return resume
    return last


def _run(c, full, quiet, force):
    """Decode ble_raw frames that arrived since the last run (watermark = rowid).

    `full=True` (or `decode.py --full`) wipes the decoded tables and rebuilds them
    from every raw frame. The first run on a DB without a watermark does that too.
    The console-log stream is cut into frames at arbitrary points, so the unfinished
    tail of the last log line is carried over to the next run.
    """
    c.executescript(EXTRA)
    db.add_columns(c, [("device_soc", "seen", "REAL")])
    last = db.get_state(c, "decode_rowid")
    if full and not force and c.execute("SELECT 1 FROM raw_archive LIMIT 1").fetchone():
        # the raw frames behind older logs/battery rows now live in R2, not here
        print("decode --full would drop everything decoded from frames that archive.py moved to R2.\n"
              "Restore them first (archive.py restore), or pass --force to rebuild from local frames only.")
        return
    if full or last is None:
        for t in ("realtime_hr", "device_soc", "device_logs", "strap_samples", "motion_min", "strap_min", "offwrist"):
            c.execute(f"DELETE FROM {t}")
        c.execute("DELETE FROM sync_state WHERE key IN ('strap_seq_seen', 'strap_last')")
        _put(c, "strap_backfilled", "1")
        last, carry, hold = 0, "", b""
    else:
        last = _resume_point(c, int(last))
        if not db.get_state(c, "motion_backfilled"):
            _section(c, "motion_backfill", lambda: backfill_motion(c, last))
        if not db.get_state(c, "strap_backfilled"):
            _section(c, "strap_backfill", lambda: backfill_strap(c, last))
        carry = db.get_state(c, "decode_carry", "")
        hold = bytes.fromhex(db.get_state(c, "decode_hold", ""))
    fetched = c.execute("SELECT rowid, ts, hex FROM ble_raw WHERE rowid > ? ORDER BY rowid", (last,)).fetchall()
    if not fetched:
        if not quiet:
            print("decoded: nothing new")
        return
    rows = [bytes.fromhex(r["hex"]) for r in fetched]
    seen = time.time()
    n = {}

    def realtime():
        rt = 0
        for inner in rows:
            if inner and inner[0] == 0x28 and len(inner) >= 18:
                rtc = struct.unpack_from("<I", inner, 2)[0]
                hr = inner[8]; cnt = inner[9]
                rr = [struct.unpack_from("<H", inner, 10 + 2*i)[0] for i in range(min(cnt, 4))]
                c.execute("INSERT OR REPLACE INTO realtime_hr(rtc,hr,rr_1024) VALUES(?,?,?)",
                          (rtc, hr, " ".join(map(str, rr)))); rt += 1
        n["realtime_hr"] = rt

    def console_logs():
        # reassemble the fragmented ASCII stream, strip 34 00 01 markers
        nonlocal carry, hold
        blob = bytearray(hold)
        for inner in rows:
            if inner and inner[0] == 0x32:
                blob += inner[3:][7:]
        blob = bytes(blob)
        tail = b""
        for k in (2, 1):                      # a marker split across two runs: keep its start for next time
            if blob.endswith(MARK[:k]):
                tail, blob = blob[-k:], blob[:-k]
                break
        text = carry + blob.replace(MARK, b"").decode("ascii", "ignore")
        # the stream is "<lvl>, <rtc>: <SUBSYS>: <msg>. " — split on the ", <digits>:" anchor
        parts = re.split(r"\s*\d+,\s*", text)
        rest = parts.pop()                    # unfinished last line: finish it next run
        if len(rest) > 4096:                  # garbage with no anchors: don't let it grow forever
            rest = ""
        logs = soc = 0
        for p in parts:
            m = LINE_RE.match(p.strip())
            if not m:
                continue
            rtc, subsys, msg = int(m.group(1)), m.group(2), m.group(3).strip(" .")
            # the strap re-sends log lines after a reconnect; a repeat must not stop the decode
            c.execute("INSERT OR IGNORE INTO device_logs(rtc,subsystem,message) VALUES(?,?,?)", (rtc, subsys, msg))
            logs += 1
            sm = SOC_RE.search(msg)
            if sm:
                c.execute("INSERT OR REPLACE INTO device_soc(rtc,soc_pct,subsystem,seen) VALUES(?,?,?,?)",
                          (rtc, float(sm.group(1)), subsys, seen + soc * 1e-4)); soc += 1   # keeps arrival order
        n["device_logs"], n["device_soc"] = logs, soc
        carry, hold = rest, tail              # only once the lines are in (a failed pass keeps the old ones)

    def samples():
        # strap sensor samples from event stream (type 0x30, cmd 3 and 63)
        samp = 0
        for inner in rows:
            if inner and inner[0] == 0x30 and len(inner) >= 3 and inner[2] in (3, 63):
                d = inner[3:]
                if len(d) < 9:
                    continue
                ts = struct.unpack_from("<I", d, 1)[0]
                sub = struct.unpack_from("<H", d, 5)[0]
                plen = struct.unpack_from("<H", d, 7)[0]
                pay = d[9:9 + plen]
                i16 = [struct.unpack_from("<h", pay, i)[0] for i in range(0, len(pay) - 1, 2)]
                c.execute("INSERT INTO strap_samples(ts,sub,event_cmd,n_i16,i16_json,hex) VALUES(?,?,?,?,?,?)",
                          (ts, sub, inner[2], len(i16), json.dumps(i16), pay.hex()))
                samp += 1
        n["strap_samples"] = samp

    def history():
        n["history_hr_backfilled"] = backfill_history(c, rows)

    # heart rate first: it is what everything downstream waits for
    for name, fn in (("history", history), ("realtime", realtime), ("console_logs", console_logs),
                     ("samples", samples)):
        _section(c, name, fn)
    _put(c, "decode_rowid", fetched[-1]["rowid"])
    _put(c, "decode_ts", fetched[-1]["ts"])
    _put(c, "decode_carry", carry)
    _put(c, "decode_hold", hold.hex())
    c.commit()
    if quiet:
        return
    print(f"decoded {len(rows)} new frames: " + "  ".join(f"{k}={v}" for k, v in n.items()))
    # quick peek
    for r in c.execute("SELECT subsystem, COUNT(*) n FROM device_logs GROUP BY subsystem ORDER BY n DESC LIMIT 12"):
        print(f"  {r['subsystem']:20s} {r['n']}")


def backfill_history(c, rows):
    """Stored history (0x2F, one record per beat-detection tick, Unix timestamps) into ble_hr.

    Only record version 24 carries heart rate where we read it (verified: ts and hr match the
    live stream). The strap also stores other record versions (25 = a block of raw sensor
    samples); reading a heart rate out of those would invent data, so they stay raw only.
    Seconds the live stream already covered are left alone, so this only fills gaps.

    Records can arrive long after they were made, and out of order: after a night with the
    laptop asleep, and when the strap re-sends old records (19 Nov 2026 it started over from
    the start of its memory and replayed June 2025). Their timestamps are the strap's own and
    were checked against the live stream, so they are placed where they belong; a replay adds
    nothing twice because seconds that already have heart rate are skipped.
    """
    hist, newest_ts = 0, None
    by_second, accel = {}, {}
    add_strap(c, [p for p in map(_strap_rec, rows) if p])
    for inner in rows:
        if inner and inner[0] == whoop.T_HISTORICAL and len(inner) > 1 and inner[1] == HR_RECORD_VERSION:
            p = whoop.parse_historical(inner)
            ts, hr = p.get("ts"), p.get("hr")
            # a strap whose clock was never set stamps records near 1970: keep those raw only
            if not ts or not (MIN_TS < ts < time.time() + 86400):
                continue
            newest_ts = ts if newest_ts is None else max(newest_ts, ts)
            g = p.get("accel_g")
            if g and all(abs(x) < 16 for x in g):
                accel.setdefault(ts, g)
            if not hr or not (25 <= hr <= 240):
                continue
            k = p.get("rr_count") or 0
            rr = [x for x in p["rr_ms"][:k] if x] if k <= 4 else []
            by_second.setdefault(ts, []).append((hr, rr))
    # cloud_sync pushes heart rate newer than its watermark; rows filled in behind it are
    # queued so it pushes exactly those (pulling the watermark back re-sent everything newer,
    # 70-85 thousand rows a run while the strap replayed 2025).
    mark = float(db.get_state(c, "cloud_hr_ts") or 0)
    late = []
    for ts in sorted(by_second):
        if c.execute("SELECT 1 FROM ble_hr WHERE ts >= ? AND ts < ? LIMIT 1", (ts, ts + 1)).fetchone():
            continue
        # The strap ticks a little faster than once a second, so now and then two records carry
        # the same second: keep both (the second one half a second later), or its beats are lost.
        for i, (hr, rr) in enumerate(by_second[ts][:2]):
            c.execute("INSERT OR IGNORE INTO ble_hr(ts,hr,rr_ms) VALUES(?,?,?)",
                      (ts + 0.5 * i, hr, " ".join(map(str, rr))))
            hist += 1
            if mark and ts + 0.5 * i <= mark:
                late.append((ts + 0.5 * i,))
    c.executemany("INSERT OR IGNORE INTO cloud_hr_pending(ts) VALUES(?)", late)
    add_motion(c, accel)
    if newest_ts is not None:
        # how far the strap's stored history has been read: Telegram waits for last night to be
        # in before it sends the morning message (a replay of old records doesn't move it back)
        _put(c, "history_ts", max(newest_ts, int(float(db.get_state(c, "history_ts", 0) or 0))))
    return hist


if __name__ == "__main__":
    import sys
    run(full="--full" in sys.argv, force="--force" in sys.argv)
