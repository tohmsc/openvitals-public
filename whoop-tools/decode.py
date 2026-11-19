#!/usr/bin/env python3
"""Decode captured raw strap frames (ble_raw) into structured tables:
  - realtime_hr : verified HR + RR from REALTIME_DATA (0x28)
  - device_soc  : battery state-of-charge over time (from firmware SOC reports)
  - device_logs : reassembled firmware console text (0x32), one row per log line
The event-framed historical sensor records (0x30 #3/#63) are kept raw in
ble_raw / history_*.ndjson; their per-field layout is not yet fully verified,
so we don't assert labels we aren't sure of.
The stored 1 Hz history records (0x2F) are also folded into ble_hr, so the seconds
the laptop was not connected get backfilled once the strap comes back in range.
"""
import db, whoop, struct, re, time, os, fcntl, json, math

EXTRA = """
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
-- raw per-sample sensor records from the historical event stream.
-- event_cmd 63 = R10/R11 raw optical (PPG); 3 = paired sensor record.
-- i16_json is the payload parsed as little-endian int16s (field semantics are
-- candidate/unverified for this firmware's event format); hex is the lossless payload.
CREATE TABLE IF NOT EXISTS strap_samples (
  ts INTEGER, sub INTEGER, event_cmd INTEGER, n_i16 INTEGER, i16_json TEXT, hex TEXT);
CREATE INDEX IF NOT EXISTS idx_samples ON strap_samples(ts, event_cmd);
"""

LINE_RE = re.compile(r"(\d+):\s*([A-Za-z0-9_]+):\s*(.*)")
SOC_RE  = re.compile(r"SOC report:\s*([0-9.]+)")

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
        for t in ("realtime_hr", "device_soc", "device_logs", "strap_samples", "motion_min"):
            c.execute(f"DELETE FROM {t}")
        last, carry, hold = 0, "", b""
    else:
        last = int(last)
        if not db.get_state(c, "motion_backfilled"):
            backfill_motion(c, last)
        carry = db.get_state(c, "decode_carry", "")
        hold = bytes.fromhex(db.get_state(c, "decode_hold", ""))
    fetched = c.execute("SELECT rowid, hex FROM ble_raw WHERE rowid > ? ORDER BY rowid", (last,)).fetchall()
    if not fetched:
        if not quiet:
            print("decoded: nothing new")
        return
    rows = [bytes.fromhex(r["hex"]) for r in fetched]

    # REALTIME_DATA
    rt = 0
    for inner in rows:
        if inner and inner[0] == 0x28 and len(inner) >= 18:
            rtc = struct.unpack_from("<I", inner, 2)[0]
            hr = inner[8]; cnt = inner[9]
            rr = [struct.unpack_from("<H", inner, 10 + 2*i)[0] for i in range(min(cnt, 4))]
            c.execute("INSERT OR REPLACE INTO realtime_hr(rtc,hr,rr_1024) VALUES(?,?,?)",
                      (rtc, hr, " ".join(map(str, rr)))); rt += 1

    # CONSOLE_LOGS: reassemble the fragmented ASCII stream, strip 34 00 01 markers
    blob = bytearray(hold)
    for inner in rows:
        if inner and inner[0] == 0x32:
            blob += inner[3:][7:]
    blob = bytes(blob)
    hold = b""
    for k in (2, 1):                      # a marker split across two runs: keep its start for next time
        if blob.endswith(MARK[:k]):
            hold, blob = blob[-k:], blob[:-k]
            break
    text = carry + blob.replace(MARK, b"").decode("ascii", "ignore")
    # the stream is "<lvl>, <rtc>: <SUBSYS>: <msg>. " — split on the ", <digits>:" anchor
    parts = re.split(r"\s*\d+,\s*", text)
    carry = parts.pop()                   # unfinished last line: finish it next run
    if len(carry) > 4096:                 # garbage with no anchors: don't let it grow forever
        carry = ""
    logs = soc = 0
    latest_soc = None
    seen = time.time()
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
            latest_soc = float(sm.group(1))
    # strap sensor samples from event stream (type 0x30, cmd 3 and 63)
    _json = json
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
                      (ts, sub, inner[2], len(i16), _json.dumps(i16), pay.hex()))
            samp += 1
    # Stored history (0x2F, one record per beat-detection tick, Unix timestamps): backfill ble_hr.
    # Only record version 24 carries heart rate where we read it (verified: ts and hr match the
    # live stream). The strap also stores other record versions (25 = a block of raw sensor
    # samples); reading a heart rate out of those would invent data, so they stay raw only.
    # Seconds the live stream already covered are left alone, so this only fills gaps.
    hist, oldest, newest_ts = 0, None, None
    by_second, accel = {}, {}
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
            n = p.get("rr_count") or 0
            rr = [round(x / 1024 * 1000, 1) for x in p["rr_1024"][:n] if x] if n <= 4 else []
            by_second.setdefault(ts, []).append((hr, rr))
    for ts in sorted(by_second):
        if c.execute("SELECT 1 FROM ble_hr WHERE ts >= ? AND ts < ? LIMIT 1", (ts, ts + 1)).fetchone():
            continue
        # The strap ticks a little faster than once a second, so now and then two records carry
        # the same second: keep both (the second one half a second later), or its beats are lost.
        for i, (hr, rr) in enumerate(by_second[ts][:2]):
            c.execute("INSERT OR IGNORE INTO ble_hr(ts,hr,rr_ms) VALUES(?,?,?)",
                      (ts + 0.5 * i, hr, " ".join(map(str, rr))))
            hist += 1
        oldest = ts if oldest is None else min(oldest, ts)
    add_motion(c, accel)
    if newest_ts is not None:
        # how far the strap's stored history has been read: Telegram waits for last night to be
        # in before it sends the morning message
        _put(c, "history_ts", max(newest_ts, int(float(db.get_state(c, "history_ts", 0) or 0))))
    # cloud_sync pushes heart rate newer than its watermark; rows backfilled into the
    # past would be skipped, so pull the watermark back to cover them.
    mark = db.get_state(c, "cloud_hr_ts")
    if oldest is not None and mark and float(mark) >= oldest:
        _put(c, "cloud_hr_ts", oldest - 1)
    if latest_soc is not None:
        # device_soc is keyed by the firmware's tick counter, which restarts when the strap
        # reboots, so "the newest row" is not reliably the latest reading. Keep that here.
        _put(c, "battery_now", _json.dumps({"soc": latest_soc, "ts": time.time()}))
    _put(c, "decode_rowid", fetched[-1]["rowid"])
    _put(c, "decode_carry", carry)
    _put(c, "decode_hold", hold.hex())
    c.commit()
    if quiet:
        return
    print(f"decoded {len(rows)} new frames: realtime_hr={rt}  device_logs={logs}  device_soc={soc}  "
          f"strap_samples={samp}  history_hr_backfilled={hist}")
    # quick peek
    for r in c.execute("SELECT subsystem, COUNT(*) n FROM device_logs GROUP BY subsystem ORDER BY n DESC LIMIT 12"):
        print(f"  {r['subsystem']:20s} {r['n']}")

if __name__ == "__main__":
    import sys
    run(full="--full" in sys.argv, force="--force" in sys.argv)
